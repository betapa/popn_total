"""
popn_playdata_single.csv 를 Notion DB 로 업로드한다. (변경분만 처리)

- DB 가 없으면 자동 생성 (NOTION_PARENT_PAGE_ID 필요)
- 기존 DB 에 updated_at / song_updated_at / play_updated_at 속성이 없으면 자동 추가
- 행 키(version_name + title + dif_name)로 기존 페이지와 매칭 (재실행해도 중복되지 않음)
    * 신규 행                        -> 페이지 생성
    * CSV updated_at != Notion 값    -> 페이지 갱신
    * CSV updated_at == Notion 값    -> 생략 (속성 갱신/이미지 탐색/업로드 없음)
    * 변경은 없지만 커버가 없고 이미지가 있는 경우 -> 커버만 추가
- images/{version_num}/ 에서 곡 제목과 일치하는 이미지를 찾아 페이지 커버로 설정

옵션:
    --full      updated_at 과 무관하게 모든 행을 갱신 (스키마 변경/복구용)
    --dry-run   Notion 에 쓰지 않고 추가/갱신/생략 건수만 출력
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
DRY_RUN = "--dry-run" in sys.argv

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
# [1단계] DB 생성 / 스키마 보강
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
            **{name: {"date": {}} for name in DATE_PROPS},
        },
    }
    db = api("POST", "/databases", json=body)
    print(f"DB 생성 완료. 다음 실행부터는 NOTION_DATABASE_ID={db['id']} 를 설정하세요.")
    return db["id"]


def ensure_schema():
    """기존 DB 에 날짜 속성이 없으면 추가 (이미 있으면 아무 것도 하지 않음)"""
    db = api("GET", f"/databases/{DB_ID}")
    missing = {n: {"date": {}} for n in DATE_PROPS if n not in db["properties"]}
    if missing:
        print(f"DB 에 속성 추가: {', '.join(missing)}")
        if not DRY_RUN:
            api("PATCH", f"/databases/{DB_ID}", json={"properties": missing})


# ============================================================
# [2단계] 기존 페이지 조회 -> {키: {id, has_cover, ts}}
# ============================================================
def row_key(version_name, title, dif_name):
    # select 속성은 저장 시 쉼표가 공백으로 치환되므로 키 비교에도 동일하게 적용
    vn = (version_name or "").strip().replace(",", " ")
    dn = (dif_name or "").strip().replace(",", " ")
    return (vn, (title or "").strip(), dn)


def plain(prop):
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in items)


def load_existing():
    existing, cursor, dup = {}, None, 0
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = api("POST", f"/databases/{DB_ID}/query", json=body)
        for pg in data["results"]:
            p = pg["properties"]
            vn = (p["version_name"]["select"] or {}).get("name", "")
            dn = (p["dif_name"]["select"] or {}).get("name", "")
            key = row_key(vn, plain(p["title"]), dn)
            if key in existing:
                dup += 1          # 이미 같은 키의 페이지가 있음 -> 첫 번째 것만 사용
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
        print(f"[경고] Notion DB 에 중복 키 페이지 {dup}개 발견 (첫 페이지만 갱신 대상, 나머지는 수동 정리 필요)")
    return existing


# ============================================================
# [3단계] 이미지 매칭 (버전 폴더 내에서 제목과 일치하는 파일 찾기)
# ============================================================
def norm_name(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\W_]+", "", s)   # 공백/기호 제거 (파일명에 못 쓰는 문자 차이 흡수)


_image_index = {}


def image_index(version_num):
    """버전 폴더의 {정규화된 파일명: 경로} 사전 (폴더별 1회만 생성)"""
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
    # 1) 정확히 일치하는 파일명 우선
    for ext in IMAGE_EXTS:
        p = folder / f"{title}{ext}"
        if p.exists():
            return p
    # 2) 정규화 비교
    return image_index(version_num).get(norm_name(title))


# ============================================================
# [4단계] 커버 생성 (GitHub URL 또는 Notion 파일 업로드)
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
# [5단계] 행 -> Notion 속성 변환
# ============================================================
def to_num(v):
    try:
        return float(str(v).strip()) if str(v).strip() != "" else None
    except ValueError:
        return None


def rt(v):
    return {"rich_text": [{"type": "text", "text": {"content": (v or "").strip()[:2000]}}]}


def sel(v):
    v = (v or "").strip().replace(",", " ")   # 선택 옵션 이름에는 쉼표 불가
    return {"select": {"name": v[:100]} if v else None}


def dt(v):
    v = (v or "").strip()
    return {"date": {"start": v} if v else None}


def build_properties(r):
    return {
        "title": {"title": [{"type": "text", "text": {"content": r["title"].strip()[:2000]}}]},
        "version_name": sel(r["version_name"]),
        "genre": rt(r["genre"]),
        "artist": rt(r["artist"]),
        "dif_name": sel(r["dif_name"]),
        "level": {"number": to_num(r["level"])},
        "bpm": rt(r["bpm"]),
        "notes": {"number": to_num(r["notes"])},
        "dif_tier": {"number": to_num(r.get("dif_tier"))},
        "medal": sel(r["medal"]),
        "score": {"number": to_num(r["score"])},
        "best_version": sel(r["best_version"]),
        "updated_at": dt(r.get("updated_at")),
        "song_updated_at": dt(r.get("song_updated_at")),
        "play_updated_at": dt(r.get("play_updated_at")),
    }


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
            print("[경고] CSV 에 updated_at 컬럼이 없습니다. merge_playdata_single.py 를 먼저 실행하세요. (전체 갱신으로 진행)")

    mode = "FULL" if FULL_MODE else "증분"
    print(f"CSV {len(rows)}행 / 모드: {mode}{' / DRY-RUN' if DRY_RUN else ''}")

    try:
        ensure_schema()
        print("기존 페이지 조회 중...")
        existing = load_existing()
    except Exception as e:
        sys.exit(f"기존 DB 조회 실패 (integration 연결/DB ID 확인): {e}")
    print(f"기존 페이지 {len(existing)}개")

    missing, failed = set(), []
    created = updated = cover_only = skipped = 0
    seen_keys = set()

    for i, r in enumerate(rows, 1):
        key = row_key(r["version_name"], r["title"], r["dif_name"])

        # CSV 내 중복 키는 같은 페이지를 덮어쓰게 되므로 첫 행만 처리
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

        # 커버가 없는 페이지만 이미지 탐색 (이미 커버가 있으면 파일 탐색 자체를 생략)
        cover = None
        if COVER_MODE != "none" and not (ex and ex["has_cover"]):
            img = find_image(r.get("version_num", ""), r["title"])
            if img is None:
                missing.add(f'{r.get("version_num")}\t{r["title"]}')
            else:
                cover = img

        # 변경 없음 + 새로 붙일 커버도 없음 -> 완전 생략
        if unchanged and cover is None:
            skipped += 1
            continue

        try:
            page_id = ex["id"] if ex else None

            def send(fresh=False):
                body = {}
                if not unchanged:
                    body["properties"] = build_properties(r)
                if cover is not None:
                    body["cover"] = make_cover(cover, fresh)
                if page_id:
                    return api("PATCH", f"/pages/{page_id}", json=body)
                body["parent"] = {"database_id": DB_ID}
                return api("POST", "/pages", json=body)

            if not DRY_RUN:
                try:
                    send()
                except RuntimeError:
                    if COVER_MODE == "upload" and cover is not None:
                        send(fresh=True)   # 업로드 ID 재사용 실패 시 새로 업로드
                    else:
                        raise

            if page_id is None:
                created += 1
            elif unchanged:
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
    print(
        f"완료: 추가 {created}, 갱신 {updated}, 커버만 추가 {cover_only}, "
        f"변경 없음(생략) {skipped}, 실패 {len(failed)}, 이미지 없음 {len(missing)}곡 -> {MISSING_LOG.name}"
    )
    for k, msg in failed[:20]:
        print("  -", k, msg)


if __name__ == "__main__":
    main()
