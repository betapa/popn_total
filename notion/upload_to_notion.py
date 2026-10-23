"""
popn_playdata_single.csv 를 Notion DB 로 업로드한다. (변경분만 처리)

[원칙]
- 날짜 컬럼(updated_at / song_updated_at / play_updated_at)은 CSV(GitHub)에만 남고, Notion 으로는 전송하지 않는다.
- 변경 여부는 "CSV 값 vs Notion 에 실제로 저장된 값"을 직접 비교해서 판단한다.
    -> 별도 상태 파일/날짜 기준이 필요 없고, 이전 실행이 중간에 실패해도 다음 실행에서 자동으로 보충된다.
- 데이터 유실 방지
    * 페이지를 삭제/보관(archive)하지 않는다.
    * 정상 커버는 제거/교체하지 않는다. (커버가 없는 페이지에만 추가, --repair-covers 일 때만 '깨진 URL' 커버를 교체)
    * 변경된 속성만 PATCH 한다. (나머지 속성은 건드리지 않음)
    * CSV 의 기록 값(medal/score/best_version/bpm/notes/dif_tier)이 비어 있는데 Notion 에는 값이 있으면
      빈 값으로 덮어쓰지 않는다. (CSV 생성 오류로 인한 기록 소실 방지, --allow-blank 로 해제)
    * 페이지 생성 요청이 네트워크 오류로 불확실할 때는 DB 를 다시 조회해 중복 생성을 막는다.

[이미지 매핑]
    * 일반 곡(version_num 0~29)  -> images/<version_num>/<곡명>.png
    * 기타 곡(version_num=-1 / version_name="기타") -> images/etc/<곡명>.png
    * 매칭 순서 (폴더 안에서):
        1) 파일명 완전 일치(대소문자/유니코드 정규화 무시)
        2) 스크레이퍼가 저장한 파일명 규칙(윈도우 금지문자 제거) 일치
        3) 기호/공백 제거 후 일치
        4) 공백만 제거 후 일치 (기호만으로 된 제목 대응)
      -> 후보가 둘 이상이면 잘못된 커버를 막기 위해 '모호'로 처리하고 연결하지 않는다.
      -> 해당 폴더에 없으면 다른 폴더를 검색하고, 후보가 정확히 1개일 때만 대체 사용한다.
    * 이미지 파일이 HTML/빈 파일 등 손상된 경우 연결하지 않는다.
    * GitHub 모드: 커버로 쓰기 전 raw URL 이 실제로 열리는지(404 여부) 확인한다. (--no-verify-url 로 생략)

[images.csv] 이미지 - 문서 연결 점검 결과
    * 문서 연결 실패 : 이미지를 못 찾음 / 모호 / 손상 의심 / GitHub 404  (+ 비슷한 이미지 후보)
    * 폴더 불일치 대체 : 다른 폴더의 이미지를 대신 사용한 곡 (확인용)
    * 이미지 미사용   : 어떤 문서와도 연결되지 않은 이미지 파일 (+ 비슷한 곡 후보)

[동작]
    * Notion 에 없는 행                     -> 페이지 생성
    * 값이 다른 행                          -> 달라진 속성만 갱신
    * 값이 같은 행                          -> 생략 (속성 갱신 없음)
    * 값은 같지만 커버가 없고 이미지가 있음 -> 커버만 추가

옵션:
    --dry-run         Notion 에 쓰지 않고 어떤 처리가 일어날지만 출력 (DB ID 필수)
    --full            값이 같아도 모든 속성을 다시 기록 (복구용)
    --allow-blank     CSV 의 빈 값으로 Notion 의 기존 기록을 덮어쓰는 것을 허용
    --repair-covers   Notion 에 이미 있는 외부 URL 커버가 404 로 깨져 있으면 올바른 이미지로 교체 (GitHub 모드)
    --no-verify-url   커버 적용 전 raw URL 확인을 생략
"""
import csv
import difflib
import math
import mimetypes
import os
import re
import sys
import time
import unicodedata
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
IMAGES_REPORT = BASE_DIR / "images.csv"

# 어느 버전에도 속하지 않는 곡 (homepage_scraper.py 의 설정과 동일하게 유지)
ETC_VERSION_NUM = -1
ETC_VERSION_NAME = "기타"
ETC_IMAGE_FOLDER = "etc"

TOKEN = os.environ.get("NOTION_TOKEN")
DB_ID = os.environ.get("NOTION_DATABASE_ID")
PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")

NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"
REQUEST_INTERVAL = 0.35   # Notion 제한(평균 초당 3회) 준수
IMAGE_EXT_ORDER = [".png", ".jpg", ".jpeg", ".webp", ".gif"]   # 같은 이름이 여러 확장자면 앞쪽 우선
IMAGE_EXTS = set(IMAGE_EXT_ORDER)

DRY_RUN = "--dry-run" in sys.argv
FULL_MODE = "--full" in sys.argv
ALLOW_BLANK = "--allow-blank" in sys.argv
REPAIR_COVERS = "--repair-covers" in sys.argv
VERIFY_URL = "--no-verify-url" not in sys.argv

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
# [3단계] 기존 페이지 조회 -> {키: {id, has_cover, cover_type, cover_url, values}}
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
            cover = pg.get("cover") or {}
            ctype = cover.get("type")
            existing[key] = {
                "id": pg["id"],
                "has_cover": pg.get("cover") is not None,
                "cover_type": ctype,
                "cover_url": (cover.get(ctype) or {}).get("url") if ctype else None,
                "values": vals,
            }
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    if dup:
        print(f"[경고] Notion DB 에 중복 키 페이지 {len(dup)}개 발견 (첫 페이지만 갱신 대상, 나머지는 건드리지 않음 -> 수동 정리 필요)")
        for k in dup[:10]:
            print(f"   - {k[1]} ({k[0]}, {k[2]})")
    return existing


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
# [4단계] 이미지 매칭
# ============================================================
def clean_filename(filename):
    """homepage_scraper.py 가 이미지를 저장할 때 쓰는 파일명 규칙과 동일"""
    return re.sub(r'[\\/*?:"<>|]', "", filename or "").strip()


def norm_name(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\W_]+", "", s)   # 공백/기호 제거 (파일명에 못 쓰는 문자 차이 흡수)


def norm_loose(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").lower())   # 기호는 유지


def exact_key(s):
    return unicodedata.normalize("NFC", s or "").strip().casefold()


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


_all_folders = None


def all_folders():
    global _all_folders
    if _all_folders is None:
        _all_folders = sorted(d.name for d in IMAGES_DIR.iterdir() if d.is_dir()) if IMAGES_DIR.is_dir() else []
    return _all_folders


_folder_index = {}


def folder_index(folder_name):
    """폴더의 파일 인덱스 (폴더별 1회 생성). 같은 키에 여러 파일이 올 수 있어 리스트로 보관"""
    if folder_name in _folder_index:
        return _folder_index[folder_name]
    idx = {"files": [], "exact": {}, "norm": {}, "loose": {}}
    folder = IMAGES_DIR / folder_name
    if folder.is_dir():
        def rank(p):
            return (p.stem, IMAGE_EXT_ORDER.index(p.suffix.lower()))
        for f in sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS), key=rank):
            idx["files"].append(f)
            idx["exact"].setdefault(exact_key(f.stem), []).append(f)
            n = norm_name(f.stem)
            if n:
                idx["norm"].setdefault(n, []).append(f)
            idx["loose"].setdefault(norm_loose(f.stem), []).append(f)
    _folder_index[folder_name] = idx
    return idx


def pick(cands):
    """후보가 사실상 하나(같은 이름의 다른 확장자 포함)면 그 파일, 아니면 None(모호)"""
    if len({exact_key(p.stem) for p in cands}) == 1:
        return cands[0]
    return None


def lookup_in_folder(folder_name, title):
    """(찾은 파일 | None, 모호한 후보 목록)"""
    idx = folder_index(folder_name)
    tiers = (
        ("exact", exact_key(title)),
        ("exact", exact_key(clean_filename(title))),
        ("norm", norm_name(title)),
        ("loose", norm_loose(title)),
    )
    for kind, key in tiers:
        if not key:
            continue
        cands = idx[kind].get(key)
        if not cands:
            continue
        p = pick(cands)
        return (p, []) if p else (None, list(cands))
    return None, []


_broken_cache = {}


def looks_broken(path):
    """HTML 오류 페이지/빈 파일 등 이미지가 아닌 파일인지 (매직 바이트 확인)"""
    if path in _broken_cache:
        return _broken_cache[path]
    try:
        if path.stat().st_size < 100:
            res = True
        else:
            with open(path, "rb") as f:
                head = f.read(12)
            ok = (head.startswith(b"\x89PNG") or head[:2] == b"\xff\xd8" or head[:3] == b"GIF"
                  or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))
            res = not ok
    except OSError:
        res = True
    _broken_cache[path] = res
    return res


_link_cache = {}
used_images = set()


