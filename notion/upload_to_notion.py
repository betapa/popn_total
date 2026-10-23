"""
popn_playdata_single.csv 를 Notion DB 로 업로드한다. (변경분만 처리)

[원칙]
- 날짜 컬럼(updated_at / song_updated_at / play_updated_at)은 CSV(GitHub)에만 남고, Notion 으로는 전송하지 않는다.
- 변경 여부는 "CSV 값 vs Notion 에 실제로 저장된 값"을 직접 비교해서 판단한다.
    -> 별도 상태 파일/날짜 기준이 필요 없고, 이전 실행이 중간에 실패해도 다음 실행에서 자동으로 보충된다.
- 데이터 유실 방지
    * 페이지를 삭제/보관(archive)하지 않는다.
    * 정상 커버는 제거/교체하지 않는다. (커버가 없거나, 검증에 실패한 "깨진 커버"인 페이지에만 새 커버를 넣는다)
    * 변경된 속성만 PATCH 한다. (나머지 속성은 건드리지 않음)
    * CSV 의 기록 값(medal/score/best_version/bpm/notes/dif_tier)이 비어 있는데 Notion 에는 값이 있으면
      빈 값으로 덮어쓰지 않는다. (CSV 생성 오류로 인한 기록 소실 방지, --allow-blank 로 해제)
    * 페이지 생성 요청이 네트워크 오류로 불확실할 때는 DB 를 다시 조회해 중복 생성을 막는다.

[이미지 매핑]
    * 일반 곡(version_num 0~29)  -> images/<version_num>/<곡명>.png
    * 기타 곡(version_num=-1 / version_name="기타") -> images/etc/<곡명>.png
      (homepage_scraper.py 가 어느 버전에도 속하지 않는 곡의 이미지를 etc 폴더에 저장함)

[이미지 검증]  (깨진 이미지는 21B, 정상 이미지는 대부분 10KB 초과)
    * 넣으려는 로컬 이미지: 크기가 MIN_IMAGE_BYTES 이상이고 PNG/JPEG/GIF/WEBP 시그니처여야 한다.
    * github 모드에서는 실제로 Notion 이 읽게 될 raw URL 도 미리 받아 보고 같은 기준으로 확인한다.
      (GitHub 에 아직 push 되지 않았거나, 경로/유니코드 표기가 달라 404 이거나, 손상된 파일이면 커버를 넣지 않는다)
    * Notion 에 이미 저장된 커버: 커버 URL 을 받아 같은 기준으로 확인한다. 검증에 실패하면 "깨진 커버"로 보고
      올바른 이미지가 있을 때 교체한다. (네트워크 오류 등으로 확인하지 못한 커버는 건드리지 않는다)
    * 연결에 실패한 문서/이미지는 images.csv 에 정리한다. (실행할 때마다 새로 작성)

[동작]
    * Notion 에 없는 행                     -> 페이지 생성
    * 값이 다른 행                          -> 달라진 속성만 갱신
    * 값이 같은 행                          -> 생략 (이미지 업로드/속성 갱신 없음)
    * 값은 같지만 커버가 없고 이미지가 있음 -> 커버만 추가
    * 값은 같지만 커버가 깨져 있고 올바른 이미지가 있음 -> 커버만 교체

옵션:
    --dry-run           Notion 에 쓰지 않고 어떤 처리가 일어날지만 출력 (DB ID 필수)
    --full              값이 같아도 모든 속성을 다시 기록 (복구용)
    --allow-blank       CSV 의 빈 값으로 Notion 의 기존 기록을 덮어쓰는 것을 허용
    --skip-cover-check  커버/이미지 URL 을 내려받아 확인하는 단계를 생략 (로컬 파일 검증만 수행)
"""
import csv
import math
import mimetypes
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import requests

# ============================================================
# [설정]
# ============================================================
COVER_MODE = "github"          # "github" | "upload" | "none"
GITHUB_USER = "betapa"
GITHUB_REPO = "popn_total"
GITHUB_BRANCH = "main"

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "popn_playdata_single.csv"
IMAGES_DIR = BASE_DIR.parent / "popn_homepage_scraper" / "images"
IMAGES_REPO_PATH = "popn_homepage_scraper/images"
MISSING_LOG = BASE_DIR / "missing_images.txt"
IMAGES_CSV = BASE_DIR / "images.csv"          # 이미지 연결에 실패한 문서 / 이미지 정보

