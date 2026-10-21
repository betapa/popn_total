import os
import csv
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
import re
import time
import unicodedata

# Cloudflare 우회를 위해 기본 requests 대신 curl_cffi의 requests 사용
from curl_cffi import requests

# 1. 크롤링할 대상 기본 URL 및 헤더 설정
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

# 3. 0~29번에 대응하는 버전 이름 리스트
VERSION_NAMES = [
    "pop'n 가정용", "pop'n music", "pop'n music 2", "pop'n music 3", "pop'n music 4",
    "pop'n music 5", "pop'n music 6", "pop'n music 7", "pop'n music 8", "pop'n music 9",
    "pop'n music 10", "pop'n music 11", "pop'n music 12 이로하", "pop'n music 13 카니발", "pop'n music 14 FEVER!",
    "pop'n music 15 ADVENTURE", "pop'n music 16 PARTY♪", "pop'n music 17 THE MOVIE", "pop'n music 18 전국구 열전", "pop'n music 19 TUNE STREET",
    "pop'n music 20 fantasia", "pop'n music Sunny Park", "pop'n music 라피스트리아", "pop'n music éclale", "pop'n music 토끼와 고양이와 소년의 꿈",
    "pop'n music peace", "pop'n music 해명 리들스", "pop'n music UniLab", "pop'n music Jam&Fizz", "pop'n music High☆Cheers!!"
]

# 4. 변경 감지 설정 (곡 메타데이터 업데이트 날짜)
KST = timezone(timedelta(hours=9))
NOW_TS = datetime.now(KST).isoformat(timespec="seconds")   # 예: 2026-10-21T14:30:00+09:00

# 곡 키(version_num, title, genre, artist, dif_name)가 같을 때, 아래 값이 달라지면 song_updated_at 갱신
SONG_COMPARE_FIELDS = ["version_name", "level", "bpm", "notes", "dif_tier"]
# 위키 수집 실패(프록시 차단 등) 시 이전 값을 유지할 필드
WIKI_FIELDS = ["bpm", "notes", "dif_tier"]


def single_key(r):
    return (str(r["version_num"]), r["title"], r["genre"], r["artist"], r["dif_name"])


def load_previous_single():
    """이전 실행에서 저장된 single CSV 를 {키: 행} 으로 로드 (없으면 빈 사전)"""
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
        print(f"[주의] 이전 single CSV 를 읽지 못했습니다. 전체를 신규로 처리합니다: {e}")
    return prev


def stamp_song(rec, prev_map):
    """신규/변경 곡이면 song_updated_at = 현재, 변경 없으면 이전 값을 유지"""
    old = prev_map.get(single_key(rec))
    if old is None:
        rec["song_updated_at"] = NOW_TS          # 신규 곡
        return rec

    # 위키 값이 이번에 비어 있고 이전에 있었다면 이전 값 유지 (일시적 수집 실패로 인한 허위 변경 방지)
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
    """파일명에 사용할 수 없는 특수문자 제거"""
    return re.sub(r'[\\/*?:"<>|]', "", filename).strip()

def normalize_title(title):
    """공식 사이트와 위키 간의 제목 매칭을 위한 문자열 정규화 함수"""
    s = unicodedata.normalize('NFKC', title).lower()
    s = s.replace("Ⓤ", "(upper)")
    s = re.sub(r'[\s\~\-\.\,\!\?\'\"☆♡♪]', '', s)
    return s

