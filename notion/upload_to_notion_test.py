"""
popn_playdata_single.csv 를 Notion DB 로 업로드하는 테스트 스크립트 (수정 무효화)

- 실제 노션 데이터베이스나 페이지를 생성/수정하지 않습니다.
- 기존 DB와 비교하여 어떤 페이지가 추가/갱신/생략/커버추가 되는지 콘솔 로그로만 출력합니다.
"""
import csv
import mimetypes
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

COVER_MODE = "github"
GITHUB_USER = "betapa"
GITHUB_REPO = "popn_total"
GITHUB_BRANCH = "main"

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "popn_playdata_single.csv"
IMAGES_DIR = BASE_DIR.parent / "popn_homepage_scraper" / "images"
IMAGES_REPO_PATH = "popn_homepage_scraper/images"
MISSING_LOG = BASE_DIR / "missing_images.txt"

TOKEN = os.environ.get("NOTION_TOKEN")
DB_ID = os.environ.get("NOTION_DATABASE_ID")
PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")

NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"
REQUEST_INTERVAL = 0.35
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

FULL_MODE = "--full" in sys.argv
# 테스트 전용 스크립트이므로 DRY_RUN을 항상 True로 고정합니다.
DRY_RUN = True 

KST = timezone(timedelta(hours=9))
DATE_PROPS = ("updated_at", "song_updated_at", "play_updated_at")

# ============================================================
# [API 호출 공통] 재시도 + 429 대기
# ============================================================
session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}", "Notion-Version": NOTION_VERSION})

def api(method, path, retries=5, **kw):
    for attempt in range(1, retries + 1):
        try:
            res = session.request(method, API + path, timeout=60, **kw)
        except requests.RequestException as e:
            print(f"  [네트워크 오류] {e} (시도 {attempt}/{retries})")
            time.sleep(2 * attempt)
            continue
        time.sleep(REQUEST_INTERVAL)
        if res.status_code == 429:
            wait = float(res.headers.get("Retry-After", "2"))
            print(f"  [429] {wait}초 대기")
            time.sleep(wait)
            continue
        if res.status_code >= 500:
            time.sleep(2 * attempt)
            continue
        if not res.ok:
            raise RuntimeError(f"{method} {path} -> {res.status_code}: {res.text[:300]}")
        return res.json()
    raise RuntimeError(f"{method} {path} 재시도 초과")

# ============================================================
# [시간 유틸] CSV 의 ISO 문자열과 Notion 이 돌려주는 값을 같은 기준(UTC, 초 단위)으로 비교
# ============================================================
def parse_ts(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return d.astimezone(timezone.utc).replace(microsecond=0)

# ============================================================
# [1단계] DB 생성 / 스키마 보강 (Mock 처리)
# ============================================================
def create_database():
    print(f"[테스트] DB 생성 시뮬레이션 (부모 페이지: {PARENT_PAGE_ID})")
    print("실제로는 여기서 DB가 생성됩니다. 다음 단계를 위해 임시 DB ID를 반환합니다.")
    return "DUMMY_TEST_DB_ID"

def ensure_schema():
    if DB_ID == "DUMMY_TEST_DB_ID":
        print("[테스트] 가상 DB이므로 스키마 보강을 생략합니다.")
        return
        
    db = api("GET", f"/databases/{DB_ID}")
    missing = {n: {"date": {}} for n in DATE_PROPS if n not in db["properties"]}
    if missing:
        print(f"[테스트] DB 속성 추가 시뮬레이션 (실제 추가 안됨): {', '.join(missing)}")

# ============================================================
# [2단계] 기존 페이지 조회 -> {키: {id, has_cover, ts}}
# ============================================================
def row_key(version_name, title, dif_name):
    vn = (version_name or "").strip().replace(",", " ")
    dn = (dif_name or "").strip().replace(",", " ")
    return (vn, (title or "").strip(), dn)

def plain(prop):
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in items)

def load_existing():
    if DB_ID == "DUMMY_TEST_DB_ID":
        print("[테스트] 가상 DB이므로 기존 페이지 조회 생략 (모든 데이터가 신규로 간주됨)")
        return {}
        
    existing, cursor, dup = {}, None, 0
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        # query는 데이터를 읽기만 하므로 실제 요청 허용
        data = api("POST", f"/databases/{DB_ID}/query", json=body)
        for pg in data["results"]:
            p = pg["properties"]
            vn = (p["version_name"]["select"] or {}).get("name", "")
            dn = (p["dif_name"]["select"] or {}).get("name", "")
            key = row_key(vn, plain(p["title"]), dn)
            if key in existing:
                dup += 1
                continue
            date_prop = (p.get("updated_at") or {}).get("date") or {}
            existing[key] = {
                "id": pg["id"],
                "has_cover": pg.get("cover") is not None,
                "ts": parse_ts(date_prop.get("start")),
            }
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    if dup:
        print(f"[경고] Notion DB 에 중복 키 페이지 {dup}개 발견 (첫 페이지만 갱신 대상)")
    return existing

