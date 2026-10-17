import os
import csv
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
    
    # Chrome 120 버전의 지문(Fingerprint)으로 완전 위장
    session = requests.Session(impersonate="chrome120")
    
    # 1. 위키 데이터 선행 수집
    wiki_data = fetch_wiki_data(session)

    print(f"[{BASE_DIR}] 위치에 공식 홈페이지 정보 수집 및 저장을 시작합니다...\n")

    # 2. 공식 홈페이지 버전 0부터 29까지 탐색 루프
    for version in range(30):
        page = 0
        current_version_name = VERSION_NAMES[version]
        
        while True:
            url = f"https://p.eagate.573.jp/game/popn/popn29/music/list.html?page={page}&version={version}&lv=0&bemani=0&category=0&keyword=&sort=music&sort_type=up"
            print(f"▶ 탐색 중: Version {version} ({current_version_name}), Page {page}")
            
            response = session.get(url, headers=HEADERS)
            
            if response.status_code != 200:
                print(f"페이지 로드 실패 (Status: {response.status_code}): {url}")
                break
                
            soup = BeautifulSoup(response.content.decode('utf-8', 'ignore'), 'html.parser')
            
            ul_tables = soup.find_all('ul', class_='mu_list_table')
            target_ul = None
            for ul in ul_tables:
                if 'mu_head' not in ul.get('class', []):
                    target_ul = ul
                    break
                    
            if not target_ul:
                print("곡 리스트를 찾을 수 없습니다. 다음 버전으로 넘어갑니다.")
                break
                
            list_items = target_ul.find_all('li', recursive=False)
            
            song_count = len(list_items) // 3
            if song_count == 0:
                print(f"곡이 존재하지 않습니다. 버전 {version} 탐색 종료.\n")
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
                            
                            music_data_single_list.append({
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
                            })
                            
                except Exception as e:
                    print(f"데이터 파싱 에러 발생: {e}")
                    continue
            
            if song_count < 25:
                print(f"→ 해당 버전의 마지막 페이지입니다. (Version {version} 완료)\n")
                break
                
            page += 1
            time.sleep(0.5) 

    # 4. 전체 데이터를 CSV 파일로 저장
    csv_headers = ["Version_Num", "Version_Name", "Genre", "Title", "Artist", "Light", "Normal", "Hyper", "EX"]
    with open(CSV_FILENAME, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers)
        writer.writeheader()
        for data in music_data_list:
            writer.writerow(data)
            
    single_headers = ["version_num", "version_name", "genre", "title", "artist", "dif_name", "level", "bpm", "notes", "dif_tier"]
    with open(CSV_SINGLE_FILENAME, mode='w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=single_headers)
        writer.writeheader()
        for data in music_data_single_list:
            writer.writerow(data)
            
    print(f"\n모든 작업이 완료되었습니다.")
    print(f"이미지 저장 경로: {IMAGES_DIR}")
    print(f"종합 CSV 저장 경로: {CSV_FILENAME}")
    print(f"단일 난이도 분리(+Wiki 정보) CSV 저장 경로: {CSV_SINGLE_FILENAME}")

if __name__ == "__main__":
    main()