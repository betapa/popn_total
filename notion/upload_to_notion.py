"""
popn_playdata_single.csv 를 Notion DB 로 업로드한다. (변경분만 처리)

[원칙]
- 날짜 컬럼(updated_at / song_updated_at / play_updated_at)은 CSV(GitHub)에만 남고, Notion 으로는 전송하지 않는다.
- 변경 여부는 "CSV 값 vs Notion 에 실제로 저장된 값"을 직접 비교해서 판단한다.
    -> 별도 상태 파일/날짜 기준이 필요 없고, 이전 실행이 중간에 실패해도 다음 실행에서 자동으로 보충된다.
- 데이터 유실 방지
    * 페이지를 삭제/보관(archive)하지 않는다.
    * 기존 커버를 제거/교체하지 않는다. (커버가 없는 페이지에만 추가)
    * 변경된 속성만 PATCH 한다. (나머지 속성은 건드리지 않음)
    * CSV 의 기록 값(medal/score/best_version/bpm/notes/dif_tier)이 비어 있는데 Notion 에는 값이 있으면
      빈 값으로 덮어쓰지 않는다. (CSV 생성 오류로 인한 기록 소실 방지, --allow-blank 로 해제)
    * 페이지 생성 요청이 네트워크 오류로 불확실할 때는 DB 를 다시 조회해 중복 생성을 막는다.

[이미지 매핑]
    * 일반 곡(version_num 0~29)  -> images/<version_num>/<곡명>.png
    * 기타 곡(version_num=-1 / version_name="기타") -> images/etc/<곡명>.png
      (homepage_scraper.py 가 어느 버전에도 속하지 않는 곡의 이미지를 etc 폴더에 저장함)

[동작]
    * Notion 에 없는 행                     -> 페이지 생성
    * 값이 다른 행                          -> 달라진 속성만 갱신
    * 값이 같은 행                          -> 생략 (이미지 업로드/속성 갱신 없음)
    * 값은 같지만 커버가 없고 이미지가 있음 -> 커버만 추가

옵션:
    --dry-run       Notion 에 쓰지 않고 어떤 처리가 일어날지만 출력 (DB ID 필수)
    --full          값이 같아도 모든 속성을 다시 기록 (복구용)
    --allow-blank   CSV 의 빈 값으로 Notion 의 기존 기록을 덮어쓰는 것을 허용
"""
import csv
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
MISSING_LOG = BASE_DIR / "missing_images.txt"

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
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

DRY_RUN = "--dry-run" in sys.argv
FULL_MODE = "--full" in sys.argv
ALLOW_BLANK = "--allow-blank" in sys.argv

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
# [3단계] 기존 페이지 조회 -> {키: {id, has_cover, values}}
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
            existing[key] = {"id": pg["id"], "has_cover": pg.get("cover") is not None, "values": vals}
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
# [4단계] 이미지 매칭 (버전 폴더 / etc 폴더 내에서 제목과 일치하는 파일 찾기)
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


# ============================================================
# [5단계] 커버 생성 (GitHub URL 또는 Notion 파일 업로드)
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
        rel = img_path.relative_to(IMAGES_DIR).as_posix()
        url = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{IMAGES_REPO_PATH}/{quote(rel)}"
        return {"type": "external", "external": {"url": url}}
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
    if cover_img is not None:                    # 커버가 없는 페이지에만 전달됨 (기존 커버는 절대 건드리지 않음)
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

    flags = [n for n, on in (("DRY-RUN", DRY_RUN), ("FULL", FULL_MODE), ("ALLOW-BLANK", ALLOW_BLANK)) if on]
    print(f"CSV {len(rows)}행 / 옵션: {', '.join(flags) if flags else '없음 (증분)'}")

    try:
        check_schema()
        print("기존 페이지 조회 중...")
        existing = load_existing()
    except Exception as e:
        sys.exit(f"기존 DB 조회 실패 (integration 연결/DB ID 확인): {e}")
    print(f"기존 페이지 {len(existing)}개 (값 비교를 시작합니다)")

    missing, failed = set(), []
    created = updated = cover_only = skipped = protected_rows = 0
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
        has_cover = bool(ex and ex["has_cover"])

        # 커버가 없는 페이지(또는 신규)만 이미지 탐색 (기타 곡은 images/etc 에서 탐색)
        cover_img = None
        if COVER_MODE != "none" and not has_cover:
            cover_img = find_image(r.get("version_num", ""), want["title"], r.get("version_name", ""))
            if cover_img is None:
                folder_label = image_folders(r.get("version_num", ""), r.get("version_name", ""))[0]
                missing.add(f'{folder_label}\t{want["title"]}')

        if ex is None:
            status, fields, prot = "신규 생성", list(FIELDS), []
        else:
            fields, prot = diff_fields(want, ex["values"])
            if fields:
                status = "페이지 갱신"
            elif cover_img is not None:
                status = "커버만 추가"
            else:
                status = None
        if prot:
            protected_rows += 1
            print(f"  [보호] {key[1]} ({key[0]}, {key[2]}): CSV 의 빈 값으로 덮어쓰지 않음 -> {', '.join(prot)}")

        if status is None:
            skipped += 1
            continue

        cover_text = "기존 커버 유지" if has_cover else ("커버 추가" if cover_img is not None else "이미지 없음")
        detail = f" | 변경: {', '.join(fields)}" if status == "페이지 갱신" else ""
        print(f"[{status}] 곡: {key[1]} ({key[0]}, {key[2]}){detail} | {cover_text}")

        try:
            if not DRY_RUN:
                props = build_properties(want, fields) if status != "커버만 추가" else {}
                if ex is None:
                    with_upload_retry(lambda fresh: create_page(key, props, cover_img, fresh), cover_img)
                else:
                    with_upload_retry(lambda fresh: update_page(ex["id"], props, cover_img, fresh), cover_img)
            if ex is None:
                created += 1
            elif status == "커버만 추가":
                cover_only += 1
            else:
                updated += 1
        except Exception as e:
            failed.append((key, str(e)[:200]))
            print(f"  [실패] {key}: {str(e)[:200]}")

        done = created + updated + cover_only
        if done and done % 50 == 0:
            print(f"진행 {i}/{len(rows)} (추가 {created}, 갱신 {updated}, 커버만 {cover_only}, 생략 {skipped}, 실패 {len(failed)})")

    MISSING_LOG.write_text("\n".join(sorted(missing)), encoding="utf-8")

    if csv_dups:
        print(f"[경고] CSV 에 같은 키(version_name+title+dif_name)가 중복된 행 {len(csv_dups)}개를 건너뜀 (Notion 에는 첫 행만 반영)")
        for k in csv_dups[:10]:
            print(f"   - {k[1]} ({k[0]}, {k[2]})")

    tag = " [DRY-RUN: 실제 반영 없음]" if DRY_RUN else ""
    print(
        f"\n완료{tag}: 추가 {created}, 갱신 {updated}, 커버만 추가 {cover_only}, 변경 없음(생략) {skipped}, "
        f"빈 값 보호 {protected_rows}행, 실패 {len(failed)}, 이미지 없음 {len(missing)}곡 -> {MISSING_LOG.name}"
    )
    for k, msg in failed[:20]:
        print("  -", k, msg)
    if failed:
        sys.exit(1)   # 실패 행은 다음 실행 때 값 비교로 자동 재시도됨 (Actions 에서도 실패로 표시)


if __name__ == "__main__":
    main()