# 어느 버전에도 속하지 않는 곡 (homepage_scraper.py 의 설정과 동일하게 유지)
ETC_VERSION_NUM = -1
ETC_VERSION_NAME = "기타"
ETC_IMAGE_FOLDER = "etc"

# 이미지 검증 기준: 깨진 이미지는 21B, 정상 이미지는 대부분 10KB 초과이므로 1KB 미만이면 비정상으로 본다.
MIN_IMAGE_BYTES = 1024
CHECK_WORKERS = 8                                  # 기존 커버 URL 을 병렬로 확인할 스레드 수
URL_CHECK_TIMEOUT = 30

TOKEN = os.environ.get("NOTION_TOKEN")
DB_ID = os.environ.get("NOTION_DATABASE_ID")
PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")

NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"
REQUEST_INTERVAL = 0.35   # Notion 제한(평균 초당 3회) 준수
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

DRY_RUN = "--dry-run" in sys.argv
FULL_MODE = "--full" in sys.argv
ALLOW_BLANK = "--allow-blank" in sys.argv
VERIFY_URLS = "--skip-cover-check" not in sys.argv

# Notion 으로 전송되는 속성 (날짜 컬럼은 의도적으로 제외)
TEXT_FIELDS = ("genre", "artist", "bpm")
SELECT_FIELDS = ("version_name", "dif_name", "medal", "best_version")
NUMBER_FIELDS = ("level", "notes", "dif_tier", "score")
FIELDS = ("title",) + TEXT_FIELDS + SELECT_FIELDS + NUMBER_FIELDS
# 빈 값으로 덮어쓰지 않도록 보호할 속성
PROTECT_FIELDS = ("bpm", "notes", "dif_tier", "medal", "score", "best_version")

# ============================================================
# [API 호출 공통] 재시도 + 429 대기
# ============================================================
session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}", "Notion-Version": NOTION_VERSION})


class UncertainResult(RuntimeError):
    """요청이 Notion 에 반영됐는지 알 수 없는 상태 (생성 요청의 중복 방지용)"""


def api(method, path, retries=5, safe_retry=True, **kw):
    """safe_retry=False 이면 네트워크 오류/5xx 시 재시도하지 않고 UncertainResult 를 발생시킨다."""
    for attempt in range(1, retries + 1):
        try:
            res = session.request(method, API + path, timeout=60, **kw)
        except requests.RequestException as e:
            print(f"  [네트워크 오류] {e} (시도 {attempt}/{retries})")
            if not safe_retry:
                raise UncertainResult(str(e))
            time.sleep(2 * attempt)
            continue
        time.sleep(REQUEST_INTERVAL)
        if res.status_code == 429:
            wait = float(res.headers.get("Retry-After", "2"))
            print(f"  [429] {wait}초 대기")
            time.sleep(wait)
            continue
        if res.status_code >= 500:
            if not safe_retry:
                raise UncertainResult(f"{res.status_code}")
            time.sleep(2 * attempt)
            continue
        if not res.ok:
            raise RuntimeError(f"{method} {path} -> {res.status_code}: {res.text[:300]}")
        return res.json()
    raise RuntimeError(f"{method} {path} 재시도 초과")


# ============================================================
# [1단계] DB 생성 / 스키마 확인 (확인은 읽기 전용)
# ============================================================
def create_database():
    sel = {"select": {}}
    body = {
        "parent": {"type": "page_id", "page_id": PARENT_PAGE_ID},
        "title": [{"type": "text", "text": {"content": "pop'n music 스코어"}}],
        "properties": {
            "title": {"title": {}},
            "version_name": sel,
            "genre": {"rich_text": {}},
            "artist": {"rich_text": {}},
            "dif_name": sel,
            "level": {"number": {"format": "number"}},
            "bpm": {"rich_text": {}},
            "notes": {"number": {"format": "number"}},
            "dif_tier": {"number": {"format": "number"}},
            "medal": sel,
            "score": {"number": {"format": "number"}},
            "best_version": sel,
        },
    }
    db = api("POST", "/databases", json=body)
    print(f"DB 생성 완료. 다음 실행부터는 NOTION_DATABASE_ID={db['id']} 를 설정하세요.")
    return db["id"]