# ============================================================
# [3단계] 이미지 매칭 (버전 폴더 내에서 제목과 일치하는 파일 찾기)
# ============================================================
def norm_name(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\W_]+", "", s)

_image_index = {}

def image_index(version_num):
    if version_num not in _image_index:
        idx = {}
        folder = IMAGES_DIR / str(version_num)
        if folder.is_dir():
            for f in folder.iterdir():
                if f.suffix.lower() in IMAGE_EXTS:
                    idx.setdefault(norm_name(f.stem), f)
        _image_index[version_num] = idx
    return _image_index[version_num]

def find_image(version_num, title):
    folder = IMAGES_DIR / str(version_num)
    for ext in IMAGE_EXTS:
        p = folder / f"{title}{ext}"
        if p.exists():
            return p
    return image_index(version_num).get(norm_name(title))

# ============================================================
# [4단계] 커버 생성 (업로드 Mock)
# ============================================================
_upload_cache = {}

def upload_image(path):
    print(f"  -> [테스트] 이미지 파일 업로드 시뮬레이션: {path.name}")
    return "DUMMY_FILE_UPLOAD_ID"

def make_cover(img_path, fresh_upload=False):
    if COVER_MODE == "github":
        rel = img_path.relative_to(IMAGES_DIR).as_posix()
        url = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{IMAGES_REPO_PATH}/{quote(rel)}"
        return {"type": "external", "external": {"url": url}}
    
    if fresh_upload or img_path not in _upload_cache:
        _upload_cache[img_path] = upload_image(img_path)
    return {"type": "file_upload", "file_upload": {"id": _upload_cache[img_path]}}

# ============================================================
# [메인]
# ============================================================
def main():
    global DB_ID
    if not TOKEN:
        sys.exit("NOTION_TOKEN 환경변수를 설정하세요.")
    if not DB_ID:
        if not PARENT_PAGE_ID:
            sys.exit("NOTION_DATABASE_ID 또는 NOTION_PARENT_PAGE_ID 중 하나는 필요합니다.")
        DB_ID = create_database()

    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        if "updated_at" not in (reader.fieldnames or []):
            print("[경고] CSV 에 updated_at 컬럼이 없습니다. (전체 갱신으로 진행)")

    mode = "FULL" if FULL_MODE else "증분"
    print(f"\n=======================================================")
    print(f" 모드: 테스트 전용 (실제 Notion 수정 안함) | 검사 로직: {mode}")
    print(f" 총 CSV 데이터: {len(rows)}행")
    print(f"=======================================================\n")

    try:
        ensure_schema()
        print("기존 페이지 조회 중...")
        existing = load_existing()
    except Exception as e:
        sys.exit(f"기존 DB 조회 실패: {e}")
    
    print(f"기존 페이지 {len(existing)}개 (비교를 시작합니다)\n")

    missing, failed = set(), []
    created = updated = cover_only = skipped = 0
    seen_keys = set()

    for i, r in enumerate(rows, 1):
        key = row_key(r["version_name"], r["title"], r["dif_name"])

        if key in seen_keys:
            skipped += 1
            continue
        seen_keys.add(key)

        ex = existing.get(key)
        csv_ts = parse_ts(r.get("updated_at"))
        unchanged = (
            ex is not None and not FULL_MODE
            and csv_ts is not None and ex["ts"] == csv_ts
        )

        cover = None
        if COVER_MODE != "none" and not (ex and ex["has_cover"]):
            img = find_image(r.get("version_num", ""), r["title"])
            if img is None:
                missing.add(f'{r.get("version_num")}\t{r["title"]}')
            else:
                cover = img

        if unchanged and cover is None:
            skipped += 1
            continue

        # 실제로는 API로 PATCH, POST를 날리는 부분 생략 (Dry Run 처리)
        page_id = ex["id"] if ex else None
        
        # 상세 결과 로깅
        status_text = ""
        if page_id is None:
            created += 1
            status_text = "[신규 생성]"
        elif unchanged:
            cover_only += 1
            status_text = "[커버만 추가]"
        else:
            updated += 1
            status_text = "[페이지 갱신]"

        cover_text = "커버 적용" if cover is not None else "커버 없음"
        print(f"{status_text} 곡: {key[1]} ({key[0]}, {key[2]}) | {cover_text}")

    MISSING_LOG.write_text("\n".join(sorted(missing)), encoding="utf-8")
    
    print("\n=======================================================")
    print(f" 테스트 완료 통계 요약")
    print(f" - 신규 생성 예정     : {created}건")
    print(f" - 페이지 갱신 예정   : {updated}건")
    print(f" - 커버만 추가 예정   : {cover_only}건")
    print(f" - 변경 없음 (생략)   : {skipped}건")
    print(f" - 이미지 없음        : {len(missing)}건 -> {MISSING_LOG.name} 에 기록됨")
    print("=======================================================")
    print("※ 이 스크립트는 테스트 용도이며, 실제 데이터가 반영되지 않았습니다.")

if __name__ == "__main__":
    main()