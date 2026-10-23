import os
import csv
import subprocess
import difflib
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
import re
import time
import unicodedata

from curl_cffi import requests

BASE_URL = "https://p.eagate.573.jp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,ja-JP;q=0.8,ja;q=0.7,en-US;q=0.6"
}

# 2. 저장 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, "images")
CSV_FILENAME = os.path.join(BASE_DIR, "popn_music_list.csv")
CSV_SINGLE_FILENAME = os.path.join(BASE_DIR, "popn_music_list_single.csv")
WIKI_UNMATCHED_FILENAME = os.path.join(BASE_DIR, "wiki.csv")   # 공식 데이터와 연결되지 못한 위키 정보

# 3. 0~29번에 대응하는 버전 이름 리스트
VERSION_NAMES = [
    "pop'n 가정용", "pop'n music", "pop'n music 2", "pop'n music 3", "pop'n music 4",
    "pop'n music 5", "pop'n music 6", "pop'n music 7", "pop'n music 8", "pop'n music 9",
    "pop'n music 10", "pop'n music 11", "pop'n music 12 이로하", "pop'n music 13 카니발", "pop'n music 14 FEVER!",
    "pop'n music 15 ADVENTURE", "pop'n music 16 PARTY♪", "pop'n music 17 THE MOVIE", "pop'n music 18 전국구 열전", "pop'n music 19 TUNE STREET",
    "pop'n music 20 fantasia", "pop'n music Sunny Park", "pop'n music 라피스트리아", "pop'n music éclale", "pop'n music 토끼와 고양이와 소년의 꿈",
    "pop'n music peace", "pop'n music 해명 리들스", "pop'n music UniLab", "pop'n music Jam&Fizz", "pop'n music High☆Cheers!!"
]

# 어느 버전에도 속하지 않는 곡 (version=-1 전체 목록에서만 발견되는 곡)
ETC_VERSION_NUM = -1
ETC_VERSION_NAME = "기타"
ETC_IMAGE_FOLDER = "etc"

# 한 버전에서 탐색할 최대 페이지 수 (무한 루프 방지용 안전장치)
MAX_PAGES = 500

# 4. 변경 감지 설정 (곡 메타데이터 업데이트 날짜)
KST = timezone(timedelta(hours=9))
NOW_TS = datetime.now(KST).isoformat(timespec="seconds")   # 예: 2026-10-21T14:30:00+09:00

SONG_COMPARE_FIELDS = ["version_name", "level", "bpm", "notes", "dif_tier"]
WIKI_FIELDS = ["bpm", "notes", "dif_tier"]

# UPPER 표기 감지용 (공식 사이트: "곡명 (UPPER)", 위키: "곡명Ⓤ")
UPPER_MARK = "Ⓤ"
UPPER_RE = re.compile(r'[\(\[（［]\s*upper\s*[\)\]）］]', re.IGNORECASE)
UPPER_SUFFIX = "|upper"

# 위키 장르 칸 끝의 난이도 표기: 예) "스카렛3(EX)", "デステクノ(H)"
WIKI_DIFF_SUFFIX_RE = re.compile(r'\s*\((EX|H|N|E)\)\s*$', re.IGNORECASE)

# 표기 차이(글자 자체가 다른 경우) 보조 매칭 설정
#  - 장르가 정규화 후 "완전히 같을 때만" 후보가 된다. (JADAMGA / ZADAMGA 처럼 장르가 다르면 절대 합치지 않음)
#  - 위키에는 아티스트 정보가 없으므로 장르가 동일한지만으로 후보를 좁힌 뒤 곡명 유사도를 확인한다.
FUZZY_MIN_RATIO = 0.6          # 곡명 유사도 최소값
FUZZY_MIN_CONTAIN_LEN = 3      # 한쪽 곡명이 다른 쪽에 포함될 때 인정하는 최소 글자 수
FUZZY_TIE_MARGIN = 0.05        # 1,2위 후보 점수 차이가 이보다 작으면 모호한 것으로 보고 합치지 않음