def check_schema():
    """필요한 속성이 DB 에 모두 있는지 확인 (없으면 중간에 실패하기 전에 종료)"""
    db = api("GET", f"/databases/{DB_ID}")
    props = db.get("properties", {})
    need = {"title": "title", "genre": "rich_text", "artist": "rich_text", "bpm": "rich_text",
            "version_name": "select", "dif_name": "select", "medal": "select", "best_version": "select",
            "level": "number", "notes": "number", "dif_tier": "number", "score": "number"}
    problems = [f"{n}({t})" for n, t in need.items() if props.get(n, {}).get("type") != t]
    if problems:
        sys.exit(f"[중단] Notion DB 에 필요한 속성이 없거나 타입이 다릅니다: {', '.join(problems)}")


# ============================================================
# [2단계] 값 정규화: CSV 값 <-> Notion 값을 같은 기준으로 비교
# ============================================================
def to_num(v):
    s = str(v if v is not None else "").strip()
    if s == "":
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    return round(x, 6) if math.isfinite(x) else None


def sel_name(v):
    return (v or "").strip().replace(",", " ").strip()[:100]   # 선택 옵션 이름에는 쉼표 불가


def desired_values(r):
    """CSV 행 -> Notion 에 저장될 값(정규화)"""
    d = {"title": (r.get("title") or "").strip()[:2000]}
    for f in TEXT_FIELDS:
        d[f] = (r.get(f) or "").strip()[:2000]
    for f in SELECT_FIELDS:
        d[f] = sel_name(r.get(f))
    for f in NUMBER_FIELDS:
        d[f] = to_num(r.get(f))
    return d


def plain(prop):
    prop = prop or {}
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in items)


def notion_values(p):
    """Notion 페이지 properties -> 정규화된 값"""
    d = {"title": plain(p.get("title")).strip()}
    for f in TEXT_FIELDS:
        d[f] = plain(p.get(f)).strip()
    for f in SELECT_FIELDS:
        d[f] = ((p.get(f) or {}).get("select") or {}).get("name", "").strip()
    for f in NUMBER_FIELDS:
        d[f] = to_num((p.get(f) or {}).get("number"))
    return d


def is_blank(v):
    return v is None or v == ""


def diff_fields(want, have):
    """(Notion 에 써야 할 속성 목록, 빈 값 덮어쓰기 방지로 제외된 속성 목록)"""
    changed, protected = [], []
    for f in FIELDS:
        if not ALLOW_BLANK and f in PROTECT_FIELDS and is_blank(want[f]) and not is_blank(have[f]):
            protected.append(f)
            continue
        if FULL_MODE or want[f] != have[f]:
            changed.append(f)
    return changed, protected


def prop_payload(f, v):
    if f == "title":
        return {"title": [{"type": "text", "text": {"content": v}}]}
    if f in TEXT_FIELDS:
        return {"rich_text": [{"type": "text", "text": {"content": v}}] if v else []}
    if f in SELECT_FIELDS:
        return {"select": {"name": v} if v else None}
    return {"number": v}


def build_properties(want, fields):
    return {f: prop_payload(f, want[f]) for f in fields}