def resolve_link(version_num, version_name, title):
    """
    곡 -> 이미지 연결 결과
      status: ok | missing | ambiguous | broken
      cross : 다른 폴더의 이미지를 대체 사용했는지
    """
    folders = image_folders(version_num, version_name)
    ck = (tuple(folders), title)
    if ck in _link_cache:
        return _link_cache[ck]

    res = {"path": None, "status": "missing", "candidates": [], "cross": False, "folders": folders}
    path, ambiguous = None, []
    for fn in folders:
        p, cands = lookup_in_folder(fn, title)
        if p:
            path = p
            break
        if cands:
            ambiguous = cands

    if path is None and not ambiguous:
        # 지정 폴더에 없으면 다른 폴더를 검색 (후보가 정확히 1개일 때만 대체 사용)
        found = []
        for fn in all_folders():
            if fn in folders:
                continue
            p, cands = lookup_in_folder(fn, title)
            found.extend([p] if p else cands)
        if len(found) == 1:
            path, res["cross"] = found[0], True
        elif len(found) > 1:
            ambiguous = found

    if path is not None:
        if looks_broken(path):
            res.update(status="broken", candidates=[path], cross=False)
        else:
            res.update(path=path, status="ok")
            used_images.add(path)
    elif ambiguous:
        res.update(status="ambiguous", candidates=ambiguous)

    _link_cache[ck] = res
    return res


# ============================================================
# [5단계] 커버 생성 (GitHub URL 또는 Notion 파일 업로드)
# ============================================================
_upload_cache = {}
_url_cache = {}


def cover_url(img_path):
    rel = img_path.relative_to(IMAGES_DIR).as_posix()
    return f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{IMAGES_REPO_PATH}/{quote(rel)}"


def url_status(url):
    """True: 열림(200) / False: 없음(404·410) / None: 확인 불가 (네트워크 오류 등 -> 차단하지 않음)"""
    if url in _url_cache:
        return _url_cache[url]
    val = None
    for attempt in range(2):
        try:
            r = requests.head(url, timeout=15, allow_redirects=True)   # Notion 인증 세션이 아닌 일반 요청
            if r.status_code == 200:
                val = True
            elif r.status_code in (404, 410):
                val = False
            break
        except requests.RequestException:
            time.sleep(1)
    _url_cache[url] = val
    return val


def upload_image(path):
    ctype = mimetypes.guess_type(path.name)[0] or "image/png"
    fu = api("POST", "/file_uploads", json={"filename": path.name, "content_type": ctype})
    with open(path, "rb") as f:
        api("POST", f"/file_uploads/{fu['id']}/send", files={"file": (path.name, f, ctype)})
    return fu["id"]


def make_cover(img_path, fresh_upload=False):
    if COVER_MODE == "github":
        return {"type": "external", "external": {"url": cover_url(img_path)}}
    # upload 모드: 같은 이미지는 업로드 ID 를 재사용 (실패하면 새로 업로드)
    if fresh_upload or img_path not in _upload_cache:
        _upload_cache[img_path] = upload_image(img_path)
    return {"type": "file_upload", "file_upload": {"id": _upload_cache[img_path]}}


# ============================================================
# [6단계] 쓰기 (생성은 중복 방지 처리 포함)
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
    if cover_img is not None:                    # 커버가 없거나(--repair-covers 시) URL 이 깨진 페이지에만 전달됨
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
# [7단계] images.csv 리포트 (이미지 - 문서 연결 점검)
# ============================================================
REASON_TEXT = {
    "missing": "이미지 파일 없음 (모든 폴더에서 못 찾음)",
    "ambiguous": "후보 이미지가 여러 개라 연결하지 않음 (모호)",
    "broken": "이미지 파일 손상 의심 (HTML/빈 파일 등)",
}

failures = {}        # (폴더, 제목) -> 문서 연결 실패 정보
substitutions = {}   # (폴더, 제목) -> 다른 폴더 이미지로 대체한 곡


def _record(store, version_num, version_name, title, dif, cover_state, reason, found=""):
    folders = image_folders(version_num, version_name)
    k = (folders[0], title)
    e = store.setdefault(k, {
        "version_num": version_num, "version_name": version_name, "title": title,
        "folders": folders, "difs": [], "cover": [], "reason": reason, "found": found,
    })
    if dif not in e["difs"]:
        e["difs"].append(dif)
    if cover_state not in e["cover"]:
        e["cover"].append(cover_state)