def single_key(r):
    return (str(r["version_num"]), r["title"], r["genre"], r["artist"], r["dif_name"])


def load_previous_single():
    prev = {}
    if not os.path.exists(CSV_SINGLE_FILENAME):
        return prev
    try:
        with open(CSV_SINGLE_FILENAME, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    prev[single_key(r)] = r
                except KeyError:
                    continue
    except Exception as e:
        print(f"[주의] 이전 single CSV 를 읽지 못했습니다: {e}")
    return prev


def stamp_song(rec, prev_map):
    old = prev_map.get(single_key(rec))
    if old is None:
        rec["song_updated_at"] = NOW_TS
        return rec

    for k in WIKI_FIELDS:
        if str(rec.get(k, "")) == "" and (old.get(k) or "") != "":
            rec[k] = old[k]

    changed = any(str(rec.get(k, "")).strip() != (old.get(k) or "").strip() for k in SONG_COMPARE_FIELDS)
    if changed or not old.get("song_updated_at"):
        rec["song_updated_at"] = NOW_TS
    else:
        rec["song_updated_at"] = old["song_updated_at"]
    return rec


def clean_filename(filename):
    return re.sub(r'[\\/*?:"<>|]', "", filename).strip()


def strip_symbols(s):
    """
    NFKC 정규화 + 소문자화 후, 글자(L)/숫자(N)/결합문자(M)만 남기고 나머지는 모두 제거한다.
    따옴표(' ’ " “ ”), 물결표(~ 〜 ～), 괄호(전각/반각), 중점(・), 하이픈, 공백, 눈에 보이지 않는 제어문자(U+202C 등),
    ♡ ⚡ ● ‖ × = 같은 기호는 전부 사라지므로 표기 차이로 인한 불일치가 없어진다.
    기호만으로 이루어진 이름은 전부 지워지지 않도록 공백만 제거한 형태로 대신한다.
    """
    s = unicodedata.normalize('NFKC', s).lower()
    kept = "".join(ch for ch in s if unicodedata.category(ch)[0] in "LNM")
    if kept:
        return kept
    return re.sub(r'\s+', '', s)


def normalize_title(title):
    """
    공식 사이트와 위키의 곡명을 같은 형태로 맞춘다.
    - 위키의 'Ⓤ' 와 공식 사이트의 '(UPPER)' 는 모두 UPPER 패턴 표시로 보고
      본문에서 제거한 뒤 동일한 접미사(|upper)를 붙인다.
    - 주의: 'Ⓤ' 는 NFKC 정규화를 거치면 일반 'U' 로 바뀌므로
      반드시 정규화 전에 먼저 처리해야 한다.
    - 그 외 모든 기호/공백/전각·반각 차이는 strip_symbols 에서 제거한다.
    """
    is_upper = UPPER_MARK in title
    s = title.replace(UPPER_MARK, " ")

    s = unicodedata.normalize('NFKC', s).lower()

    if UPPER_RE.search(s):
        is_upper = True
        s = UPPER_RE.sub(" ", s)

    s = strip_symbols(s)
    return s + (UPPER_SUFFIX if is_upper else "")


def normalize_genre(genre):
    """
    장르명 비교용 정규화. 공식 사이트의 '(UPPER)' 와 위키의 'Ⓤ' 표기는 제거하고
    공백·기호·전각/반각 차이를 없앤다. 숫자는 남기므로 '카우보이' 와 '카우보이2' 는 서로 다른 장르로 취급된다.
    """
    g = genre.replace(UPPER_MARK, " ")
    g = unicodedata.normalize('NFKC', g).lower()
    g = UPPER_RE.sub(" ", g)
    return strip_symbols(g)


def wiki_genre_key(genre_diff_text):
    """위키의 '장르(난이도)' 칸에서 난이도 표기를 떼어내고 비교용 장르 키를 만든다."""
    diff_source = genre_diff_text.replace(UPPER_MARK, "").strip()
    return normalize_genre(WIKI_DIFF_SUFFIX_RE.sub("", diff_source))


def fetch_wiki_data(session):
    """
    popn.wiki 의 40~50레벨 표를 수집한다.
    반환:
      wiki_data    : {(정규화 제목, 난이도): [entry, ...]}  (매칭용)
      wiki_entries : 수집한 모든 위키 행 (매칭 여부 확인 및 wiki.csv 출력용)
    """
    print("\n[Wiki 정보 수집] popn.wiki에서 40~50레벨 세부 정보를 수집합니다 (로컬 직접 연결)...")
    wiki_data = {}
    wiki_entries = []

    for lv in range(40, 51):
        target_path = quote('難易度表') + f"/lv{lv}"
        url = f"https://popn.wiki/{target_path}"

        level_total_songs = 0
        level_tier_collected = 0

        try:
            response = session.get(url, headers=HEADERS, timeout=15)
            response.encoding = 'utf-8'

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                rows = soup.select('table tbody tr')

                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) < 7:
                        continue

                    genre_diff_text = cols[-6].text.strip()
                    title_text = cols[-5].text.strip()
                    bpm = cols[-4].text.strip()
                    notes = cols[-2].text.strip()
                    tier_raw = cols[-1].text.strip()

                    # UPPER 여부: 제목 칸 또는 장르/난이도 칸에 Ⓤ 가 있는 경우
                    is_upper = (UPPER_MARK in title_text) or (UPPER_MARK in genre_diff_text)

                    # Ⓤ 가 (EX) 뒤에 붙어 있어도 난이도를 읽을 수 있도록 제거 후 파싱
                    diff_source = genre_diff_text.replace(UPPER_MARK, "").strip()

                    diff_val = ""
                    diff_match = re.search(r'\((EX|H|N|E)\)\s*$', diff_source, re.IGNORECASE)
                    if diff_match:
                        d = diff_match.group(1).upper()
                        if d == 'EX': diff_val = 'ex'
                        elif d == 'H': diff_val = 'hyper'
                        elif d == 'N': diff_val = 'normal'
                        elif d == 'E': diff_val = 'light'
                    elif is_upper:
                        # UPPER 패턴은 별도 난이도 표기가 없으면 EX 계열로 취급
                        diff_val = 'ex'

                    dif_tier = ""
                    tier_match = re.search(r'\(([-+]?\d+\.?\d*)', tier_raw)
                    if tier_match:
                        dif_tier = tier_match.group(1)

                    norm_title = normalize_title(title_text)
                    if is_upper and not norm_title.endswith(UPPER_SUFFIX):
                        norm_title += UPPER_SUFFIX   # Ⓤ 가 난이도 칸에만 있던 경우 보정

                    entry = {
                        "wiki_level": lv,
                        "title": title_text,
                        "genre_diff": genre_diff_text,
                        "dif_name": diff_val,
                        "upper": "Y" if is_upper else "",
                        "bpm": bpm,
                        "notes": notes,
                        "dif_tier": dif_tier,
                        "norm_title": norm_title,
                        "norm_genre": wiki_genre_key(genre_diff_text),   # 표기차이 보조 매칭용 (wiki.csv 에는 저장되지 않음)
                        "matched": False,
                    }

                    if norm_title:
                        wiki_entries.append(entry)

                    if norm_title and diff_val:
                        level_total_songs += 1
                        if dif_tier:
                            level_tier_collected += 1
                        wiki_data.setdefault((norm_title, diff_val), []).append(entry)

                print(f" - Wiki Lv{lv} 수집 결과 : 총 {level_total_songs}곡 (난이도 지수 {level_tier_collected}개)")
            else:
                print(f"   (Lv{lv} 시도 실패: HTTP {response.status_code})")

        except Exception as e:
            print(f"   (Lv{lv} 연결 에러 발생: {e})")

        time.sleep(1)  # 과부하 방지

    print(f"위키 데이터 수집 완료. (총 {len(wiki_data)}개의 패턴 데이터 확보)\n")
    return wiki_data, wiki_entries