def fetch_wiki_data(session):
    """popn.wiki에서 40~50 레벨 데이터를 수집하여 딕셔너리로 반환"""
    print("\n[Wiki 정보 수집] popn.wiki에서 40~50레벨 세부 정보(BPM, Notes, Tier)를 수집합니다...")
    wiki_data = {}
    
    for lv in range(40, 51):
        target_path = quote('難易度表') + f"/lv{lv}"
        
        # 1순위: 다이렉트 URL / 2순위: 구글 번역기 프록시 우회 URL (데이터센터 IP 차단 우회용)
        urls_to_try = [
            f"https://popn.wiki/{target_path}",
            f"https://popn-wiki.translate.goog/{target_path}?_x_tr_sl=ja&_x_tr_tl=ja&_x_tr_hl=ko&_x_tr_pto=wapp"
        ]
        
        level_total_songs = 0
        level_tier_collected = 0
        success = False
        
        for url in urls_to_try:
            try:
                response = session.get(url, headers=HEADERS, timeout=15)
                response.encoding = 'utf-8'
                
                if response.status_code == 200:
                    success = True
                    soup = BeautifulSoup(response.text, 'html.parser')
                    rows = soup.select('table tbody tr')
                    
                    for row in rows:
                        cols = row.find_all('td')
                        if len(cols) < 7:
                            continue
                        
                        # 뒤에서부터 접근 (구글 번역기가 삽입하는 태그 변동 대응)
                        genre_diff_text = cols[-6].text.strip()
                        title_text = cols[-5].text.strip()
                        bpm = cols[-4].text.strip()
                        notes = cols[-2].text.strip()
                        tier_raw = cols[-1].text.strip()
                    
                        diff_val = ""
                        diff_match = re.search(r'\((EX|H|N|E)\)$', genre_diff_text, re.IGNORECASE)
                        if diff_match:
                            d = diff_match.group(1).upper()
                            if d == 'EX': diff_val = 'ex'
                            elif d == 'H': diff_val = 'hyper'
                            elif d == 'N': diff_val = 'normal'
                            elif d == 'E': diff_val = 'light'  
                        
                        dif_tier = ""
                        tier_match = re.search(r'\(([-+]?\d+\.?\d*)', tier_raw)
                        if tier_match:
                            dif_tier = tier_match.group(1)
                        
                        norm_title = normalize_title(title_text)
                        if norm_title and diff_val:
                            level_total_songs += 1
                            if dif_tier:
                                level_tier_collected += 1
                                
                            wiki_data[(norm_title, diff_val)] = {
                                "bpm": bpm,
                                "notes": notes,
                                "dif_tier": dif_tier
                            }
                    
                    print(f" - Wiki Lv{lv} 수집 결과 : 총 {level_total_songs}곡 중 난이도 지수 {level_tier_collected}개 추출 완료")
                    break # 성공 시 다음 URL은 시도하지 않음
                else:
                    print(f"   (Lv{lv} 시도 실패: {response.status_code})")
                    
            except Exception as e:
                print(f"   (Lv{lv} 연결 에러 발생: {e})")
                
        if not success:
            print(f" [!] Lv{lv} 페이지는 프록시 우회로도 로드할 수 없었습니다.")
            
        time.sleep(1) # 과부하 방지
            
    print(f"위키 데이터 수집 완료. (총 {len(wiki_data)}개의 패턴 데이터 확보)\n")
    return wiki_data

def download_image(session, img_url, version_num, file_name):
    """버전별 숫자 폴더(0~29)를 생성하고 이미지를 다운로드하는 함수"""
    folder_path = os.path.join(IMAGES_DIR, str(version_num))
    
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
            pass # 로그 간소화를 위해 에러 무시

def main():
    music_data_list = []
    music_data_single_list = []

    # 이전 실행 결과 (변경 감지용)
    prev_single = load_previous_single()
    print(f"[변경 감지] 이전 single 데이터 {len(prev_single)}행 로드")
    
    # Chrome 120 버전의 지문(Fingerprint)으로 완전 위장
    session = requests.Session(impersonate="chrome120")
    
