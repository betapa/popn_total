(async function() {
    if (document.getElementById('popn-scraper-ui')) return;

    // --- 설정 변수 (본인 레포지토리에 맞게 수정) ---
    const REPO_OWNER = 'betapa'; // 깃허브 아이디
    const REPO_NAME = 'popn_total'; // 레포지토리 이름
    const FILE_PATH = 'popn_homepage_scraper/popn_music_list_single.csv'; // 저장할 파일 경로
    // -----------------------------------------------

    const ui = document.createElement('div');
    ui.id = 'popn-scraper-ui';
    ui.style.cssText = 'position:fixed;top:20px;right:20px;padding:20px;background:rgba(0,0,0,0.85);color:#fff;z-index:99999;border-radius:10px;font-family:sans-serif;box-shadow:0 4px 6px rgba(0,0,0,0.3);max-height:80vh;overflow-y:auto;width:380px;';
    ui.innerHTML = '<h3 style="margin:0 0 10px 0;color:#fff;font-size:16px;">팝픈 스코어 스크래퍼</h3><p id="popn-scraper-status" style="margin:0;font-size:13px;word-break:keep-all;font-weight:bold;color:#4ade80;">초기화 중...</p><ul id="popn-scraper-log" style="margin-top:10px;padding-left:15px;font-size:11px;color:#ccc;max-height:250px;overflow-y:auto;list-style-type:square;"></ul>';
    document.body.appendChild(ui);

    function updateStatus(msg) {
        const el = document.getElementById('popn-scraper-status');
        if (el) el.innerText = msg;
    }

    function addLog(msg) {
        const ul = document.getElementById('popn-scraper-log');
        if (!ul) return;
        const li = document.createElement('li');
        li.style.marginBottom = '4px';
        li.innerText = msg;
        ul.appendChild(li);
        ul.scrollTop = ul.scrollHeight;
        console.log(`[Popn Scraper] ${msg}`);
    }

    const randomSleep = (min, max) => new Promise(resolve => setTimeout(resolve, Math.floor(Math.random() * (max - min + 1) + min)));

    // 토큰 확인
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

    // [핵심 수정] e-amusement 세션 유지를 위한 fetch 옵션
    const fetchOptions = {
        method: 'GET',
        credentials: 'include' 
    };

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

    async function fetchPlayData(siteId) {
        const dataMap = {};
        addLog(`=== [${siteId}] 데이터 수집 시작 ===`);

        // 1. 기본 페이지 로드 (로그인 확인 및 버전 목록 추출)
        const baseUrl = `https://p.eagate.573.jp/game/popn/${siteId}/playdata/mu_top.html`;
        const initRes = await fetch(baseUrl, fetchOptions);
        const initHtml = await initRes.text();

        if (initHtml.includes("login_form") || initHtml.includes("ea_common_login")) {
            addLog(`[에러] ${siteId} 세션이 만료되었습니다. 사이트에 로그인해주세요.`);
            throw new Error("로그인 세션 만료");
        }

        const parser = new DOMParser();
        const initDoc = parser.parseFromString(initHtml, "text/html");

        const versionSelect = initDoc.querySelector('select#s_version');
        if (!versionSelect) {
            addLog(`[에러] ${siteId} 버전 선택창을 찾을 수 없습니다.`);
            throw new Error("버전 선택창 누락");
        }

        // "ALL" 항목을 제외한 모든 버전 value 동적 추출
        const versions = Array.from(versionSelect.querySelectorAll('option'))
            .filter(opt => opt.innerText.trim() !== 'ALL' && opt.value !== "-1")
            .map(opt => ({ val: opt.value, text: opt.innerText.trim() }));

        addLog(`[${siteId}] 총 ${versions.length}개의 버전을 순회합니다.`);

        for (const vObj of versions) {
            const v = vObj.val;
            let page = 0;
            let maxPage = 0;
            let verSongCount = 0;
            let verChartCount = 0;

            while (page <= maxPage) {
                const pageUrl = `${baseUrl}?page=${page}&version=${v}&category=0&keyword=&sort=none&sort_type=none&_t=${Date.now()}`;
                const res = await fetch(pageUrl, fetchOptions);
                const html = await res.text();
                const doc = parser.parseFromString(html, "text/html");

                // 첫 페이지에서 최대 페이지 수 파악
                if (page === 0) {
                    const pageSelect = doc.querySelector('select#s_page');
                    if (pageSelect) {
                        const options = pageSelect.querySelectorAll('option');
                        if (options.length > 0) {
                            maxPage = parseInt(options[options.length - 1].value, 10);
                        }
                    }
                }

                const items = Array.from(doc.querySelectorAll('ul.mu_list_table > li')).filter(li =>
                    !li.classList.contains('st_th') && !li.querySelector('.th_music') && li.querySelector('.col_music a')
                );

                for (const li of items) {
                    const titleEl = li.querySelector('.col_music a');
                    if (!titleEl) continue;
                    const title = titleEl.innerText.trim();

                    // 곡명 실시간 출력 업데이트
                    updateStatus(`[${siteId}] ${vObj.text} (${page+1}/${maxPage+1}p)\n▶ 탐색 중: ${title}`);

                    const diffs = [
                        { name: 'light', col: '.col_5' },
                        { name: 'normal', col: '.col_normal' },
                        { name: 'hyper', col: '.col_hyper' },
                        { name: 'ex', col: '.col_ex' }
                    ];

                    let songHasData = false;

                    diffs.forEach(diff => {
                        const colNode = li.querySelector(diff.col);
                        if (colNode) {
                            const img = colNode.querySelector('img:nth-child(1)');
                            const scoreText = colNode.innerText.replace(/[^0-9]/g, '');
                            const score = scoreText ? parseInt(scoreText, 10) : 0;
                            const medalSrc = img ? img.src : '';
                            const medal = getMedalName(medalSrc);

                            if (score > 0 || medal !== '') {
                                dataMap[`${normalizeTitle(title)}_${diff.name}`] = { score, medal };
                                verChartCount++;
                                songHasData = true;
                            }
                        }
                    });

                    if (songHasData) verSongCount++;
                }

                page++;
                if (page <= maxPage) {
                    await randomSleep(400, 800);
                }
            }
            
            if (verSongCount > 0) {
                addLog(`[${siteId}] ${vObj.text} 완료: ${verSongCount}곡 / ${verChartCount}개 패턴 확인됨`);
            }
            await randomSleep(500, 1000); // 버전 간 과부하 방지
        }
        return dataMap;
    }

    try {
        updateStatus("GitHub에서 기존 CSV를 불러옵니다...");
        const apiUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/contents/${FILE_PATH}?ref=main`;
        
        const fileRes = await fetch(apiUrl, {
            headers: { 'Authorization': `token ${token}`, 'Cache-Control': 'no-cache' }
        });
        
        if (!fileRes.ok) {
            updateStatus(`GitHub CSV 로드 실패 (${fileRes.status})`);
            if (fileRes.status === 401 || fileRes.status === 403) {
                localStorage.removeItem('GITHUB_PAT');
                addLog("권한 에러로 인해 저장된 토큰이 초기화되었습니다.");
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

        addLog("기존 CSV 데이터 로드 성공");

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

        addLog(`총 ${updateCount}개의 채보 점수가 업데이트되었습니다.`);
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
                message: `Update play data via Bookmarklet (${new Date().toLocaleDateString()}) - ${updateCount} charts updated`,
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