def title_similarity(norm_a, norm_b):
    """정규화된 두 곡명의 유사도(0~1). 한쪽이 다른 쪽에 포함되면(예: 'Pink Rose' / 'Pink Rose(from ...)') 높게 본다."""
    a = norm_a[:-len(UPPER_SUFFIX)] if norm_a.endswith(UPPER_SUFFIX) else norm_a
    b = norm_b[:-len(UPPER_SUFFIX)] if norm_b.endswith(UPPER_SUFFIX) else norm_b
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= FUZZY_MIN_CONTAIN_LEN and short in long_:
        ratio = max(ratio, 0.9)
    return ratio


def link_wiki_aliases(wiki_data, wiki_entries, official_songs):
    """
    특수문자 정규화로도 연결되지 않은 위키 곡(예: 'redrew lots' / 'redraw lots')을
    공식 곡에 연결한다. 조건은 다음과 같다.
      1) 공식 곡 중 곡명(정규화)이 같은 곡이 없는 위키 행만 대상으로 한다.
      2) 장르(정규화)가 공식 곡과 완전히 같아야 한다. 한 글자라도 다르면 후보에서 제외한다.
         (예: JADAMGA = ゴシックナイトメア2 / ZADAMGA = ゴシックナイトメア → 합치지 않음)
      3) UPPER 여부가 같아야 한다.
      4) 곡명 유사도가 기준 이상이고, 1위 후보가 뚜렷해야 한다.
    연결되면 wiki_data 에 공식 곡의 정규화 제목으로 별칭 키를 추가하므로 이후 lookup_wiki 가 그대로 동작한다.
    """
    official_titles = set()
    by_genre = defaultdict(set)   # (정규화 장르, UPPER 여부) -> {정규화 곡명}
    for s in official_songs:
        nt = normalize_title(s["title"])
        official_titles.add(nt)
        by_genre[(normalize_genre(s["genre"]), nt.endswith(UPPER_SUFFIX))].add(nt)

    linked = 0
    skipped = []
    for e in wiki_entries:
        if e["norm_title"] in official_titles or not e["dif_name"]:
            continue
        genre_key = e.get("norm_genre", "")
        if not genre_key:
            continue

        candidates = by_genre.get((genre_key, e["upper"] == "Y"), set())
        scored = sorted(((title_similarity(e["norm_title"], c), c) for c in candidates), reverse=True)
        scored = [x for x in scored if x[0] >= FUZZY_MIN_RATIO]
        if not scored:
            continue

        if len(scored) > 1 and scored[0][0] - scored[1][0] < FUZZY_TIE_MARGIN:
            skipped.append(e["title"])
            continue

        best = scored[0][1]
        wiki_data.setdefault((best, e["dif_name"]), []).append(e)
        e["alias_of"] = best
        linked += 1
        print(f"   [표기차이 연결] wiki '{e['title']}' ({e['dif_name']}) → 공식 정규화 제목 '{best}' (유사도 {scored[0][0]:.2f})")

    for t in skipped:
        print(f"   [표기차이 보류] '{t}' : 같은 장르에서 후보가 모호하여 연결하지 않았습니다.")
    print(f"[표기차이 보조 매칭] {linked}개 위키 행을 공식 곡에 추가 연결했습니다.")


