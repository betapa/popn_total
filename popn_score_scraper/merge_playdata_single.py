"""
popn_playdata.csv 의 플레이 기록을 popn_music_list_single.csv 에 병합하여
popn_playdata_single.csv 를 생성한다.

- 곡 매칭 기준: title, genre, artist 3개가 모두 일치
- Light / Normal / Hyper / Ex 를 각각 분리하여, single 의 dif_name 행마다 해당 난이도의 값을 채운다.
- Big 은 single 에 해당하는 난이도가 없으므로 사용하지 않는다.

[변경 감지]
- 이전 실행 결과(popn_playdata_single.csv)와 비교하여
    play_updated_at : medal / score / best_version 이 바뀌었거나 신규 행이면 현재 시각, 아니면 이전 값 유지
    song_updated_at : 곡 메타데이터 변경 시각 (homepage_scraper.py 가 기록)
    updated_at      : 위 두 값 중 더 최근 시각 -> upload_to_notion.py 가 이 값으로 변경 여부를 판단
"""

import csv
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ============================================================
# [설정] 경로 및 옵션
# ============================================================
BASE_DIR = Path(__file__).resolve().parent                       # popn_score_scraper 폴더
SINGLE_PATH = BASE_DIR.parent / "popn_homepage_scraper" / "popn_music_list_single.csv"
PLAYDATA_PATH = BASE_DIR / "popn_playdata.csv"
OUTPUT_DIR = BASE_DIR.parent / "notion"
OUTPUT_PATH = OUTPUT_DIR / "popn_playdata_single.csv"

NORMALIZE_UNICODE = True

KST = timezone(timedelta(hours=9))
NOW_TS = datetime.now(KST).isoformat(timespec="seconds")   # 예: 2026-10-21T14:30:00+09:00

PLAY_FIELDS = ("medal", "score", "best_version")

# single 의 dif_name -> playdata 의 난이도 접두어
# dif_name 이 아래에 없으면 경고가 출력되므로, 실제 값에 맞게 추가하세요.
DIF_ALIASES = {
    "light": "Light", "easy": "Light", "l": "Light", "e": "Light", "라이트": "Light", "イージー": "Light",
    "normal": "Normal", "n": "Normal", "노말": "Normal", "ノーマル": "Normal",
    "hyper": "Hyper", "h": "Hyper", "하이퍼": "Hyper", "ハイパー": "Hyper",
    "ex": "Ex", "ex.": "Ex", "x": "Ex", "엑스트라": "Ex", "extra": "Ex", "エキスパート": "Ex",
}


# ============================================================
# [유틸]
# ============================================================
def norm(text):
    """곡 비교용 문자열 정규화"""
    s = (text or "").strip()
    if NORMALIZE_UNICODE:
        s = unicodedata.normalize("NFKC", s)
        s = re.sub(r"\s+", " ", s)
    return s


def song_key(title, genre, artist):
    return (norm(title), norm(genre), norm(artist))


def row_key(row):
    """이전 결과와의 행 단위 비교 키 (곡 + 난이도)"""
    return (
        (row.get("version_num") or "").strip(),
        norm(row.get("title")),
        norm(row.get("genre")),
        norm(row.get("artist")),
        (row.get("dif_name") or "").strip().lower(),
    )


