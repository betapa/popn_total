(async () => {
  if (document.getElementById('popn-scraper-ui')) return;

  const REPO_OWNER = 'betapa';
  const REPO_NAME = 'popn_total';
  const FILE_PATH = 'popn_score_scraper/popn_playdata_hc.csv';

  const BASE_PATH = '/game/popn/popn29/playdata/mu_top.html';
  const VERSION_MIN = 0;
  const VERSION_MAX = 29;
  const PAGE_SIZE = 25;
  const MAX_PAGES = 500;          // 무한 루프 방지용 상한
  const DELAY_MIN = 800;
  const DELAY_MAX = 1500;
  const MAX_RETRY = 3;
  const DIFFS = ['Big', 'Light', 'Normal', 'Hyper', 'Ex'];

  // ============================================================
  // [설정] 어느 버전(0~29)에도 속하지 않는 곡 수집
  //  - 전체 목록(version=-1)을 풀 스캔한 뒤, 0~29 에서 수집되지 않은 곡만 Version=-1 로 추가
  //  - 전체 탐색용 경로는 플레이데이터 페이지(mu_top.html)를 사용한다.
  //    (music/list.html 은 곡 목록 전용 페이지라 메달/스코어가 없음)
  // ============================================================
  const FULL_SCAN_ENABLED = true;
  const FULL_SCAN_PATH = BASE_PATH;
  const FULL_SCAN_VERSION_PARAM = -1;   // 요청 URL 의 version 값
  const OTHER_VERSION = -1;             // CSV 의 Version 컬럼에 기록할 값 (-1 또는 99)

  const MEDAL_MAP = {
    'meda_none': '',
    'meda_a': '금☆',
    'meda_b': '은☆',
    'meda_c': '은◇',
    'meda_d': '은○',
    'meda_e': '동☆',
    'meda_f': '동◇',
    'meda_g': '동○',
    'meda_h': '흑☆',
    'meda_i': '흑◇',
    'meda_j': '흑○',
    'meda_k': '이지'
  };

  const ui = document.createElement('div');
  ui.id = 'popn-scraper-ui';
  ui.style.cssText = 'position:fixed;top:20px;right:20px;padding:20px;background:rgba(0,0,0,0.85);color:#fff;z-index:99999;border-radius:10px;font-family:sans-serif;box-shadow:0 4px 6px rgba(0,0,0,0.3);max-height:80vh;overflow-y:auto;width:380px;';
  ui.innerHTML = '<h3 style="margin:0 0 10px 0;color:#fff;font-size:16px;">팝픈 스코어 스크래퍼 (HC)</h3><p id="popn-scraper-status" style="margin:0;font-size:13px;word-break:keep-all;font-weight:bold;color:#4ade80;">초기화 중...</p><ul id="popn-scraper-log" style="margin-top:10px;padding-left:15px;font-size:11px;color:#ccc;max-height:250px;overflow-y:auto;list-style-type:square;"></ul>';
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

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const randomDelay = () => sleep(DELAY_MIN + Math.random() * (DELAY_MAX - DELAY_MIN));
  const log = (...a) => console.log('[popn-scraper-HC]', ...a);
  const warn = (...a) => console.warn('[popn-scraper-HC]', ...a);
  const unknownMedals = new Set();

  function medalFromImg(img) {
    if (!img) return '';
    const m = (img.getAttribute('src') || '').match(/medal\/([^/.]+)\.png/);
    if (!m) return '';
    const key = m[1];
    if (key in MEDAL_MAP) return MEDAL_MAP[key];
    unknownMedals.add(key);
    return key;
  }

  const csvCell = (v) => {
    const s = String(v ?? '');
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };

  function utf8ToBase64(str) {
    return btoa(encodeURIComponent(str).replace(/%([0-9A-F]{2})/g, function(match, p1) {
      return String.fromCharCode('0x' + p1);
    }));
  }

  // 곡 식별 키 (SongID 우선, 없으면 제목|장르|아티스트)
  const rowKey = (r) => r.SongID || [r.Title, r.Genre, r.Artist].join('|');

  async function fetchPage(path, version, page) {
    const url = `${path}?page=${page}&version=${version}&bemani=0&category=0&keyword=&sort=music&sort_type=up`;
    for (let attempt = 1; attempt <= MAX_RETRY; attempt++) {
      try {
        const res = await fetch(url, { credentials: 'include' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        if (res.redirected && /login/i.test(res.url)) throw new Error('LOGIN_LOST');
        
        const html = await res.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const hasList = !!doc.querySelector('ul.mu_list_table');
        if (!hasList && /ログイン|login_form|login\.html/.test(html)) throw new Error('LOGIN_LOST');
        
        return doc;
      } catch (e) {
        if (e.message === 'LOGIN_LOST') throw e;
        warn(`요청 실패 (v${version} p${page}, 시도 ${attempt}/${MAX_RETRY}):`, e.message);
        if (attempt === MAX_RETRY) throw e;
        await sleep(2000 * attempt);
      }
    }
  }

  function parseRows(doc, versionTag) {
    const rows = [];
    const items = doc.querySelectorAll('ul.mu_list_table > li:not(.st_th)');
    items.forEach((li, idx) => {
      try {
        const cols = li.children;
        if (cols.length < 1 + DIFFS.length) throw new Error('열 개수 부족: ' + cols.length);

        const a = cols[0].querySelector('a');
        const ps = cols[0].querySelectorAll('p');
        const title = a ? a.textContent.trim() : '';
        const genre = ps[0] ? ps[0].textContent.trim() : '';
        const artist = ps[1] ? ps[1].textContent.trim() : '';
        let songId = '';
        try { songId = new URL(a.getAttribute('href'), location.origin).searchParams.get('no') || ''; } catch (_) {}

        const row = { Version: versionTag, SongID: songId, Title: title, Genre: genre, Artist: artist };

        DIFFS.forEach((d, i) => {
          try {
            const cell = cols[i + 1];
            const medal = medalFromImg(cell.querySelector('img[src*="/medal/meda"]'));
            const txt = (cell.querySelector('p')?.textContent || '').trim();
            const score = /^\d+$/.test(txt) ? parseInt(txt, 10) : '';
            row[`${d}_Medal`] = medal;
            row[`${d}_Score`] = score;
          } catch (e) {
            warn(`난이도 파싱 오류 (v${versionTag}, ${title}, ${d}):`, e.message);
            row[`${d}_Medal`] = '';
            row[`${d}_Score`] = '';
          }
        });
        rows.push(row);
      } catch (e) {
        warn(`곡 파싱 오류 (v${versionTag}, 행 ${idx}) -> 건너뜀:`, e.message);
      }
    });
    return { rows, rawCount: items.length };
  }

  function readMaxPage(doc) {
    const pageSelect = doc.querySelector('select#s_page');
    if (!pageSelect) return null;   // 페이지 수를 알 수 없음 -> 곡 수(PAGE_SIZE)로 종료 판단
    const nums = Array.from(pageSelect.querySelectorAll('option'))
      .map((o) => parseInt(o.value, 10))
      .filter((n) => !isNaN(n));
    return nums.length ? Math.max(...nums) : null;
  }

  // 한 목록(버전 또는 전체)의 모든 페이지를 순회
  async function scanList(path, versionParam, versionTag, label) {
    const out = [];
    const seen = new Set();
    let maxPage = null;

    for (let p = 0; p < MAX_PAGES; p++) {
      updateStatus(`[popn29] ${label} (${p + 1}p) 탐색 중...`);
      const doc = await fetchPage(path, versionParam, p);
      if (p === 0) maxPage = readMaxPage(doc);

      const { rows, rawCount } = parseRows(doc, versionTag);
      const fresh = rows.filter((r) => {
        const k = rowKey(r);
        if (seen.has(k)) return false;
        seen.add(k);
        return true;
      });
      out.push(...fresh);

      if (rows.length > 0) addLog(`${label} / ${p}p: ${rawCount}곡 처리됨`);

      await randomDelay();
      if (rawCount < PAGE_SIZE) break;
      if (maxPage !== null && p >= maxPage) break;
      if (rawCount > 0 && fresh.length === 0) {
        addLog(`${label}: 이미 수집한 곡만 반복되어 종료합니다.`);
        break;
      }
    }
    return out;
  }

  async function uploadToGitHub(allRecords, token) {
    updateStatus(`CSV 텍스트 생성 중...`);
    const headers = ['Version', 'SongID', 'Title', 'Genre', 'Artist'];
    DIFFS.forEach((d) => headers.push(`${d}_Medal`, `${d}_Score`));
    
    let csvContent = headers.join(',') + '\n';
    allRecords.forEach(r => {
      csvContent += headers.map(h => csvCell(r[h])).join(',') + '\n';
    });

    const encodedContent = utf8ToBase64('\uFEFF' + csvContent);
    const apiUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/contents/${FILE_PATH}`;

    let sha;
    const apiHeaders = {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json"
    };

    try {
      updateStatus("GitHub에서 기존 CSV 구조를 조회합니다...");
      const getRes = await fetch(apiUrl, {
        method: "GET",
        headers: apiHeaders
      });

      if (getRes.status === 200) {
        const data = await getRes.json();
        if (!data.sha) {
          throw new Error("GitHub 응답에 파일 SHA가 없습니다.");
        }
        sha = data.sha;
        addLog(`기존 파일 SHA 조회 성공: ${sha}`);
      } else if (getRes.status === 404) {
        sha = undefined;
        addLog("기존 파일이 없습니다. 새 파일을 생성합니다.");
      } else {
        const errorText = await getRes.text();
        throw new Error(`SHA 조회 실패: HTTP ${getRes.status} - ${errorText}`);
      }
    } catch (e) {
      console.error("[popn-scraper-HC] SHA 조회 실패:", e);
      alert(`GitHub 파일 조회 실패:\n${e.message}`);
      setTimeout(() => document.body.removeChild(ui), 5000);
      return;
    }

    updateStatus("GitHub로 갱신된 파일을 전송 중...");
    
    const body = {
      message: `Update HC play data (${new Date().toLocaleDateString()}) - ${allRecords.length} songs`,
      content: encodedContent
    };

    if (sha) {
      body.sha = sha;
    }

    const putRes = await fetch(apiUrl, {
      method: "PUT",
      headers: {
        ...apiHeaders,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    });

    if (putRes.ok) {
      updateStatus("완료! GitHub에 성공적으로 저장되었습니다.");
      alert(`✅ 성공적으로 깃허브에 저장되었습니다!\n(${allRecords.length}곡 수집 완료)`);
    } else {
      const errTxt = await putRes.text();
      updateStatus(`저장 실패: HTTP ${putRes.status}`);
      alert(`❌ 업로드 실패: HTTP ${putRes.status}\n${errTxt}`);
      if (putRes.status === 401 || putRes.status === 403) localStorage.removeItem('GITHUB_PAT');
    }
    setTimeout(() => document.body.removeChild(ui), 5000);
  }

  const all = [];
  try {
    let token = localStorage.getItem('GITHUB_PAT');
    if (!token) {
      token = prompt("GitHub Personal Access Token (PAT)을 입력하세요:\n(repo 권한 필요, 이 브라우저에 저장됩니다)");
      if (!token) {
        alert("토큰이 없어 작업을 취소합니다.");
        document.body.removeChild(ui);
        return;
      }
      localStorage.setItem('GITHUB_PAT', token);
    }

    // [1단계] 버전별(0~29) 수집
    const known = new Set();
    for (let v = VERSION_MIN; v <= VERSION_MAX; v++) {
      const rows = await scanList(BASE_PATH, v, v, `Ver.${v}`);
      rows.forEach((r) => known.add(rowKey(r)));
      all.push(...rows);
    }
    addLog(`버전별 수집 완료: ${all.length}곡`);

    // [2단계] 전체 풀 스캔 -> 어느 버전에도 속하지 않는 곡만 추가
    if (FULL_SCAN_ENABLED) {
      try {
        const full = await scanList(FULL_SCAN_PATH, FULL_SCAN_VERSION_PARAM, OTHER_VERSION, '전체(버전 미분류 탐색)');
        if (full.length === 0) {
          throw new Error('전체 탐색 결과가 비어 있습니다. (version=-1 미지원 또는 페이지 구조 불일치)');
        }
        const others = full.filter((r) => !known.has(rowKey(r)));
        addLog(`전체 ${full.length}곡 중 버전 미분류 ${others.length}곡 발견`);
        others.forEach((r) => addLog(`  기타: ${r.Title} (ID ${r.SongID})`));
        all.push(...others);
      } catch (e) {
        if (e.message === 'LOGIN_LOST') throw e;
        warn('전체 탐색 실패:', e);
        addLog(`전체 탐색 실패: ${e.message}`);
        // 실패한 채로 업로드하면 이전에 저장된 '기타' 곡이 CSV 에서 사라질 수 있으므로 사용자에게 확인
        if (!confirm(`전체 탐색에 실패했습니다.\n${e.message}\n\n버전 미분류 곡 없이 현재까지 수집한 ${all.length}곡만 업로드할까요?\n(취소하면 업로드하지 않고 종료합니다)`)) {
          throw new Error('전체 탐색 실패로 사용자가 업로드를 취소했습니다.');
        }
      }
    }

    addLog('전체 탐색 완료. 업로드를 시작합니다.');
    
    if (all.length > 0) {
      await uploadToGitHub(all, token);
    } else {
      alert("수집된 데이터가 없습니다.");
      document.body.removeChild(ui);
    }
    
  } catch (e) {
    if (e.message === 'LOGIN_LOST') {
      alert('로그인이 풀렸습니다. 재로그인 후 다시 실행하세요.');
    } else {
      alert(`수집 중 오류 발생: ${e.message}`);
    }
    document.body.removeChild(ui);
  } finally {
    if (unknownMedals.size) warn('MEDAL_MAP에 없는 메달 파일명 발견:', [...unknownMedals]);
  }
})();