def lookup_wiki(wiki_data, norm_title, dif_name, genre_key=None):
    """
    위키 정보를 찾고, 찾은 행은 matched 로 표시한다.
    genre_key 가 주어지면(정규화 제목이 같은 서로 다른 곡이 있는 경우) 장르가 같은 위키 행만 사용한다.
    """
    entries = wiki_data.get((norm_title, dif_name))
    if entries and genre_key is not None:
        entries = [e for e in entries if e.get("norm_genre") == genre_key]
    if not entries:
        return {}
    for e in entries:
        e["matched"] = True
    return entries[0]


def save_wiki_unmatched(wiki_entries):
    """공식 데이터와 연결되지 못한 위키 정보를 wiki.csv 로 저장한다."""
    unmatched = [e for e in wiki_entries if not e["matched"]]

    headers = ["wiki_level", "title", "genre_diff", "dif_name", "upper",
               "bpm", "notes", "dif_tier", "norm_title"]
    with open(WIKI_UNMATCHED_FILENAME, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(unmatched)

    print(f"[Wiki 매칭 결과] 전체 {len(wiki_entries)}행 중 {len(unmatched)}행이 매칭되지 않아 "
          f"{os.path.basename(WIKI_UNMATCHED_FILENAME)} 에 저장했습니다.")


def download_image(session, img_url, version_num, file_name):
    folder_name = ETC_IMAGE_FOLDER if version_num == ETC_VERSION_NUM else str(version_num)
    folder_path = os.path.join(IMAGES_DIR, folder_name)
    if not os.path.exists(folder_path):
        os.makedirs(folder_path, exist_ok=True)

    safe_file_name = clean_filename(file_name) + ".png"
    file_path = os.path.join(folder_path, safe_file_name)

    if not os.path.exists(file_path):
        try:
            img_data = session.get(img_url, headers=HEADERS).content
            with open(file_path, 'wb') as handler:
                handler.write(img_data)
        except Exception:
            pass


def fetch_music_page(session, version, page):
    """
    공식 홈페이지의 곡 목록 한 페이지를 읽어 곡 정보 리스트를 반환한다.
    - 요청 실패(HTTP 200 아님): None
    - 곡이 없는 페이지: 빈 리스트
    """
    url = (f"https://p.eagate.573.jp/game/popn/popn29/music/list.html"
           f"?page={page}&version={version}&lv=0&bemani=0&category=0&keyword=&sort=music&sort_type=up")
    response = session.get(url, headers=HEADERS)

    if response.status_code != 200:
        return None

    try:
        soup = BeautifulSoup(response.content.decode('utf-8', 'ignore'), 'lxml')
    except Exception:
        soup = BeautifulSoup(response.content.decode('utf-8', 'ignore'), 'html.parser')

    ul_tables = soup.find_all('ul', class_='mu_list_table')
    target_ul = next((ul for ul in ul_tables if 'mu_head' not in ul.get('class', [])), None)

    if not target_ul:
        return []

    list_items = target_ul.find_all('li', recursive=False)
    songs = []

    for i in range(0, len(list_items), 3):
        if i + 2 >= len(list_items):
            break

        try:
            li_img, li_info, li_level = list_items[i], list_items[i + 1], list_items[i + 2]

            img_tag = li_img.find('img')
            img_url = urljoin(BASE_URL, img_tag['src']) if img_tag and 'src' in img_tag.attrs else ""

            info_ps = li_info.find_all('p')
            genre = info_ps[0].text.strip() if len(info_ps) > 0 else "Unknown Genre"
            title = info_ps[1].text.strip() if len(info_ps) > 1 else "Unknown Title"
            artist = info_ps[2].text.strip() if len(info_ps) > 2 else "Unknown Artist"

            level_ps = li_level.find_all('p')
            songs.append({
                "img_url": img_url,
                "genre": genre,
                "title": title,
                "artist": artist,
                "light": level_ps[0].get('data-d', '-') if len(level_ps) > 0 else "-",
                "normal": level_ps[1].get('data-d', '-') if len(level_ps) > 1 else "-",
                "hyper": level_ps[2].get('data-d', '-') if len(level_ps) > 2 else "-",
                "ex": level_ps[3].get('data-d', '-') if len(level_ps) > 3 else "-",
            })
        except Exception:
            continue

    return songs


def scrape_version(session, version, label, on_song):
    """한 버전(또는 -1 전체 목록)의 모든 페이지를 순회하며 곡마다 on_song 을 호출한다."""
    page = 0
    prev_signature = None

    while page < MAX_PAGES:
        songs = fetch_music_page(session, version, page)

        if songs is None:
            break

        print(f"▶ 탐색 중: Version {version} ({label}), Page {page} Songs {len(songs)}")

        if not songs:
            print(f"→ 해당 버전의 마지막 페이지에 도달했습니다. (Version {version} 완료)\n")
            break

        # 범위를 넘어선 페이지가 직전 페이지를 그대로 반복해서 돌려주는 경우 종료
        signature = tuple((s["title"], s["genre"], s["artist"]) for s in songs)
        if signature == prev_signature:
            print(f"→ 이전 페이지와 동일한 내용이 반복되어 종료합니다. (Version {version} 완료)\n")
            break
        prev_signature = signature

        for song in songs:
            on_song(song)

        page += 1
        time.sleep(0.5)


def build_single_records(official_entries, wiki_data, prev_single):
    """수집이 모두 끝난 공식 곡 목록을 난이도별 레코드로 변환하면서 위키 정보를 붙인다."""
    # 정규화하면 곡명이 같아지는 서로 다른 곡(예: 'memories' / 'memories...')은 장르까지 같아야 위키 정보를 붙인다.
    genres_by_title = defaultdict(set)
    for song, _, _ in official_entries:
        genres_by_title[normalize_title(song["title"])].add(normalize_genre(song["genre"]))

    records = []
    for song, version_num, version_name in official_entries:
        norm_t = normalize_title(song["title"])
        genre_key = normalize_genre(song["genre"]) if len(genres_by_title[norm_t]) > 1 else None
        for dif_name in ("light", "normal", "hyper", "ex"):
            level = song[dif_name]
            if level == "-":
                continue
            w_data = lookup_wiki(wiki_data, norm_t, dif_name, genre_key)
            rec = {
                "version_num": version_num, "version_name": version_name,
                "genre": song["genre"], "title": song["title"], "artist": song["artist"],
                "dif_name": dif_name, "level": level,
                "bpm": w_data.get("bpm", ""), "notes": w_data.get("notes", ""),
                "dif_tier": w_data.get("dif_tier", "")
            }
            records.append(stamp_song(rec, prev_single))
    return records


def push_to_github():
    print("\n[GitHub 업로드] 변경된 파일들을 GitHub에 푸시합니다...")
    try:
        # 변경사항 확인
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=BASE_DIR)
        if not status.stdout.strip():
            print(" -> 변경된 파일이 없습니다. 푸시를 생략합니다.")
            return

        # git add, commit, push 실행
        subprocess.run(["git", "add", "."], cwd=BASE_DIR, check=True)
        commit_message = f"Auto-update: pop'n music data ({datetime.now(KST).strftime('%Y-%m-%d %H:%M')})"
        subprocess.run(["git", "commit", "-m", commit_message], cwd=BASE_DIR, check=True)
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)

        print(f" -> GitHub 업로드 완료! (커밋 메시지: {commit_message})")
    except subprocess.CalledProcessError as e:
        print(f" -> [오류] Git 명령 실행 중 에러가 발생했습니다: {e}")
    except FileNotFoundError:
        print(" -> [오류] 시스템에 'git' 명령어가 설치되어 있지 않거나 경로에 없습니다.")


