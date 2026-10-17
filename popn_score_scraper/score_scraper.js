(async function() {
    if (document.getElementById('popn-scraper-ui')) return;

    // --- 설정 변수 (본인 레포지토리에 맞게 수정) ---
    const REPO_OWNER = 'betapa'; // 깃허브 아이디
    const REPO_NAME = 'popn_total'; // 레포지토리 이름
    const FILE_PATH = 'popn_homepage_scraper/popn_music_list_single.csv'; // 저장할 파일 경로
    // -----------------------------------------------

    const ui = document.createElement('div');
    ui.id = 'popn-scraper-ui';
    ui.style.cssText = 'position:fixed;top:20px;right:20px;padding:20px;background:rgba(0,0,0,0.8);color:#fff;z-index:99999;border-radius:10px;font-family:sans-serif;box-shadow:0 4px 6px rgba(0,0,0,0.3);';
    ui.innerHTML = '<h3 style="margin:0 0 10px 0;color:#fff;">팝픈 스코어 스크래퍼</h3><p id="popn-scraper-status" style="margin:0;font-size:14px;">초기화 중...</p>';
    document.body.appendChild(ui);

    function updateStatus(msg) {
        document.getElementById('popn-scraper-status').innerText = msg;
        console.log(msg);
    }

    // GitHub 토큰 처리 (localStorage에 저장하여 매번 묻지 않게 함)
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

    // UTF-8 문자열을 Base64로 인코딩 (GitHub API 요구사항)
    function utf8ToBase64(str) {
        return btoa(encodeURIComponent(str).replace(/%([0-9A-F]{2})/g, function(match, p1) {
            return String.fromCharCode('0x' + p1);
        }));
    }

    async function fetchPlayData(versionName) {
        const dataMap = {};
        let page = 0;
        
        while (true) {
            updateStatus(`[${versionName}] 페이지 ${page} 탐색 중...`);
            const vQuery = versionName === 'popn29' ? 0 : 1; 
            const url = `https://p.eagate.573.jp/game/popn/${versionName}/playdata/mu_top.html?page=${page}&version=${vQuery}&category=0&keyword=&sort=none&sort_type=none`;
            
            const response = await fetch(url);
            const html = await response.text();
            
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, "text/html");

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
                        const img = col.querySelector('img:nth-child(1)');
                        const scoreText = col.innerText.replace(/[^0-9]/g, '');
                        const score = scoreText ? parseInt(scoreText, 10) : 0;
                        const medalSrc = img ? img.src : '';
                        const medal = getMedalName(medalSrc);

                        if (score > 0 || medal !== '') {
                            dataMap[`${title}_${diff.name}`] = { score, medal };
                        }
                    }
                });
            });

            if (items.length < 25) {
                updateStatus(`[${versionName}] 탐색 완료.`);
                break;
            }
            page++;
            await new Promise(r => setTimeout(r, 500));
        }
        return dataMap;
    }

    try {
        // 1. GitHub API를 통해 기존 CSV 파일 로드 및 sha 값 확보
        updateStatus("GitHub에서 기존 CSV를 불러옵니다...");
        const apiUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/contents/${FILE_PATH}`;
        
        const fileRes = await fetch(apiUrl, {
            headers: { 'Authorization': `token ${token}` }
        });
        
        if (!fileRes.ok) {
            updateStatus("GitHub 연동 실패. 토큰이나 경로를 확인하세요.");
            if (fileRes.status === 401) localStorage.removeItem('GITHUB_PAT'); // 토큰 재입력 유도
            setTimeout(() => document.body.removeChild(ui), 4000);
            return;
        }

        const fileData = await fileRes.json();
        const fileSha = fileData.sha; // 파일 업데이트 시 반드시 필요함
        
        // base64로 인코딩된 파일 내용을 디코딩
        const csvText = decodeURIComponent(atob(fileData.content).split('').map(function(c) {
            return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
        }).join(''));

        const lines = csvText.split('\n').map(line => line.trim()).filter(line => line !== '');
        
        // 첫 번째 문자열에 BOM(\uFEFF)이 있으면 제거
        let headerLine = lines[0];
        if (headerLine.charCodeAt(0) === 0xFEFF) {
            headerLine = headerLine.substring(1);
        }

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

        // 2. 스코어 데이터 수집
        const hcData = await fetchPlayData('popn29');
        const jfData = await fetchPlayData('jamfizz');

        updateStatus("데이터 비교 및 병합 중...");

        data.forEach(row => {
            const key = `${row.title}_${row.dif_name}`;
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

            row.medal = bestMedal;
            row.score = bestScore > 0 ? bestScore : '';
            row.best_version = bestVer;
        });

        updateStatus("GitHub로 갱신된 파일을 전송 중...");

        let newCsvStr = headers.join(',') + '\n';
        data.forEach(row => {
            newCsvStr += headers.map(h => escapeCSV(row[h])).join(',') + '\n';
        });

        // 엑셀 한글 깨짐 방지를 위한 BOM 추가 후 Base64 인코딩
        const encodedContent = utf8ToBase64('\uFEFF' + newCsvStr);

        // 3. GitHub API로 파일 업데이트(PUT)
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
        } else {
            const errJson = await putRes.json();
            updateStatus(`저장 실패: ${errJson.message}`);
        }

        setTimeout(() => document.body.removeChild(ui), 5000);

    } catch (e) {
        console.error(e);
        updateStatus("에러가 발생했습니다. 콘솔을 확인해주세요.");
        setTimeout(() => document.body.removeChild(ui), 5000);
    }
})();