import os
import csv
import subprocess
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

SONG_COMPARE_FIELDS = ["version_name", "level", "bpm", "notes", "dif_tier"]
WIKI_FIELDS = ["bpm", "notes", "dif_tier"]


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


def normalize_title(title):
    s = unicodedata.normalize('NFKC', title).lower()
    s = s.replace("Ⓤ", "(upper)")
    s = re.sub(r'[\s\~\-\.\,\!\?\'\"☆♡♪]', '', s)
    return s


def fetch_wiki_data(session):
    print("\n[Wiki 정보 수집] popn.wiki에서 40~50레벨 세부 정보를 수집합니다 (로컬 직접 연결)...")
    wiki_data = {}
    
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
                print(f" - Wiki Lv{lv} 수집 결과 : 총 {level_total_songs}곡 (난이도 지수 {level_tier_collected}개)")
            else:
                print(f"   (Lv{lv} 시도 실패: HTTP {response.status_code})")
                
        except Exception as e:
            print(f"   (Lv{lv} 연결 에러 발생: {e})")
            
        time.sleep(1) # 과부하 방지
            
    print(f"위키 데이터 수집 완료. (총 {len(wiki_data)}개의 패턴 데이터 확보)\n")
    return wiki_data


def download_image(session, img_url, version_num, file_name):
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
            pass


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
    music_data_single_list = []

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
    wiki_data = fetch_wiki_data(session)

    print(f"\n[{BASE_DIR}] 공식 홈페이지 정보 수집을 시작합니다...\n")

    # 2. 공식 홈페이지 파싱
    for version in range(30):
        page = 0
        current_version_name = VERSION_NAMES[version]
        
        while True:
            url = f"https://p.eagate.573.jp/game/popn/popn29/music/list.html?page={page}&version={version}&lv=0&bemani=0&category=0&keyword=&sort=music&sort_type=up"
            response = session.get(url, headers=HEADERS)
            
            if response.status_code != 200:
                break
                
            try:
                soup = BeautifulSoup(response.content.decode('utf-8', 'ignore'), 'lxml')
            except Exception:
                soup = BeautifulSoup(response.content.decode('utf-8', 'ignore'), 'html.parser')
            
            ul_tables = soup.find_all('ul', class_='mu_list_table')
            target_ul = next((ul for ul in ul_tables if 'mu_head' not in ul.get('class', [])), None)
                    
            if not target_ul:
                break
                
            list_items = target_ul.find_all('li', recursive=False)
            song_count = len(list_items) // 3
            print(f"▶ 탐색 중: Version {version} ({current_version_name}), Page {page} Songs {song_count}")
            
            if song_count == 0:
                print(f"→ 해당 버전의 마지막 페이지에 도달했습니다. (Version {version} 완료)\n")
                break
            
            for i in range(0, len(list_items), 3):
                if i + 2 >= len(list_items):
                    break
                    
                try:
                    li_img, li_info, li_level = list_items[i], list_items[i+1], list_items[i+2]
                    
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
                        "Version_Num": version, "Version_Name": current_version_name,
                        "Genre": genre, "Title": title, "Artist": artist,
                        "Light": level_light, "Normal": level_normal,
                        "Hyper": level_hyper, "EX": level_ex
                    })
                    
                    norm_t = normalize_title(title)
                    for dif_name, level in [("light", level_light), ("normal", level_normal), ("hyper", level_hyper), ("ex", level_ex)]:
                        if level != "-": 
                            w_data = wiki_data.get((norm_t, dif_name), {})
                            rec = {
                                "version_num": version, "version_name": current_version_name,
                                "genre": genre, "title": title, "artist": artist,
                                "dif_name": dif_name, "level": level,
                                "bpm": w_data.get("bpm", ""), "notes": w_data.get("notes", ""),
                                "dif_tier": w_data.get("dif_tier", "")
                            }
                            music_data_single_list.append(stamp_song(rec, prev_single))
                            
                except Exception as e:
                    continue
                
            page += 1
            time.sleep(0.5)

    # 4. 파일 저장
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

    print("\n모든 데이터 저장 완료.")
    
    # 5. GitHub 자동 업로드
    push_to_github()

if __name__ == "__main__":
    main()