def write_image_report():
    all_files = [p for fn in all_folders() for p in folder_index(fn)["files"]]
    orphans = [p for p in all_files if p not in used_images]
    orphan_by_folder = {}
    for p in orphans:
        orphan_by_folder.setdefault(p.parent.name, []).append(p)

    def suggest(folders, title):
        pool = [p for fl in folders for p in orphan_by_folder.get(fl, [])] or orphans
        names = {}
        for p in pool:
            names.setdefault(norm_name(p.stem) or p.stem, p)
        m = difflib.get_close_matches(norm_name(title) or title, list(names), n=3, cutoff=0.5)
        return " | ".join(f"{names[x].parent.name}/{names[x].name}" for x in m)

    headers = ["type", "version_num", "version_name", "title", "difficulties", "notion_cover",
               "reason", "expected_path", "found_image", "candidates", "url"]
    rows = []

    for e in sorted(failures.values(), key=lambda x: (str(x["version_num"]), x["title"])):
        folder = e["folders"][0]
        cands = " | ".join(f"{p.parent.name}/{p.name}" for p in e.get("cands", []))
        rows.append({
            "type": "문서 연결 실패", "version_num": e["version_num"], "version_name": e["version_name"],
            "title": e["title"], "difficulties": "/".join(e["difs"]), "notion_cover": "/".join(e["cover"]),
            "reason": e["reason"], "expected_path": f"images/{folder}/{clean_filename(e['title'])}.png",
            "found_image": e["found"], "candidates": cands or suggest(e["folders"], e["title"]),
            "url": e.get("url", ""),
        })

    for e in sorted(substitutions.values(), key=lambda x: (str(x["version_num"]), x["title"])):
        rows.append({
            "type": "폴더 불일치 대체", "version_num": e["version_num"], "version_name": e["version_name"],
            "title": e["title"], "difficulties": "/".join(e["difs"]), "notion_cover": "/".join(e["cover"]),
            "reason": "지정 폴더에 없어 다른 폴더의 이미지를 사용", "expected_path": f"images/{e['folders'][0]}/{clean_filename(e['title'])}.png",
            "found_image": e["found"], "candidates": "", "url": "",
        })

    fail_titles = {}
    for e in failures.values():
        fail_titles.setdefault(norm_name(e["title"]) or e["title"], e["title"])
    for p in sorted(orphans):
        m = difflib.get_close_matches(norm_name(p.stem) or p.stem, list(fail_titles), n=2, cutoff=0.5)
        rows.append({
            "type": "이미지 미사용", "version_num": "", "version_name": "", "title": p.stem,
            "difficulties": "", "notion_cover": "", "reason": "어떤 문서와도 연결되지 않은 이미지",
            "expected_path": "", "found_image": f"{p.parent.name}/{p.name}",
            "candidates": " | ".join(fail_titles[x] for x in m), "url": "",
        })

    with open(IMAGES_REPORT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)
    print(f"[이미지 점검] 문서 연결 실패 {len(failures)}곡 / 폴더 불일치 대체 {len(substitutions)}곡 / "
          f"미사용 이미지 {len(orphans)}개 -> {IMAGES_REPORT.name}")


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
                             ("REPAIR-COVERS", REPAIR_COVERS), ("NO-VERIFY-URL", not VERIFY_URL)) if on]
    print(f"CSV {len(rows)}행 / 옵션: {', '.join(flags) if flags else '없음 (증분)'}")
    if COVER_MODE != "none" and not IMAGES_DIR.is_dir():
        print(f"[경고] 이미지 폴더를 찾을 수 없습니다: {IMAGES_DIR}")

    try:
        check_schema()
        print("기존 페이지 조회 중...")
        existing = load_existing()
    except Exception as e:
        sys.exit(f"기존 DB 조회 실패 (integration 연결/DB ID 확인): {e}")
    print(f"기존 페이지 {len(existing)}개 (값 비교를 시작합니다)")

    failed = []
    created = updated = cover_only = skipped = protected_rows = 0
    seen_keys, csv_dups = set(), []
    url_404_sample = None

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
        has_cover = bool(ex and ex["has_cover"])
        vnum, vname, title = r.get("version_num", ""), r.get("version_name", ""), want["title"]

        # ---- 이미지 연결 (모든 행에서 점검: 파일 시스템만 사용하므로 빠름) ----
        cover_img, repair = None, False
        if COVER_MODE != "none":
            cover_state = "신규" if ex is None else ("있음" if has_cover else "없음")
            link = resolve_link(vnum, vname, title)

            if link["status"] != "ok":
                _record(failures, vnum, vname, title, key[2], cover_state, REASON_TEXT[link["status"]])
                failures[(image_folders(vnum, vname)[0], title)]["cands"] = link["candidates"]
            elif link["cross"]:
                _record(substitutions, vnum, vname, title, key[2], cover_state, "", f"{link['path'].parent.name}/{link['path'].name}")

            need_cover = not has_cover
            if (has_cover and REPAIR_COVERS and COVER_MODE == "github"
                    and ex["cover_type"] == "external" and ex["cover_url"]
                    and url_status(ex["cover_url"]) is False):
                need_cover, repair = True, True      # 기존 커버 URL 이 404 -> 교체 대상

            if need_cover and link["status"] == "ok":
                url = cover_url(link["path"]) if COVER_MODE == "github" else ""
                if COVER_MODE == "github" and VERIFY_URL and url_status(url) is False:
                    # 로컬에는 있지만 GitHub 에 없어서 Notion 에서 보이지 않을 이미지 -> 커버를 붙이지 않음
                    _record(failures, vnum, vname, title, key[2], cover_state,
                            "GitHub raw URL 이 404 (push 안 됨/경로·브랜치 불일치)",
                            f"{link['path'].parent.name}/{link['path'].name}")
                    failures[(image_folders(vnum, vname)[0], title)]["url"] = url
                    url_404_sample = url_404_sample or url
                else:
                    cover_img = link["path"]

        if ex is None:
            status, fields, prot = "신규 생성", list(FIELDS), []
        else:
            fields, prot = diff_fields(want, ex["values"])
            if fields:
                status = "페이지 갱신"
            elif cover_img is not None:
                status = "커버 복구" if repair else "커버만 추가"
            else:
                status = None
        if prot:
            protected_rows += 1
            print(f"  [보호] {key[1]} ({key[0]}, {key[2]}): CSV 의 빈 값으로 덮어쓰지 않음 -> {', '.join(prot)}")

        if status is None:
            skipped += 1
            continue

        if cover_img is not None:
            cover_text = "커버 복구(깨진 URL 교체)" if repair else "커버 추가"
        elif has_cover:
            cover_text = "기존 커버 유지"
        elif COVER_MODE == "none":
            cover_text = "커버 사용 안 함"
        else:
            cover_text = "이미지 연결 실패 -> images.csv"
        detail = f" | 변경: {', '.join(fields)}" if status == "페이지 갱신" else ""
        print(f"[{status}] 곡: {key[1]} ({key[0]}, {key[2]}){detail} | {cover_text}")

        try:
            if not DRY_RUN:
                props = {} if status in ("커버만 추가", "커버 복구") else build_properties(want, fields)
                if ex is None:
                    with_upload_retry(lambda fresh: create_page(key, props, cover_img, fresh), cover_img)
                else:
                    with_upload_retry(lambda fresh: update_page(ex["id"], props, cover_img, fresh), cover_img)
            if ex is None:
                created += 1
            elif status in ("커버만 추가", "커버 복구"):
                cover_only += 1
            else:
                updated += 1
        except Exception as e:
            failed.append((key, str(e)[:200]))
            print(f"  [실패] {key}: {str(e)[:200]}")

        done = created + updated + cover_only
        if done and done % 50 == 0:
            print(f"진행 {i}/{len(rows)} (추가 {created}, 갱신 {updated}, 커버 {cover_only}, 생략 {skipped}, 실패 {len(failed)})")

    if COVER_MODE != "none":
        write_image_report()
        if url_404_sample:
            print(f"[안내] GitHub raw URL 404 가 있었습니다. 예: {url_404_sample}\n"
                  f"       (이미지가 push 되었는지, 저장소가 public 인지, 브랜치명이 '{GITHUB_BRANCH}' 인지 확인하세요)")

    if csv_dups:
        print(f"[경고] CSV 에 같은 키(version_name+title+dif_name)가 중복된 행 {len(csv_dups)}개를 건너뜀 (Notion 에는 첫 행만 반영)")
        for k in csv_dups[:10]:
            print(f"   - {k[1]} ({k[0]}, {k[2]})")

    tag = " [DRY-RUN: 실제 반영 없음]" if DRY_RUN else ""
    print(
        f"\n완료{tag}: 추가 {created}, 갱신 {updated}, 커버 추가/복구 {cover_only}, 변경 없음(생략) {skipped}, "
        f"빈 값 보호 {protected_rows}행, 실패 {len(failed)}, 이미지 연결 실패 {len(failures)}곡 -> {IMAGES_REPORT.name}"
    )
    for k, msg in failed[:20]:
        print("  -", k, msg)
    if failed:
        sys.exit(1)   # 실패 행은 다음 실행 때 값 비교로 자동 재시도됨 (Actions 에서도 실패로 표시)


if __name__ == "__main__":
    main()