"""
popn_playdata.csv 의 플레이 기록을 popn_music_list_single.csv 에 병합하여
popn_playdata_single.csv 를 생성한다.

- 곡 매칭 기준: title, genre, artist 3개가 모두 일치
- Light / Normal / Hyper / Ex 를 각각 분리하여, single 의 dif_name 행마다 해당 난이도의 값을 채운다.
- Big 은 single 에 해당하는 난이도가 없으므로 사용하지 않는다.
"""

import csv
import re
import sys
import unicodedata
from pathlib import Path

# ============================================================
# [설정] 경로 및 옵션
# ============================================================
BASE_DIR = Path(__file__).resolve().parent                       # popn_score_scraper 폴더
SINGLE_PATH = BASE_DIR.parent / "popn_homepage_scraper" / "popn_music_list_single.csv"
PLAYDATA_PATH = BASE_DIR / "popn_playdata.csv"
OUTPUT_PATH = BASE_DIR / "popn_playdata_single.csv"

# True 이면 전각/반각, 공백 차이를 무시하고 비교한다. (엄격한 완전 일치를 원하면 False)
NORMALIZE_UNICODE = True

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


def to_int(value, default=-1):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


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

        if diff is None:
            unknown_difs.add(dif_raw)
            continue

        by_diff = index.get(key)
        if by_diff is None:
            unmatched_songs.add(key)
            continue

        rec = by_diff.get(diff)
        if rec is None:
            continue

        matched_rows += 1
        # 기록이 비어있는 경우(미플레이)도 그대로 반영
        row["medal"] = rec["medal"]
        row["score"] = rec["score"]
        row["best_version"] = rec["version"]
        if rec["score"] not in ("", "0"):
            filled += 1

    # ========================================================
    # [3단계] 저장
    # ========================================================
    with open(OUTPUT_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=single_fields)
        writer.writeheader()
        writer.writerows(single_rows)

    print(f"single 전체 {len(single_rows)}행 중 매칭 {matched_rows}행 (플레이 기록 있는 행 {filled}개)")
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