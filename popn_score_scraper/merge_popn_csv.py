import pandas as pd
import numpy as np

def get_best_record(hc_sc, hc_md, jf_sc, jf_md):
    """
    HC와 JF의 스코어 및 메달을 비교하여 더 좋은 기록을 반환하는 함수
    동점일 경우 HC의 데이터를 우선적으로 선택합니다.
    """
    # 스코어를 정수형으로 안전하게 변환 (비어있거나 숫자가 아니면 0)
    val_hc = int(hc_sc) if str(hc_sc).strip().isdigit() else 0
    val_jf = int(jf_sc) if str(jf_sc).strip().isdigit() else 0

    if val_hc > 0 or val_jf > 0:
        if val_hc >= val_jf:
            return hc_sc, hc_md, 'hc'
        else:
            return jf_sc, jf_md, 'jf'
    else:
        # 두 버전 모두 스코어가 0이거나 없는 경우, 메달 존재 여부로 확인
        str_hc_md = str(hc_md).strip()
        str_jf_md = str(jf_md).strip()
        
        if str_hc_md or str_jf_md:
            if str_hc_md:
                return hc_sc, hc_md, 'hc'
            else:
                return jf_sc, jf_md, 'jf'
        else:
            # 둘 다 기록이 없는 경우
            return '', '', ''

def merge_popn_data(hc_file, jf_file, output_file):
    # 1. CSV 데이터 불러오기 (모든 데이터를 문자열로 취급하여 형변환 오류 방지)
    try:
        df_hc = pd.read_csv(hc_file, dtype=str)
    except Exception as e:
        print(f"HC 파일을 읽는 중 오류가 발생했습니다: {e}")
        # 파일이 없을 경우를 대비한 빈 데이터프레임 구조 생성
        df_hc = pd.DataFrame(columns=[
            'Version', 'SongID', 'Title', 'Genre', 'Artist', 
            'Big_Medal', 'Big_Score', 'Light_Medal', 'Light_Score', 
            'Normal_Medal', 'Normal_Score', 'Hyper_Medal', 'Hyper_Score', 'Ex_Medal', 'Ex_Score'
        ])

    try:
        df_jf = pd.read_csv(jf_file, dtype=str)
    except Exception as e:
        print(f"JF 파일을 읽는 중 오류가 발생했습니다: {e}")
        df_jf = pd.DataFrame(columns=[
            'Version', 'SongID', 'Title', 'Genre', 'Artist', 
            'Easy_Medal', 'Easy_Score', 'Normal_Medal', 'Normal_Score', 
            'Hyper_Medal', 'Hyper_Score', 'Ex_Medal', 'Ex_Score'
        ])

    # 2. jf 파일의 Easy를 Light로 이름 변경하여 매핑 통일
    df_jf = df_jf.rename(columns={'Easy_Medal': 'Light_Medal', 'Easy_Score': 'Light_Score'})

    # 3. SongID를 기준으로 병합 (Outer Join을 통해 두 파일 중 한 곳에만 있는 곡도 보존)
    df_merged = pd.merge(df_hc, df_jf, on='SongID', how='outer', suffixes=('_hc', '_jf'))

    # NaN 값을 빈 문자열로 처리
    df_merged = df_merged.fillna('')

    final_rows = []

    # 4. 각 곡(행) 단위로 순회하며 최고 기록 추출
    for _, row in df_merged.iterrows():
        # 기본 정보 (hc를 우선으로 하되, hc에 없으면 jf의 데이터를 사용)
        version = row['Version_hc'] if row['Version_hc'] else row['Version_jf']
        title = row['Title_hc'] if row['Title_hc'] else row['Title_jf']
        genre = row['Genre_hc'] if row['Genre_hc'] else row['Genre_jf']
        artist = row['Artist_hc'] if row['Artist_hc'] else row['Artist_jf']
        song_id = row['SongID']

        # Big 난이도는 hc 버전에만 존재하므로 hc 데이터를 그대로 사용
        big_medal = row['Big_Medal'] if 'Big_Medal' in row else ''
        big_score = row['Big_Score'] if 'Big_Score' in row else ''

        # 나머지 난이도별 기록 비교 및 병합
        light_score, light_medal, light_ver = get_best_record(row['Light_Score_hc'], row['Light_Medal_hc'], row['Light_Score_jf'], row['Light_Medal_jf'])
        normal_score, normal_medal, normal_ver = get_best_record(row['Normal_Score_hc'], row['Normal_Medal_hc'], row['Normal_Score_jf'], row['Normal_Medal_jf'])
        hyper_score, hyper_medal, hyper_ver = get_best_record(row['Hyper_Score_hc'], row['Hyper_Medal_hc'], row['Hyper_Score_jf'], row['Hyper_Medal_jf'])
        ex_score, ex_medal, ex_ver = get_best_record(row['Ex_Score_hc'], row['Ex_Medal_hc'], row['Ex_Score_jf'], row['Ex_Medal_jf'])

        # 행 정보 추가
        final_rows.append({
            'Version': version,
            'SongID': song_id,
            'Title': title,
            'Genre': genre,
            'Artist': artist,
            'Big_Medal': big_medal,
            'Big_Score': big_score,
            'Light_Medal': light_medal,
            'Light_Score': light_score,
            'Light_Version': light_ver,
            'Normal_Medal': normal_medal,
            'Normal_Score': normal_score,
            'Normal_Version': normal_ver,
            'Hyper_Medal': hyper_medal,
            'Hyper_Score': hyper_score,
            'Hyper_Version': hyper_ver,
            'Ex_Medal': ex_medal,
            'Ex_Score': ex_score,
            'Ex_Version': ex_ver
        })

    # 5. 최종 데이터프레임 생성 및 열 순서 지정
    df_final = pd.DataFrame(final_rows)
    cols = [
        'Version', 'SongID', 'Title', 'Genre', 'Artist', 
        'Big_Medal', 'Big_Score', 
        'Light_Medal', 'Light_Score', 'Light_Version', 
        'Normal_Medal', 'Normal_Score', 'Normal_Version', 
        'Hyper_Medal', 'Hyper_Score', 'Hyper_Version', 
        'Ex_Medal', 'Ex_Score', 'Ex_Version'
    ]
    df_final = df_final[cols]

    # 6. 결과 저장 (엑셀 호환을 위해 utf-8-sig 인코딩 사용)
    df_final.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f"'{output_file}' 병합 및 저장이 완료되었습니다. 총 {len(df_final)}곡이 처리되었습니다.")

if __name__ == "__main__":
    # 실행하려는 파이썬 파일과 동일한 경로에 csv 파일들이 있어야 합니다.
    merge_popn_data('popn_playdata_hc.csv', 'popn_playdata_jf.csv', 'popn_playdata.csv')