def to_int(value, default=-1):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def parse_ts(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return d


def latest_ts(*values):
    """여러 타임스탬프 문자열 중 가장 최근 값을 반환 (모두 없으면 빈 문자열)"""
    best_raw, best_dt = "", None
    for v in values:
        d = parse_ts(v)
        if d is not None and (best_dt is None or d > best_dt):
            best_raw, best_dt = v.strip(), d
    return best_raw


def load_previous_output():
    """이전 실행의 popn_playdata_single.csv -> {행 키: 행}"""
    if not OUTPUT_PATH.exists():
        return {}
    try:
        _, rows = read_csv(OUTPUT_PATH)
    except Exception as e:
        print(f"[주의] 이전 결과를 읽지 못했습니다. 전체를 신규로 처리합니다: {e}")
        return {}
    return {row_key(r): r for r in rows}


# ============================================================
# [1단계] playdata 읽어서 (곡 키, 난이도) -> 기록 사전 구성
# ============================================================
def build_playdata_index(rows):
    index = {}  # {(title, genre, artist): {"Light": {...}, "Normal": {...}, ...}}
    dup_count = 0
    for r in rows:
        key = song_key(r.get("Title"), r.get("Genre"), r.get("Artist"))
        by_diff = index.setdefault(key, {})
        for diff in ("Light", "Normal", "Hyper", "Ex"):
            rec = {
                "medal": (r.get(f"{diff}_Medal") or "").strip(),
                "score": (r.get(f"{diff}_Score") or "").strip(),
                "version": (r.get(f"{diff}_Version") or "").strip(),
            }
            old = by_diff.get(diff)
            if old is None:
                by_diff[diff] = rec
            else:
                # 같은 곡이 중복된 경우: 점수가 더 높은 기록을 채택
                dup_count += 1
                if to_int(rec["score"]) > to_int(old["score"]):
                    by_diff[diff] = rec
    return index, dup_count


# ============================================================
# [2단계] single 의 각 행에 해당 난이도 기록 채우기
# ============================================================
def main():
    for p in (SINGLE_PATH, PLAYDATA_PATH):
        if not p.exists():
            sys.exit(f"파일을 찾을 수 없습니다: {p}")

    single_fields, single_rows = read_csv(SINGLE_PATH)
    _, play_rows = read_csv(PLAYDATA_PATH)
    prev_map = load_previous_output()
    print(f"이전 결과: {len(prev_map)}행 (변경 감지 기준)")

    for field in ("medal", "score", "best_version", "song_updated_at", "play_updated_at", "updated_at"):
        if field not in single_fields:
            single_fields.append(field)

    index, dup_count = build_playdata_index(play_rows)
    print(f"playdata: {len(play_rows)}행 -> 고유 곡 {len(index)}개 (중복 병합 {dup_count}건)")

    matched_rows = 0
    unmatched_songs = set()
    unknown_difs = set()
    filled = 0

    for row in single_rows:
        key = song_key(row.get("title"), row.get("genre"), row.get("artist"))
        dif_raw = (row.get("dif_name") or "").strip()
        diff = DIF_ALIASES.get(dif_raw.lower())

        # 기본값 (미매칭 행도 컬럼이 비지 않도록)
        for f in PLAY_FIELDS:
            row.setdefault(f, "")

        if diff is None:
            unknown_difs.add(dif_raw)
        else:
            by_diff = index.get(key)
            if by_diff is None:
                unmatched_songs.add(key)
            else:
                rec = by_diff.get(diff)
                if rec is not None:
                    matched_rows += 1
                    # 기록이 비어있는 경우(미플레이)도 그대로 반영
                    row["medal"] = rec["medal"]
                    row["score"] = rec["score"]
                    row["best_version"] = rec["version"]
                    if rec["score"] not in ("", "0"):
                        filled += 1

    # ========================================================
    # [변경 감지] 이전 결과와 비교하여 타임스탬프 부여
    # ========================================================
    new_rows = play_changed = unchanged = 0
    for row in single_rows:
        prev = prev_map.get(row_key(row))

        # 곡 메타데이터 갱신 시각 (스크레이퍼가 기록, 없으면 이전 값 -> 현재 시각 순)
        song_ts = (row.get("song_updated_at") or "").strip() \
            or (prev.get("song_updated_at") if prev else "") or NOW_TS
        row["song_updated_at"] = song_ts

        # 플레이 기록 갱신 시각
        if prev is None:
            play_ts = NOW_TS
            new_rows += 1
        else:
            same = all((row.get(f) or "").strip() == (prev.get(f) or "").strip() for f in PLAY_FIELDS)
            prev_play_ts = (prev.get("play_updated_at") or "").strip()
            if same and prev_play_ts:
                play_ts = prev_play_ts
                unchanged += 1
            else:
                play_ts = NOW_TS
                play_changed += 1
        row["play_updated_at"] = play_ts

        row["updated_at"] = latest_ts(song_ts, play_ts) or NOW_TS

    # ========================================================
    # [3단계] 저장
    # ========================================================
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=single_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(single_rows)

    print(f"single 전체 {len(single_rows)}행 중 매칭 {matched_rows}행 (플레이 기록 있는 행 {filled}개)")
    print(f"[변경 감지] 신규 {new_rows}행 / 플레이 기록 변경 {play_changed}행 / 변경 없음 {unchanged}행")
    print(f"playdata 에서 못 찾은 곡: {len(unmatched_songs)}곡")
    if unmatched_songs:
        for k in sorted(unmatched_songs)[:20]:
            print("  -", k)
        if len(unmatched_songs) > 20:
            print(f"  ... 외 {len(unmatched_songs) - 20}곡")
    if unknown_difs:
        print("[경고] DIF_ALIASES 에 없는 dif_name 값:", sorted(unknown_difs))
    print(f"저장 완료: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
