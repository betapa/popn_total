// popn_score_scraper/popn_score_bookmarklet.js

(async function() {
    // 이미 실행 중인지 확인
    if (document.getElementById('popn-scraper-ui')) return;

    // UI 오버레이 생성 (진행 상황 표시용)
    const ui = document.createElement('div');
    ui.id = 'popn-scraper-ui';
    ui.style.cssText = 'position:fixed;top:20px;right:20px;padding:20px;background:rgba(0,0,0,0.8);color:#fff;z-index:99999;border-radius:10px;font-family:sans-serif;box-shadow:0 4px 6px rgba(0,0,0,0.3);';
    ui.innerHTML = '<h3 style="margin:0 0 10px 0;color:#fff;">팝픈 스코어 스크래퍼</h3><p id="popn-scraper-status" style="margin:0;font-size:14px;">초기화 중...</p>';
    document.body.appendChild(ui);

    function updateStatus(msg) {
        document.getElementById('popn-scraper-status').innerText = msg;
        console.log(msg);
    }

    // 메달 이미지 파일명을 기반으로 텍스트 매핑 (image_185c19.png 조건)
    function getMedalName(src) {
        if (!src || src.includes('meda_none')) return '';
        // e-amusement 메달 파일명 규칙 대응 (meda_a ~ meda_k)
        const match = src.match(/meda_([a-k])\.png/);
        if (!match) return '';
        
        const medalMap = {
            'a': '금☆', 'b': '은☆', 'c': '은◇', 'd': '은○',
            'e': '동☆', 'f': '동◇', 'g': '동○',
            'h': '흑☆', 'i': '흑◇', 'j': '흑○',
            'k': '이지'
        };
        return medalMap[match[1]] || '';
    }

    // CSV 파싱 유틸
    function parseCSVRow(str) {
        let result = [], inQuotes = false, val = '';
        for (let i = 0; i < str.length; i++) {
            let char = str[i];
            if (char === '"') {
                if (inQuotes && str[i + 1] === '"') { val += '"'; i++; } 
                else { inQuotes = !inQuotes; }
            } else if (char === ',' && !inQuotes) {
                result.push(val); val = '';
            } else { val += char; }
        }
        result.push(val);
        return result;
    }

    function escapeCSV(val) {
        if (val == null) return '';
        let str = String(val);
        if (str.includes(',') || str.includes('"') || str.includes('\n')) {
            return '"' + str.replace(/"/g, '""') + '"';
        }
        return str;
    }

    // 플레이 데이터 스크래핑 함수
    async function fetchPlayData(versionName) {
        const dataMap = {};
        let page = 0;
        
        while (true) {
            updateStatus(`[${versionName}] 페이지 ${page} 탐색 중...`);
            // version=0(popn29) 또는 version=1(jamfizz) 인덱스 대응용
            const vQuery = versionName === 'popn29' ? 0 : 1; 
            const url = `https://p.eagate.573.jp/game/popn/${versionName}/playdata/mu_top.html?page=${page}&version=${vQuery}&category=0&keyword=&sort=none&sort_type=none`;
            
            const response = await fetch(url);
            const html = await response.text();
            
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, "text/html");

            // 곡 리스트 가져오기
            const items = doc.querySelectorAll('ul.mu_list_table > li:not(.st_th)');
            if (items.length === 0) break;

            items.forEach(li => {
                const titleEl = li.querySelector('.col_music a');
                if (!titleEl) return;
                const title = titleEl.innerText.trim();

                const diffs = [
                    { name: 'light', col: 'col_5' },
                    { name: 'normal', col: 'col_normal' },
                    { name: 'hyper', col: 'col_hyper' },
                    { name: 'ex', col: 'col_ex' }
                ];

                diffs.forEach(diff => {
                    const col = li.querySelector(`.${diff.col}`);
                    if (col) {
                        const img = col.querySelector('img:nth-child(1)'); // 메달 이미지
                        const scoreText = col.innerText.replace(/[^0-9]/g, ''); // 점수 (정수형 변환)
                        const score = scoreText ? parseInt(scoreText, 10) : 0;
                        const medalSrc = img ? img.src : '';
                        const medal = getMedalName(medalSrc);

                        if (score > 0 || medal !== '') {
                            dataMap[`${title}_${diff.name}`] = { score, medal };
                        }
                    }
                });
            });

            // 페이지에 로드된 곡이 25개 미만이면 마지막 페이지
            if (items.length < 25) {
                updateStatus(`[${versionName}] 탐색 완료.`);
                break;
            }
            page++;
            await new Promise(r => setTimeout(r, 500)); // 과부하 방지 딜레이
        }
        return dataMap;
    }

    try {
        // 1. 기존 통합 CSV 로드 (Github 레포지토리의 Raw URL 사용)
        updateStatus("기본 CSV 파일을 로드하는 중...");
        const csvUrl = 'https://raw.githubusercontent.com/betapa/popn_total/main/popn_homepage_scraper/popn_music_list_single.csv';
        const csvRes = await fetch(csvUrl);
        
        if (!csvRes.ok) {
            updateStatus("CSV 로드 실패! 레포지토리 경로를 확인하세요.");
            setTimeout(() => document.body.removeChild(ui), 3000);
            return;
        }

        const csvText = await csvRes.text();
        const lines = csvText.split('\n').map(line => line.trim()).filter(line => line !== '');
        
        const headers = parseCSVRow(lines[0]);
        // 새 칼럼 추가
        if (!headers.includes("medal")) headers.push("medal");
        if (!headers.includes("score")) headers.push("score");
        if (!headers.includes("best_version")) headers.push("best_version");

        const data = [];
        for (let i = 1; i < lines.length; i++) {
            const rowArr = parseCSVRow(lines[i]);
            const rowObj = {};
            headers.forEach((h, idx) => {
                rowObj[h] = rowArr[idx] || '';
            });
            data.push(rowObj);
        }

        // 2. High☆Cheers!! (hc) 와 Jam&Fizz (jf) 데이터 수집
        const hcData = await fetchPlayData('popn29');
        const jfData = await fetchPlayData('jamfizz');

        updateStatus("데이터 비교 및 병합 중...");

        // 3. 비교 병합 로직
        data.forEach(row => {
            const key = `${row.title}_${row.dif_name}`;
            const hc = hcData[key] || { score: 0, medal: '' };
            const jf = jfData[key] || { score: 0, medal: '' };

            let bestScore = 0;
            let bestMedal = '';
            let bestVer = '';

            // 점수 비교 (동점일 경우 hc를 우선)
            if (hc.score > 0 || jf.score > 0) {
                if (hc.score >= jf.score) {
                    bestScore = hc.score;
                    bestMedal = hc.medal;
                    bestVer = 'hc';
                } else {
                    bestScore = jf.score;
                    bestMedal = jf.medal;
                    bestVer = 'jf';
                }
            } else if (hc.medal || jf.medal) {
                // 스코어가 0이어도 클리어 메달만 존재하는 경우 처리
                bestMedal = hc.medal || jf.medal;
                bestVer = hc.medal ? 'hc' : 'jf';
            }

            row.medal = bestMedal;
            row.score = bestScore > 0 ? bestScore : '';
            row.best_version = bestVer;
        });

        updateStatus("CSV 파일 생성 중...");

        // 4. 새로운 CSV 다운로드 트리거
        let newCsvStr = headers.join(',') + '\n';
        data.forEach(row => {
            newCsvStr += headers.map(h => escapeCSV(row[h])).join(',') + '\n';
        });

        // UTF-8 BOM 추가하여 한글 깨짐 방지
        const blob = new Blob(['\uFEFF' + newCsvStr], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement("a");
        const url = URL.createObjectURL(blob);
        link.setAttribute("href", url);
        link.setAttribute("download", "popn_music_list_single.csv");
        link.style.visibility = 'hidden';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);

        updateStatus("완료! 파일이 다운로드되었습니다.");
        setTimeout(() => document.body.removeChild(ui), 4000);

    } catch (e) {
        console.error(e);
        updateStatus("에러가 발생했습니다. 콘솔을 확인해주세요.");
        setTimeout(() => document.body.removeChild(ui), 5000);
    }
})();