def main():
    music_data_list = []
    official_entries = []   # (song, version_num, version_name) : 위키 매칭은 전체 수집이 끝난 뒤에 수행

    prev_single = load_previous_single()
    print(f"[변경 감지] 이전 single 데이터 {len(prev_single)}행 로드")

    session = requests.Session(impersonate="chrome120")

    print("[환경 설정] 로컬(PC) 환경: 로그인 쿠키 연동을 시도합니다...")
    cookie_loaded = False
    cookie_path = os.path.join(BASE_DIR, "cookie.txt")

    if os.path.exists(cookie_path):
        with open(cookie_path, "r", encoding="utf-8") as f:
            raw_cookie = f.read().strip()
            if raw_cookie:
                cookie_dict = {}
                for item in raw_cookie.split(';'):
                    if '=' in item:
                        k, v = item.strip().split('=', 1)
                        cookie_dict[k] = v
                session.cookies.update(cookie_dict)
                print(" -> cookie.txt 파일에서 쿠키 연동 성공!")
                cookie_loaded = True

    if not cookie_loaded:
        try:
            import browser_cookie3
            cj = browser_cookie3.chrome(domain_name='eagate.573.jp')
            cookie_dict = {c.name: c.value for c in cj}
            if cookie_dict:
                session.cookies.update(cookie_dict)
                print(" -> 브라우저 자동 쿠키 연동 성공!")
            else:
                print(" -> [주의] 로그인 쿠키를 찾지 못했습니다.")
        except Exception:
            print(" -> [주의] cookie.txt 파일이 없고, 브라우저 쿠키도 읽지 못했습니다. 비로그인 상태로 진행합니다.")

    # 1. 위키 데이터 선행 수집
    wiki_data, wiki_entries = fetch_wiki_data(session)

    print(f"\n[{BASE_DIR}] 공식 홈페이지 정보 수집을 시작합니다...\n")

    # 버전별 탐색에서 이미 수집한 곡 (version=-1 탐색 시 중복 방지용)
    seen_songs = set()

    def register_song(song, version_num, version_name):
        """곡 한 개를 통합 CSV 리스트에 등록하고, 난이도별 레코드는 나중에 만들 수 있도록 보관한다."""
        if song["img_url"]:
            download_image(session, song["img_url"], version_num, song["title"])

        music_data_list.append({
            "Version_Num": version_num, "Version_Name": version_name,
            "Genre": song["genre"], "Title": song["title"], "Artist": song["artist"],
            "Light": song["light"], "Normal": song["normal"],
            "Hyper": song["hyper"], "EX": song["ex"]
        })
        official_entries.append((song, version_num, version_name))

    # 2. 공식 홈페이지 파싱 (버전 0~29)
    for version in range(30):
        current_version_name = VERSION_NAMES[version]

        def on_version_song(song, _v=version, _n=current_version_name):
            seen_songs.add((song["title"], song["genre"], song["artist"]))
            register_song(song, _v, _n)

        scrape_version(session, version, current_version_name, on_version_song)

    # 3. 어느 버전에도 속하지 않는 곡 탐색 (version=-1 전체 목록)
    print("[기타 곡 탐색] version=-1 전체 목록에서 어느 버전에도 속하지 않는 곡을 찾습니다...\n")
    etc_count = 0

    def on_etc_song(song):
        nonlocal etc_count
        key = (song["title"], song["genre"], song["artist"])
        if key in seen_songs:
            return
        seen_songs.add(key)
        etc_count += 1
        print(f"   + 기타 곡 추가: {song['title']}")
        register_song(song, ETC_VERSION_NUM, ETC_VERSION_NAME)

    scrape_version(session, -1, "전체 목록", on_etc_song)
    print(f"[기타 곡 탐색 결과] 어느 버전에도 속하지 않는 곡 {etc_count}곡을 '{ETC_VERSION_NAME}' 로 추가했습니다.\n")

    # 4. 위키 정보 연결
    #    (1) 정규화 제목이 같은 곡은 lookup_wiki 에서 바로 연결된다. (특수문자 차이는 normalize_title 이 흡수)
    #    (2) 글자 자체가 다른 곡은 장르가 완전히 같을 때만 보조 매칭으로 연결한다.
    print("[Wiki 연결] 표기 차이가 있는 곡을 장르 기준으로 보조 매칭합니다...")
    link_wiki_aliases(wiki_data, wiki_entries, [e[0] for e in official_entries])
    music_data_single_list = build_single_records(official_entries, wiki_data, prev_single)

    # 5. 파일 저장
    csv_headers = ["Version_Num", "Version_Name", "Genre", "Title", "Artist", "Light", "Normal", "Hyper", "EX"]
    with open(CSV_FILENAME, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers)
        writer.writeheader()
        writer.writerows(music_data_list)

    single_headers = ["version_num", "version_name", "genre", "title", "artist", "dif_name", "level", "bpm", "notes", "dif_tier", "song_updated_at"]
    with open(CSV_SINGLE_FILENAME, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=single_headers)
        writer.writeheader()
        writer.writerows(music_data_single_list)

    # 6. 매칭되지 못한 위키 정보 저장 (모든 곡 수집이 끝난 뒤에 판정)
    save_wiki_unmatched(wiki_entries)

    print("\n모든 데이터 저장 완료.")

    # 7. GitHub 자동 업로드
    push_to_github()


if __name__ == "__main__":
    main()