# ============================================================
# [3단계] 이미지 유효성 검증 (로컬 파일 / URL 공통 기준)
# ============================================================
def sniff_image_type(head):
    """파일 앞부분(시그니처)으로 이미지 형식을 판별. 이미지가 아니면 None"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def check_image_content(size, head):
    """(정상 여부, 사유). size 는 전체 바이트 수, head 는 앞 16바이트 이상"""
    if size < MIN_IMAGE_BYTES:
        return False, f"파일 크기 {size}B (최소 {MIN_IMAGE_BYTES}B 미만)"
    if sniff_image_type(head) is None:
        return False, "이미지 형식(PNG/JPEG/GIF/WEBP)이 아님"
    return True, ""


_local_check = {}


def validate_local_image(path):
    """(정상 여부, 사유, 바이트 수) - 같은 파일은 한 번만 검사"""
    if path not in _local_check:
        try:
            size = path.stat().st_size
            if size < MIN_IMAGE_BYTES:
                ok, why = check_image_content(size, b"")
            else:
                with open(path, "rb") as f:
                    head = f.read(16)
                ok, why = check_image_content(size, head)
            _local_check[path] = (ok, why, size)
        except OSError as e:
            _local_check[path] = (False, f"파일을 읽을 수 없음: {e}", None)
    return _local_check[path]


_url_check = {}


def check_image_url(url):
    """
    URL 을 내려받아 (상태, 사유, 바이트 수) 를 반환한다. 상태는 "ok" | "bad" | "unknown".
      - ok      : 200 응답이고 정상 이미지
      - bad     : 404 등 4xx 이거나, 200 이지만 이미지가 아님(예: 21B 짜리 오류 본문)
      - unknown : 네트워크 오류 / 429 / 5xx 로 판단하지 못함 (기존 커버를 건드리지 않기 위해 bad 와 구분)
    주의: Notion 토큰이 담긴 session 이 아니라 별도 요청을 사용한다. (토큰을 외부 서버로 보내지 않기 위함)
    """
    if url in _url_check:
        return _url_check[url]

    result = ("unknown", "확인하지 못함", None)
    for attempt in range(1, 4):
        try:
            res = requests.get(url, timeout=URL_CHECK_TIMEOUT, headers={"User-Agent": "popn-notion-uploader"})
        except requests.RequestException as e:
            result = ("unknown", f"요청 실패: {type(e).__name__}", None)
            time.sleep(attempt)
            continue
        if res.status_code == 429 or res.status_code >= 500:
            result = ("unknown", f"HTTP {res.status_code}", None)
            time.sleep(2 * attempt)
            continue
        if res.status_code != 200:
            result = ("bad", f"HTTP {res.status_code}", None)
            break
        data = res.content
        ok, why = check_image_content(len(data), data[:16])
        result = ("ok" if ok else "bad", why, len(data))
        break

    _url_check[url] = result
    return result


# ============================================================
# [4단계] 기존 페이지 조회 -> {키: {id, cover, cover_state, values}}
# ============================================================
def row_key(version_name, title, dif_name):
    return (sel_name(version_name), (title or "").strip(), sel_name(dif_name))


def load_existing():
    existing, cursor, dup = {}, None, []
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = api("POST", f"/databases/{DB_ID}/query", json=body)
        for pg in data["results"]:
            vals = notion_values(pg["properties"])
            key = row_key(vals["version_name"], vals["title"], vals["dif_name"])
            if key in existing:
                dup.append(key)          # 같은 키의 페이지가 이미 있음 -> 첫 번째 것만 사용 (삭제하지 않음)
                continue
            existing[key] = {"id": pg["id"], "cover": pg.get("cover"), "values": vals}
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    if dup:
        print(f"[경고] Notion DB 에 중복 키 페이지 {len(dup)}개 발견 (첫 페이지만 갱신 대상, 나머지는 건드리지 않음 -> 수동 정리 필요)")
        for k in dup[:10]:
            print(f"   - {k[1]} ({k[0]}, {k[2]})")
    return existing


def cover_url_of(cover):
    """Notion 페이지의 cover 객체에서 이미지 URL 추출 (external / file 모두 지원)"""
    if not cover:
        return None
    return (cover.get(cover.get("type")) or {}).get("url")


def classify_existing_covers(existing):
    """
    Notion 에 저장된 커버가 실제로 올바른 이미지인지 확인해서 각 페이지에 cover_state 를 기록한다.
      none    : 커버 없음
      ok      : 정상 이미지
      broken  : URL 이 없거나, 404 이거나, 이미지가 아님(21B 등)  -> 교체 대상
      unknown : 네트워크 오류 등으로 확인하지 못함               -> 건드리지 않음
    """
    urls = set()
    for ex in existing.values():
        ex["cover_url"] = cover_url_of(ex["cover"])
        ex["cover_reason"] = ""
        if ex["cover"] is None:
            ex["cover_state"] = "none"
        elif not ex["cover_url"]:
            ex["cover_state"], ex["cover_reason"] = "broken", "커버 URL 없음"
        elif not VERIFY_URLS:
            ex["cover_state"] = "ok"
        else:
            ex["cover_state"] = "pending"
            urls.add(ex["cover_url"])

    if urls:
        print(f"기존 커버 {len(urls)}개의 이미지 상태를 확인합니다...")
        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            list(pool.map(check_image_url, urls))

    for ex in existing.values():
        if ex["cover_state"] == "pending":
            state, why, _ = _url_check[ex["cover_url"]]
            ex["cover_state"] = {"ok": "ok", "bad": "broken"}.get(state, "unknown")
            ex["cover_reason"] = why

    counts = {}
    for ex in existing.values():
        counts[ex["cover_state"]] = counts.get(ex["cover_state"], 0) + 1
    print(f"기존 커버 상태: 정상 {counts.get('ok', 0)}, 깨짐 {counts.get('broken', 0)}, "
          f"없음 {counts.get('none', 0)}, 확인 불가 {counts.get('unknown', 0)}")


def find_page_id(key):
    """생성 요청의 반영 여부가 불확실할 때 키로 DB 를 다시 조회"""
    vn, title, dn = key
    body = {
        "page_size": 1,
        "filter": {"and": [
            {"property": "title", "title": {"equals": title}},
            {"property": "version_name", "select": {"equals": vn}},
            {"property": "dif_name", "select": {"equals": dn}},
        ]},
    }
    res = api("POST", f"/databases/{DB_ID}/query", json=body).get("results", [])
    return res[0]["id"] if res else None


# ============================================================
# [5단계] 이미지 매칭 (버전 폴더 / etc 폴더 내에서 제목과 일치하는 파일 찾기)
# ============================================================
def norm_name(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\W_]+", "", s)   # 공백/기호 제거 (파일명에 못 쓰는 문자 차이 흡수)


def is_etc_song(version_num, version_name=""):
    """어느 버전에도 속하지 않는 곡인지 (version_num=-1 또는 version_name='기타')"""
    if to_num(version_num) == ETC_VERSION_NUM:
        return True
    return sel_name(version_name) == ETC_VERSION_NAME


def image_folders(version_num, version_name=""):
    """
    이미지를 찾을 폴더 이름 후보 (우선순위 순).
    - 기타 곡: images/etc (혹시 모를 images/-1 도 보조로 확인)
    - 일반 곡: images/<version_num>  ('3.0' 같은 표기도 '3' 으로 정리)
    """
    if is_etc_song(version_num, version_name):
        return [ETC_IMAGE_FOLDER, str(ETC_VERSION_NUM)]
    num = to_num(version_num)
    if num is not None and float(num).is_integer():
        return [str(int(num))]
    return [str(version_num or "").strip()]


_image_index = {}


def image_index(folder_name):
    """폴더의 {정규화된 파일명: 경로} 사전 (폴더별 1회만 생성)"""
    if folder_name not in _image_index:
        idx = {}
        folder = IMAGES_DIR / folder_name
        if folder.is_dir():
            for f in folder.iterdir():
                if f.suffix.lower() in IMAGE_EXTS:
                    idx.setdefault(norm_name(f.stem), f)
        _image_index[folder_name] = idx
    return _image_index[folder_name]


def find_image(version_num, title, version_name=""):
    for folder_name in image_folders(version_num, version_name):
        folder = IMAGES_DIR / folder_name
        # 1) 정확히 일치하는 파일명 우선
        for ext in IMAGE_EXTS:
            p = folder / f"{title}{ext}"
            if p.exists():
                return p
        # 2) 정규화 비교
        found = image_index(folder_name).get(norm_name(title))
        if found is not None:
            return found
    return None


def github_url(img_path):
    rel = img_path.relative_to(IMAGES_DIR).as_posix()
    return f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{IMAGES_REPO_PATH}/{quote(rel)}"


def pick_cover_image(r, title):
    """
    넣을 커버 이미지를 고르고 검증한다. 반환: (이미지 경로 또는 None, 실패 정보 또는 None)
    실패 정보: {"reason", "image_path", "image_bytes", "cover_url"}
    검증 순서: 파일 존재 -> 로컬 파일 정상 여부(크기/시그니처) -> (github 모드) raw URL 이 정상 이미지를 돌려주는지
    """
    version_num, version_name = r.get("version_num", ""), r.get("version_name", "")
    img = find_image(version_num, title, version_name)
    if img is None:
        folder = image_folders(version_num, version_name)[0]
        return None, {"reason": f"이미지 파일 없음 (images/{folder}/)", "image_path": "", "image_bytes": "", "cover_url": ""}

    ok, why, size = validate_local_image(img)
    if not ok:
        return None, {"reason": f"로컬 이미지 손상: {why}", "image_path": str(img), "image_bytes": size if size is not None else "", "cover_url": ""}

    if COVER_MODE == "github" and VERIFY_URLS:
        url = github_url(img)
        state, why, rsize = check_image_url(url)
        if state != "ok":
            label = "GitHub URL 검증 실패" if state == "bad" else "GitHub URL 확인 불가"
            hint = " (GitHub 에 push 됐는지, 저장소 경로/브랜치/파일명 표기를 확인)" if why.startswith("HTTP 4") else ""
            return None, {"reason": f"{label}: {why}{hint}", "image_path": str(img),
                          "image_bytes": rsize if rsize is not None else size, "cover_url": url}
    return img, None


# ============================================================
# [6단계] 커버 생성 (GitHub URL 또는 Notion 파일 업로드)
# ============================================================
_upload_cache = {}


def upload_image(path):
    ctype = mimetypes.guess_type(path.name)[0] or "image/png"
    fu = api("POST", "/file_uploads", json={"filename": path.name, "content_type": ctype})
    with open(path, "rb") as f:
        api("POST", f"/file_uploads/{fu['id']}/send", files={"file": (path.name, f, ctype)})
    return fu["id"]


def make_cover(img_path, fresh_upload=False):
    if COVER_MODE == "github":
        return {"type": "external", "external": {"url": github_url(img_path)}}
    # upload 모드: 같은 이미지는 업로드 ID 를 재사용 (실패하면 새로 업로드)
    if fresh_upload or img_path not in _upload_cache:
        _upload_cache[img_path] = upload_image(img_path)
    return {"type": "file_upload", "file_upload": {"id": _upload_cache[img_path]}}


# ============================================================
# [7단계] 쓰기 (생성은 중복 방지 처리 포함)
# ============================================================
def create_page(key, props, cover_img, fresh=False):
    body = {"parent": {"database_id": DB_ID}, "properties": props}
    if cover_img is not None:
        body["cover"] = make_cover(cover_img, fresh)
    try:
        return api("POST", "/pages", safe_retry=False, json=body)
    except UncertainResult:
        time.sleep(3)
        pid = find_page_id(key)          # 실제로는 생성되었는지 확인
        if pid:
            patch = {"properties": props}
            if cover_img is not None:
                patch["cover"] = body["cover"]
            return api("PATCH", f"/pages/{pid}", json=patch)
        return api("POST", "/pages", json=body)   # 생성되지 않았음이 확인됨 -> 안전하게 재시도


def update_page(page_id, props, cover_img, fresh=False):
    body = {}
    if props:
        body["properties"] = props
    if cover_img is not None:                    # 커버가 없거나 깨진 페이지에만 전달됨 (정상 커버는 절대 건드리지 않음)
        body["cover"] = make_cover(cover_img, fresh)
    return api("PATCH", f"/pages/{page_id}", json=body)


def with_upload_retry(fn, cover_img):
    """upload 모드에서 캐시된 업로드 ID 가 실패하면 새로 업로드해서 1회 재시도"""
    try:
        return fn(False)
    except RuntimeError:
        if COVER_MODE == "upload" and cover_img is not None:
            return fn(True)
        raise


# ============================================================
# [8단계] images.csv (이미지 연결에 실패한 문서 / 이미지 정보)
# ============================================================
IMAGES_CSV_HEADERS = ["version_num", "version_name", "title", "dif_name", "notion_page_id",
                      "notion_cover", "reason", "image_path", "image_bytes", "cover_url"]


def save_image_failures(failures):
    with open(IMAGES_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=IMAGES_CSV_HEADERS)
        writer.writeheader()
        writer.writerows(failures)


# ============================================================
# [메인]
# ============================================================
def main():
    global DB_ID
    if not TOKEN:
        sys.exit("NOTION_TOKEN 환경변수를 설정하세요.")
    if not DB_ID:
        if DRY_RUN:
            sys.exit("--dry-run 은 DB 를 생성하지 않으므로 NOTION_DATABASE_ID 가 필요합니다.")
        if not PARENT_PAGE_ID:
            sys.exit("NOTION_DATABASE_ID 또는 NOTION_PARENT_PAGE_ID 중 하나는 필요합니다.")
        DB_ID = create_database()

    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = set(reader.fieldnames or [])
    missing_cols = [c for c in ("title", "version_name", "dif_name") if c not in cols]
    if missing_cols:
        sys.exit(f"[중단] CSV 에 필수 컬럼이 없습니다: {missing_cols}")
    if not rows:
        sys.exit("[중단] CSV 에 데이터가 없습니다. (비정상 파일로 판단하여 업로드하지 않음)")

    flags = [n for n, on in (("DRY-RUN", DRY_RUN), ("FULL", FULL_MODE), ("ALLOW-BLANK", ALLOW_BLANK),
                             ("SKIP-COVER-CHECK", not VERIFY_URLS)) if on]
    print(f"CSV {len(rows)}행 / 옵션: {', '.join(flags) if flags else '없음 (증분)'}")

    try:
        check_schema()
        print("기존 페이지 조회 중...")
        existing = load_existing()
        if COVER_MODE != "none":
            classify_existing_covers(existing)
        else:
            for ex in existing.values():
                ex["cover_state"], ex["cover_url"], ex["cover_reason"] = "ok", cover_url_of(ex["cover"]), ""
    except Exception as e:
        sys.exit(f"기존 DB 조회 실패 (integration 연결/DB ID 확인): {e}")
    print(f"기존 페이지 {len(existing)}개 (값 비교를 시작합니다)")

    missing, failed, image_failures = set(), [], []
    created = updated = cover_only = cover_fixed = skipped = protected_rows = 0
    seen_keys, csv_dups = set(), []

    for i, r in enumerate(rows, 1):
        key = row_key(r.get("version_name"), r.get("title"), r.get("dif_name"))

        # CSV 내 중복 키는 같은 페이지를 덮어쓰게 되므로 첫 행만 처리하고 알려준다
        if key in seen_keys:
            csv_dups.append(key)
            skipped += 1
            continue
        seen_keys.add(key)

        want = desired_values(r)
        ex = existing.get(key)
        cover_state = ex["cover_state"] if ex else "none"   # none | ok | broken | unknown

        # 커버가 없거나 깨진 페이지(또는 신규)만 이미지를 고르고 검증한다. (기타 곡은 images/etc 에서 탐색)
        # 정상(ok)이거나 확인하지 못한(unknown) 커버는 그대로 둔다.
        cover_img, cover_fail = None, None
        if COVER_MODE != "none" and cover_state in ("none", "broken"):
            cover_img, cover_fail = pick_cover_image(r, want["title"])
            if cover_img is None and cover_fail["reason"].startswith("이미지 파일 없음"):
                missing.add(f'{image_folders(r.get("version_num", ""), r.get("version_name", ""))[0]}\t{want["title"]}')

        if ex is None:
            status, fields, prot = "신규 생성", list(FIELDS), []
        else:
            fields, prot = diff_fields(want, ex["values"])
            if fields:
                status = "페이지 갱신"
            elif cover_img is not None:
                status = "깨진 커버 교체" if cover_state == "broken" else "커버만 추가"
            else:
                status = None
        if prot:
            protected_rows += 1
            print(f"  [보호] {key[1]} ({key[0]}, {key[2]}): CSV 의 빈 값으로 덮어쓰지 않음 -> {', '.join(prot)}")

        def record_failure(page_id):
            """이미지 연결에 실패한 문서를 images.csv 용으로 보관"""
            if cover_fail is None:
                return
            image_failures.append({
                "version_num": r.get("version_num", ""), "version_name": r.get("version_name", ""),
                "title": want["title"], "dif_name": r.get("dif_name", ""),
                "notion_page_id": page_id or "",
                "notion_cover": "깨짐" if cover_state == "broken" else "없음",
                "reason": cover_fail["reason"], "image_path": cover_fail["image_path"],
                "image_bytes": cover_fail["image_bytes"],
                "cover_url": cover_fail["cover_url"] or (ex["cover_url"] if ex and cover_state == "broken" else "") or "",
            })

        if status is None:
            record_failure(ex["id"] if ex else "")
            skipped += 1
            continue

        if cover_img is not None:
            cover_text = "깨진 커버 교체" if cover_state == "broken" else "커버 추가"
        elif cover_state == "broken":
            cover_text = "깨진 커버 유지 (교체할 올바른 이미지 없음)"
        elif cover_state in ("ok", "unknown"):
            cover_text = "기존 커버 유지"
        else:
            cover_text = "이미지 없음/검증 실패"
        detail = f" | 변경: {', '.join(fields)}" if status == "페이지 갱신" else ""
        print(f"[{status}] 곡: {key[1]} ({key[0]}, {key[2]}){detail} | {cover_text}")

        page_id = ex["id"] if ex else ""
        try:
            if not DRY_RUN:
                props = build_properties(want, fields) if status not in ("커버만 추가", "깨진 커버 교체") else {}
                if ex is None:
                    res = with_upload_retry(lambda fresh: create_page(key, props, cover_img, fresh), cover_img)
                    page_id = (res or {}).get("id", "")
                else:
                    with_upload_retry(lambda fresh: update_page(ex["id"], props, cover_img, fresh), cover_img)
            if ex is None:
                created += 1
            elif status == "커버만 추가":
                cover_only += 1
            elif status == "깨진 커버 교체":
                cover_fixed += 1
            else:
                updated += 1
                if cover_img is not None and cover_state == "broken":
                    cover_fixed += 1
        except Exception as e:
            failed.append((key, str(e)[:200]))
            print(f"  [실패] {key}: {str(e)[:200]}")
        record_failure(page_id)

        done = created + updated + cover_only + cover_fixed
        if done and done % 50 == 0:
            print(f"진행 {i}/{len(rows)} (추가 {created}, 갱신 {updated}, 커버만 {cover_only}, 커버 교체 {cover_fixed}, 생략 {skipped}, 실패 {len(failed)})")

    MISSING_LOG.write_text("\n".join(sorted(missing)), encoding="utf-8")
    save_image_failures(image_failures)

    if csv_dups:
        print(f"[경고] CSV 에 같은 키(version_name+title+dif_name)가 중복된 행 {len(csv_dups)}개를 건너뜀 (Notion 에는 첫 행만 반영)")
        for k in csv_dups[:10]:
            print(f"   - {k[1]} ({k[0]}, {k[2]})")

    tag = " [DRY-RUN: 실제 반영 없음]" if DRY_RUN else ""
    print(
        f"\n완료{tag}: 추가 {created}, 갱신 {updated}, 커버만 추가 {cover_only}, 깨진 커버 교체 {cover_fixed}, "
        f"변경 없음(생략) {skipped}, 빈 값 보호 {protected_rows}행, 실패 {len(failed)}, "
        f"이미지 없음 {len(missing)}곡 -> {MISSING_LOG.name}"
    )
    print(f"이미지 연결 실패 {len(image_failures)}행 -> {IMAGES_CSV.name}")
    for k, msg in failed[:20]:
        print("  -", k, msg)
    if failed:
        sys.exit(1)   # 실패 행은 다음 실행 때 값 비교로 자동 재시도됨 (Actions 에서도 실패로 표시)


if __name__ == "__main__":
    main()