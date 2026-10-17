(async function() {
    if (document.getElementById('popn-scraper-ui')) return;

    // --- 설정 변수 ---
    const REPO_OWNER = 'betapa'; 
    const REPO_NAME = 'popn_total';
    const FILE_PATH = 'popn_homepage_scraper/popn_music_list_single.csv';
    // -----------------

    // UI 오버레이 생성
    const ui = document.createElement('div');
    ui.id = 'popn-scraper-ui';
    ui.style.cssText = 'position:fixed;top:20px;right:20px;padding:20px;background:rgba(0,0,0,0.85);color:#fff;z-index:99999;border-radius:10px;font-family:sans-serif;box-shadow:0 4px 6px rgba(0,0,0,0.3);max-height:80vh;overflow-y:auto;width:340px;';
    ui.innerHTML = '<h3 style="margin:0 0 10px 0;color:#fff;font-size:16px;">팝픈 스코어 스크래퍼</h3><p id="popn-scraper-status" style="margin:0;font-size:13px;word-break:keep-all;font-weight:bold;color:#4ade80;">초기화 중...</p><ul id="popn-scraper-log" style="margin-top:10px;padding-left:15px;font-size:11px;color:#ccc;max-height:250px;overflow-y:auto;"></ul>';
    document.body.appendChild(ui);

    function updateStatus(msg) {
        document.getElementById('popn-scraper-status').innerText = msg;
    }

    function addLog(msg) {
        const ul = document.getElementById('popn-scraper-log');
        const li = document.createElement('li');
        li.innerText = msg;
        ul.appendChild(li);
        ul.scrollTop = ul.scrollHeight;
        console.log(msg);
    }

    // GitHub 토큰 체크 및 저장
    let token = localStorage.getItem('GITHUB_PAT');
    if (!token) {
        token = prompt("GitHub Personal Access Token (PAT)을 입력하세요:\n(이 브라우저에 저장됩니다)");
        if (!token) {
            updateStatus("토큰이 없어 작업을 취소합니다.");
            setTimeout(() => document.body.removeChild(ui), 3000);
            return;
        }
        localStorage.setItem('GITHUB_PAT', token);
    }

    // 메달명 변환 함수 (image_185c19.png 기준)
    function getMedalName(src) {
        if (!src || src.includes('meda_none')) return '';
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

    // 곡 제목 정규화 (공식 홈페이지 간, 위키 간 매칭 누락 방지용)
    function normalizeTitle(title) {
        if (!title) return '';
        let s = title.normalize('NFKC').toLowerCase();
        s = s.replace(/Ⓤ/g, "(upper)");
        s = s.replace(/[\s\~\-\.\,\!\?\'\"☆♡♪]/g, '');
        return s;
    }

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

    function utf8ToBase64(str) {
        return btoa(encodeURIComponent(str).replace(/%([0-9A-F]{2})/g, function(match, p1) {
            return String.fromCharCode('0x' + p1);
        }));
    }

    // 각 사이트 플레이 데이터 스크래퍼
    async function fetchPlayData(versionName) {
        const dataMap = {};
        addLog(`=== [${versionName}] 데이터 수집 시작 ===`);

        // 버전별 범위 설정 (popn29는 0~29, jamfizz는 1~28 버전까지 존재)
        const minVer = (versionName === 'popn29') ? 0 : 1;
        const maxVer = (versionName === 'popn29') ? 29 : 28;

        for (let v = minVer; v <= maxVer; v++) {
            let page = 0;
            let verSongCount = 0;
            let verChartCount = 0;

            while (true) {
                const url = `https://p.eagate.573.jp/game/popn/${versionName}/playdata/mu_top.html?page=${page}&version=${v}&category=0&keyword=&sort=none&sort_type=none`;
                
                const response = await fetch(url);
                const html = await response.text();
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, "text/html");

                // 테이블 헤더(.st_th, .th_music) 제외한 실제 곡 데이터 항목만 추출
                const items = Array.from(doc.querySelectorAll('ul.mu_list_table > li')).filter(li => 
                    !li.classList.contains('st_th') && !li.querySelector('.th_music')
                );
                
                if (items.length === 0) break; // 검색된 곡이 없으면 다음 버전으로

                for (const li of items) {
                    let title = '';
                    let diffsNodes = [];

                    // 사이트별 DOM 구조 분기 처리
                    if (versionName === 'jamfizz') {
                        const titleEl = li.querySelector('.col_music a');
                        if (!titleEl) continue;
                        title = titleEl.innerText.trim();
                        diffsNodes = [
                            li.querySelector('.col_5'),      // Light(Easy)
                            li.querySelector('.col_normal'), // Normal
                            li.querySelector('.col_hyper'),  // Hyper
                            li.querySelector('.col_ex')      // EX
                        ];
                    } else { // popn29
                        const divs = li.querySelectorAll(':scope > div');
                        if (divs.length < 6) continue;
                        const titleEl = divs[0].querySelector('a');
                        if (!titleEl) continue;
                        title = titleEl.innerText.trim();
                        diffsNodes = [ divs[2], divs[3], divs[4], divs[5] ];
                    }

                    // 요청사항 반영: 현재 탐색 중인 곡을 UI에 실시간 출력
                    updateStatus(`[${versionName}] 곡 스코어 확인 중: ${title}`);

                    const diffNames = ['light', 'normal', 'hyper', 'ex'];
                    let songHasData = false;

                    diffsNodes.forEach((node, idx) => {
                        if (node) {
                            const diffName = diffNames[idx];
                            const img = node.querySelector('img:nth-child(1)');
                            const scoreText = node.innerText.replace(/[^0-9]/g, '');
                            const score = scoreText ? parseInt(scoreText, 10) : 0;
                            const medalSrc = img ? img.src : '';
                            const medal = getMedalName(medalSrc);

                            if (score > 0 || medal !== '') {
                                dataMap[`${normalizeTitle(title)}_${diffName}`] = { score, medal };
                                verChartCount++;
                                songHasData = true;
                            }
                        }
                    });
                    
                    if (songHasData) verSongCount++;
                }

                if (items.length < 25) break; // 곡이 25개 미만이면 해당 버전의 마지막 페이지
                page++;
                await new Promise(r => setTimeout(r, 400)); // 과부하 방지
            }
            
            if (verSongCount > 0) {
                addLog(`[${versionName}] Ver.${v} 완료: ${verSongCount}곡 / ${verChartCount}개 패턴 확인`);
            }
        }
        return dataMap;
    }

    try {
        updateStatus("GitHub에서 기존 CSV를 불러옵니다...");
        const apiUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/contents/${FILE_PATH}`;
        
        const fileRes = await fetch(apiUrl, {
            headers: { 'Authorization': `token ${token}`, 'Cache-Control': 'no-cache' }
        });
        
        if (!fileRes.ok) {
            updateStatus("GitHub 연동 실패. 권한이나 경로를 확인하세요.");
            if (fileRes.status === 401 || fileRes.status === 403) {
                localStorage.removeItem('GITHUB_PAT'); 
                addLog("권한 에러로 인해 저장된 토큰이 초기화되었습니다.");
            }
            setTimeout(() => document.body.removeChild(ui), 6000);
            return;
        }

        const fileData = await fileRes.json();
        const fileSha = fileData.sha;
        
        const csvText = decodeURIComponent(atob(fileData.content).split('').map(function(c) {
            return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
        }).join(''));

        const lines = csvText.split('\n').map(line => line.trim()).filter(line => line !== '');
        
        let headerLine = lines[0];
        if (headerLine.charCodeAt(0) === 0xFEFF) headerLine = headerLine.substring(1);

        const headers = parseCSVRow(headerLine);
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

        addLog("기존 CSV 데이터 로드 성공");

        // 스코어 데이터 수집
        const hcData = await fetchPlayData('popn29');
        const jfData = await fetchPlayData('jamfizz');

        updateStatus("데이터 비교 및 병합 중...");
        addLog("수집된 스코어와 CSV 정보를 병합합니다.");

        let updateCount = 0;

        data.forEach(row => {
            const normTitle = normalizeTitle(row.title);
            const key = `${normTitle}_${row.dif_name}`;
            
            const hc = hcData[key] || { score: 0, medal: '' };
            const jf = jfData[key] || { score: 0, medal: '' };

            let bestScore = 0;
            let bestMedal = '';
            let bestVer = '';

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
                bestMedal = hc.medal || jf.medal;
                bestVer = hc.medal ? 'hc' : 'jf';
            }

            if (bestScore > 0 || bestMedal !== '') {
                row.medal = bestMedal;
                row.score = bestScore > 0 ? bestScore : '';
                row.best_version = bestVer;
                updateCount++;
            }
        });

        addLog(`총 ${updateCount}개의 채보 정보가 업데이트되었습니다.`);
        updateStatus("GitHub로 갱신된 파일을 전송 중...");

        let newCsvStr = headers.join(',') + '\n';
        data.forEach(row => {
            newCsvStr += headers.map(h => escapeCSV(row[h])).join(',') + '\n';
        });

        const encodedContent = utf8ToBase64('\uFEFF' + newCsvStr);

        const putRes = await fetch(apiUrl, {
            method: 'PUT',
            headers: {
                'Authorization': `token ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                message: "Update user play scores (Bookmarklet)",
                content: encodedContent,
                sha: fileSha
            })
        });

        if (putRes.ok) {
            updateStatus("완료! GitHub에 성공적으로 저장되었습니다.");
            addLog("모든 작업이 성공적으로 종료되었습니다.");
        } else {
            const errJson = await putRes.json();
            updateStatus(`저장 실패`);
            addLog(`API 오류: ${errJson.message}`);
        }

        setTimeout(() => document.body.removeChild(ui), 8000);

    } catch (e) {
        console.error(e);
        updateStatus("실행 중 오류가 발생했습니다.");
        addLog(e.message);
        setTimeout(() => document.body.removeChild(ui), 8000);
    }
})();