# --- [환경 구분 및 쿠키 로드 로직 추가] ---
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("[환경 설정] GitHub Actions 환경: 비로그인(Guest) 상태로 공식 사이트를 탐색합니다.")
    else:
        print("[환경 설정] 로컬(PC) 환경: 로그인 쿠키 연동을 시도합니다...")
        
        cookie_loaded = False
        
        # 1순위: cookie.txt 파일에서 직접 읽기 (크롬 보안 업데이트 우회용, 가장 확실함)
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
                    print(" -> cookie.txt 파일에서 쿠키 수동 연동 성공! (해금 곡 포함 전체 곡 데이터를 수집합니다)")
                    cookie_loaded = True
        
        # 2순위: cookie.txt가 없을 경우 browser_cookie3 시도
        if not cookie_loaded:
            try:
                import browser_cookie3
                # 모든 브라우저 대신 chrome만 명시적으로 타겟팅하여 권한 충돌 최소화
                cj = browser_cookie3.chrome(domain_name='eagate.573.jp')
                cookie_dict = {c.name: c.value for c in cj}
                if cookie_dict:
                    session.cookies.update(cookie_dict)
                    print(" -> 브라우저 자동 쿠키 연동 성공! (해금 곡 포함 전체 곡 데이터를 수집합니다)")
                else:
                    print(" -> [주의] eagate.573.jp 로그인 쿠키를 찾지 못했습니다.")
            except ImportError:
                print(" -> [주의] cookie.txt 파일이 없고, browser-cookie3 라이브러리도 없습니다.")
            except Exception as e:
                print(f" -> [주의] 자동 쿠키 연동 실패: {e}")
                print(" -> 해결책: 스크립트와 같은 폴더에 'cookie.txt'를 만들고 브라우저의 cookie 값을 직접 복사해 넣으세요.")
    # ------------------------------------------
    
    # 1. 위키 데이터 선행 수집
    wiki_data = fetch_wiki_data(session)

    print(f"\n[{BASE_DIR}] 위치에 공식 홈페이지 정보 수집 및 저장을 시작합니다...\n")

    # 2. 공식 홈페이지 버전 0부터 29까지 탐색 루프
    for version in range(30):
        page = 0
        current_version_name = VERSION_NAMES[version]
        
        while True:
            url = f"https://p.eagate.573.jp/game/popn/popn29/music/list.html?page={page}&version={version}&lv=0&bemani=0&category=0&keyword=&sort=music&sort_type=up"
            
            response = session.get(url, headers=HEADERS)
            
            if response.status_code != 200:
                print(f"페이지 로드 실패 (Status: {response.status_code}): {url}")
                break
                
            # lxml 파서 사용 강제 (HTML 구조 깨짐 보완)
            try:
                soup = BeautifulSoup(response.content.decode('utf-8', 'ignore'), 'lxml')
            except Exception:
                soup = BeautifulSoup(response.content.decode('utf-8', 'ignore'), 'html.parser')
            
            ul_tables = soup.find_all('ul', class_='mu_list_table')
            target_ul = None
            for ul in ul_tables:
                if 'mu_head' not in ul.get('class', []):
                    target_ul = ul
                    break
                    
            if not target_ul:
                print(f"▶ 탐색 중: Version {version} ({current_version_name}), Page {page} Songs 0")
                print("곡 리스트를 찾을 수 없습니다. 다음 버전으로 넘어갑니다.")
                break
                
            list_items = target_ul.find_all('li', recursive=False)
            
            # 곡 수를 먼저 계산
            song_count = len(list_items) // 3
            
            # 요청하신 포맷으로 출력
            print(f"▶ 탐색 중: Version {version} ({current_version_name}), Page {page} Songs {song_count}")
            
            # 곡이 없으면 다음 버전으로 이동 (조기 종료 방지를 위한 메인 탈출 조건)
            if song_count == 0:
                print(f"→ 해당 버전의 마지막 페이지에 도달했습니다. (Version {version} 완료)\n")
                break
            
            # 3. 공식 데이터 파싱
            for i in range(0, len(list_items), 3):
                if i + 2 >= len(list_items):
                    break
                    
                try:
                    li_img = list_items[i]
                    li_info = list_items[i+1]
                    li_level = list_items[i+2]
                    
                    img_tag = li_img.find('img')
                    img_url = urljoin(BASE_URL, img_tag['src']) if img_tag and 'src' in img_tag.attrs else ""
                    
                    info_ps = li_info.find_all('p')
                    genre = info_ps[0].text.strip() if len(info_ps) > 0 else "Unknown Genre"
                    title = info_ps[1].text.strip() if len(info_ps) > 1 else "Unknown Title"
                    artist = info_ps[2].text.strip() if len(info_ps) > 2 else "Unknown Artist"
                    
                    level_ps = li_level.find_all('p')
                    level_light = level_ps[0].get('data-d', '-') if len(level_ps) > 0 else "-"
                    level_normal = level_ps[1].get('data-d', '-') if len(level_ps) > 1 else "-"
                    level_hyper = level_ps[2].get('data-d', '-') if len(level_ps) > 2 else "-"
                    level_ex = level_ps[3].get('data-d', '-') if len(level_ps) > 3 else "-"

                    if img_url:
                        download_image(session, img_url, version, title)
                        
                    music_data_list.append({
                        "Version_Num": version,
                        "Version_Name": current_version_name,
                        "Genre": genre,
                        "Title": title,
                        "Artist": artist,
                        "Light": level_light,
                        "Normal": level_normal,
                        "Hyper": level_hyper,
                        "EX": level_ex
                    })
                    
                    difficulties = [
                        ("light", level_light),
                        ("normal", level_normal),
                        ("hyper", level_hyper),
                        ("ex", level_ex)
                    ]
                    
                    norm_t = normalize_title(title)
                    
                    for dif_name, level in difficulties:
                        if level != "-": 
                            w_data = wiki_data.get((norm_t, dif_name), {})
                            
                            rec = {
                                "version_num": version,
                                "version_name": current_version_name,
                                "genre": genre,
                                "title": title,
                                "artist": artist,
                                "dif_name": dif_name,
                                "level": level,
                                "bpm": w_data.get("bpm", ""),
                                "notes": w_data.get("notes", ""),
                                "dif_tier": w_data.get("dif_tier", "")
                            }
                            # 신규/변경 곡이면 현재 시각, 아니면 이전 song_updated_at 유지
                            music_data_single_list.append(stamp_song(rec, prev_single))
                            
                except Exception as e:
                    print(f"데이터 파싱 에러 발생: {e}")
                    continue
                
            page += 1
            time.sleep(0.5)

    # 4. 전체 데이터를 CSV 파일로 저장
    csv_headers = ["Version_Num", "Version_Name", "Genre", "Title", "Artist", "Light", "Normal", "Hyper", "EX"]
    with open(CSV_FILENAME, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers)
        writer.writeheader()
        for data in music_data_list:
            writer.writerow(data)
            
    single_headers = ["version_num", "version_name", "genre", "title", "artist", "dif_name", "level", "bpm", "notes", "dif_tier", "song_updated_at"]
    with open(CSV_SINGLE_FILENAME, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=single_headers)
        writer.writeheader()
        for data in music_data_single_list:
            writer.writerow(data)

    new_cnt = sum(1 for d in music_data_single_list if single_key(d) not in prev_single)
    upd_cnt = sum(1 for d in music_data_single_list if d["song_updated_at"] == NOW_TS) - new_cnt
    print(f"\n[변경 감지] 신규 {new_cnt}행 / 메타데이터 변경 {upd_cnt}행 / 전체 {len(music_data_single_list)}행")
            
    print(f"\n모든 작업이 완료되었습니다.")
    print(f"이미지 저장 경로: {IMAGES_DIR}")
    print(f"종합 CSV 저장 경로: {CSV_FILENAME}")
    print(f"단일 난이도 분리(+Wiki 정보) CSV 저장 경로: {CSV_SINGLE_FILENAME}")

if __name__ == "__main__":
    main()
