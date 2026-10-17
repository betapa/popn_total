(async function() {
    if (document.getElementById('popn-scraper-ui')) return;

    // --- 설정 변수 (본인 레포지토리에 맞게 수정) ---
    const REPO_OWNER = 'betapa'; // 깃허브 아이디
    const REPO_NAME = 'popn_total'; // 레포지토리 이름
    const FILE_PATH = 'popn_homepage_scraper/popn_music_list_single.csv'; // 저장할 파일 경로
    // -----------------------------------------------

    const ui = document.createElement('div');
    ui.id = 'popn-scraper-ui';
    ui.style.cssText = 'position:fixed;top:20px;right:20px;padding:20px;background:rgba(0,0,0,0.85);color:#fff;z-index:99999;border-radius:10px;font-family:sans-serif;box-shadow:0 4px 6px rgba(0,0,0,0.3);max-height:80vh;overflow-y:auto;width:360px;';
    ui.innerHTML = '<h3 style="margin:0 0 10px 0;color:#fff;font-size:16px;">팝픈 스코어 스크래퍼</h3><p id="popn-scraper-status" style="margin:0;font-size:13px;word-break:keep-all;font-weight:bold;color:#4ade80;">초기화 중...</p><ul id="popn-scraper-log" style="margin-top:10px;padding-left:15px;font-size:12px;color:#ccc;max-height:300px;overflow-y:auto;list-style-type:square;"></ul>';
    document.body.appendChild(ui);

    // 단순 상태 표시는 화면에만 띄우고 콘솔에는 남기지 않음
    function updateStatus(msg) {
        document.getElementById('popn-scraper-status').innerText = msg;
    }

    // 곡 확인, 에러 등 주요 로그는 화면 누적 및 콘솔 출력
    function addLog(msg) {
        const ul = document.getElementById('popn-scraper-log');
        const li = document.createElement('li');
        li.innerText = msg;
        ul.appendChild(li);
        ul.scrollTop = ul.scrollHeight;
        console.log(msg); // 콘솔 출력
    }

    // GitHub 토큰 처리
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

    // 메달 이미지 텍스트 변환
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

    // 공식 사이트 스코어 수집
    async function fetchPlayData(versionName) {
        const dataMap = {};
        addLog(`=== [${versionName}] 데이터 수집 시작 ===`);

        const minVer = (versionName === 'popn29') ? 0 : 1;
        const maxVer = (versionName === 'popn29') ? 29 : 28;

        for (let v = minVer; v <= maxVer; v++) {
            let page = 0;

            while (true) {
                updateStatus(`[${versionName}] 버전 ${v}, 페이지 ${page} 탐색 중...`);
                
                const url = `https://p.eagate.573.jp/game/popn/${versionName}/playdata/mu_top.html?page=${page}&version=${v}&category=0&keyword=&sort=none&sort_type=none`;
                
                const response = await fetch(url);
                const html = await response.text();
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, "text/html");

                const items = Array.from(doc.querySelectorAll('ul.mu_list_table > li')).filter(li => 
                    !li.classList.contains('st_th') && !li.querySelector('.th_music')
                );
                
                if (items.length === 0) break; 

                for (const li of items) {
                    let title = '';
                    let diffsNodes = [];

                    if (versionName === 'jamfizz') {
                        const titleEl = li.querySelector('.col_music a');
                        if (!titleEl) continue;
                        title = titleEl.innerText.trim();
                        diffsNodes = [
                            li.querySelector('.col_5'),      
                            li.querySelector('.col_normal'), 
                            li.querySelector('.col_hyper'),  
                            li.querySelector('.col_ex')      
                        ];
                    } else { // popn29
                        const divs = li.querySelectorAll(':scope > div');
                        if (divs.length < 6) continue;
                        const titleEl = divs[0].querySelector('a');
                        if (!titleEl) continue;
                        title = titleEl.innerText.trim();
                        diffsNodes = [ divs[2], divs[3], divs[4], divs[5] ];
                    }

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
                                songHasData = true;
                            }
                        }
                    });
                    
                    // 플레이 기록이 존재하는 곡명만 로그에 출력
                    if (songHasData) {
                        addLog(`[${versionName}] 기록 확인됨: ${title}`);
                    }
                }

                if (items.length < 25) break; 
                page++;
                await new Promise(r => setTimeout(r, 400));
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
            updateStatus("GitHub 연동 실패. 토큰 권한이나 경로를 확인하세요.");
            if (fileRes.status === 401 || fileRes.status === 403) {
                localStorage.removeItem('GITHUB_PAT'); 
                addLog("권한 에러로 인해 토큰이 초기화되었습니다.");
            }
            setTimeout(() => document.body.removeChild(ui), 5000);
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

        addLog("기존 CSV 데이터 로드 성공!");

        // 2. 스코어 데이터 수집
        const hcData = await fetchPlayData('popn29');
        const jfData = await fetchPlayData('jamfizz');

        updateStatus("데이터 비교 및 병합 중...");
        addLog("=== 양측 서버 데이터를 CSV와 병합합니다 ===");

        let updateCount = 0;
        const updatedSongsSet = new Set();

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
                updatedSongsSet.add(row.title);
            }
        });

        addLog(`총 ${updatedSongsSet.size}개의 곡 (${updateCount}개 채보) 적용 완료`);
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
                message: "Update play data scores via bookmarklet",
                content: encodedContent,
                sha: fileSha
            })
        });

        if (putRes.ok) {
            updateStatus("완료! GitHub에 성공적으로 저장되었습니다.");
            addLog("작업이 성공적으로 종료되었습니다.");
        } else {
            const errJson = await putRes.json();
            updateStatus(`저장 실패`);
            addLog(`API 에러: ${errJson.message}`);
        }

        setTimeout(() => document.body.removeChild(ui), 8000);

    } catch (e) {
        console.error(e);
        updateStatus("에러가 발생했습니다.");
        addLog(e.message);
        setTimeout(() => document.body.removeChild(ui), 8000);
    }
})();