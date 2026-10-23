(async () => {
  if (document.getElementById('popn-scraper-ui')) return;

  // ============================================================
  // [설정] 필요 시 이 부분만 수정하세요
  // ============================================================
  const REPO_OWNER = 'betapa';         // 깃허브 계정명
  const REPO_NAME = 'popn_total';      // 레포지토리 이름
  const FILE_PATH = 'popn_score_scraper/popn_playdata_jf.csv'; // 저장할 깃허브 경로

  const BASE_PATH = '/game/popn/jamfizz/playdata/mu_top.html';
  const VERSION_MIN = 1;  // jamfizz는 1부터 시작
  const VERSION_MAX = 28; // jamfizz의 마지막 버전은 28
  const PAGE_SIZE = 25;
  const MAX_PAGES = 500;  // 무한 루프 방지용 상한
  const DELAY_MIN = 800;
  const DELAY_MAX = 1500;
  const MAX_RETRY = 3;

  // CS 버전 (어느 버전에도 속하지 않아 category=2 로만 조회됨) -> CSV 의 Version 은 0
  const CS_ENABLED = true;
  const CS_CATEGORY = 2;
  const CS_VERSION_PARAM = 0;   // 요청 URL 의 version 값
  const CS_VERSION_TAG = 0;     // CSV 의 Version 컬럼 값

  // 전체 풀 스캔 (version=0, category=0) 후, 위 수집에서 빠진 곡만 추가 -> CSV 의 Version 은 -1
  const FULL_SCAN_ENABLED = true;
  const FULL_SCAN_VERSION_PARAM = 0;
  const OTHER_VERSION_TAG = -1; // -1 또는 99
  
  // JF 특화 난이도 선택자 (Easy 존재)
  const DIFFS = [
    { name: 'Easy',   sel: '.col_5' },
    { name: 'Normal', sel: '.col_normal' },
    { name: 'Hyper',  sel: '.col_hyper' },
    { name: 'Ex',     sel: '.col_ex' },
  ];

  // 메달 이미지 파일명 -> 텍스트 변환표
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

  // ============================================================
  // [UI] 실시간 진행 상황 오버레이
  // ============================================================
  const ui = document.createElement('div');
  ui.id = 'popn-scraper-ui';
  ui.style.cssText = 'position:fixed;top:20px;right:20px;padding:20px;background:rgba(0,0,0,0.85);color:#fff;z-index:99999;border-radius:10px;font-family:sans-serif;box-shadow:0 4px 6px rgba(0,0,0,0.3);max-height:80vh;overflow-y:auto;width:380px;';
  ui.innerHTML = '<h3 style="margin:0 0 10px 0;color:#fff;font-size:16px;">팝픈 스코어 스크래퍼 (JF)</h3><p id="popn-scraper-status" style="margin:0;font-size:13px;word-break:keep-all;font-weight:bold;color:#4ade80;">초기화 중...</p><ul id="popn-scraper-log" style="margin-top:10px;padding-left:15px;font-size:11px;color:#ccc;max-height:250px;overflow-y:auto;list-style-type:square;"></ul>';
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
    console.log(`[Popn Scraper JF] ${msg}`);
  }

  // ============================================================
  // [유틸] 보조 함수
  // ============================================================
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const randomDelay = () => sleep(DELAY_MIN + Math.random() * (DELAY_MAX - DELAY_MIN));
  const warn = (...a) => console.warn('[Popn Scraper JF]', ...a);
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

  // GitHub 토큰 체크 및 저장
  let token = localStorage.getItem('GITHUB_PAT');
  if (!token) {
    token = prompt("GitHub Personal Access Token (PAT)을 입력하세요:\n(이 브라우저에 저장됩니다)");
    if (!token) {
      alert("토큰이 없어 작업을 취소합니다.");
      document.body.removeChild(ui);
      return;
    }
    localStorage.setItem('GITHUB_PAT', token);
  }

  // ============================================================
  // [1단계] 페이지 요청 함수 (category 지정 가능)
  // ============================================================
  async function fetchPage(version, category, page) {
    const url = `${BASE_PATH}?page=${page}&version=${version}&category=${category}&keyword=&sort=none&sort_type=none&_t=${Date.now()}`;
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
        warn(`요청 실패 (v${version} c${category} p${page}, 시도 ${attempt}/${MAX_RETRY}):`, e.message);
        if (attempt === MAX_RETRY) throw e;
        await sleep(2000 * attempt);
      }
    }
  }

  function readMaxPage(doc) {
    const pageSelect = doc.querySelector('select#s_page');
    if (!pageSelect) return null;   // 페이지 수를 알 수 없음 -> 곡 수(PAGE_SIZE)로 종료 판단
    const nums = Array.from(pageSelect.querySelectorAll('option'))
      .map((o) => parseInt(o.value, 10))
      .filter((n) => !isNaN(n));
    return nums.length ? Math.max(...nums) : null;
  }

  // ============================================================
  // [2단계] 한 목록(버전 / CS / 전체)의 모든 페이지를 순회
  //   versionParam, category : 요청 URL 값
  //   versionTag             : CSV 의 Version 컬럼에 기록할 값
  // ============================================================
  async function scanList(versionParam, category, versionTag, label) {
    const out = [];
    const seen = new Set();
    let maxPage = null;
    let scoreCount = 0;

    for (let p = 0; p < MAX_PAGES; p++) {
      const doc = await fetchPage(versionParam, category, p);
      if (p === 0) maxPage = readMaxPage(doc);

      // 헤더(.st_th, .th_music) 등을 제외하고 실제 곡이 담긴 행만 추출
      const items = [...doc.querySelectorAll('ul.mu_list_table > li')].filter((li) => li.querySelector('.col_music'));
      let freshCount = 0;

      for (const li of items) {
        const music = li.querySelector('.col_music');
        const a = music.querySelector('a');
        const infoDivs = music.querySelectorAll('div');
        
        const title = a ? a.textContent.trim() : '';
        const genre = infoDivs[0] ? infoDivs[0].textContent.trim() : '';
        const artist = infoDivs[1] ? infoDivs[1].textContent.trim() : '';
        let songId = '';
        try { songId = new URL(a.getAttribute('href'), location.origin).searchParams.get('no') || ''; } catch (_) {}

        const row = { Version: versionTag, SongID: songId, Title: title, Genre: genre, Artist: artist };
        const key = rowKey(row);
        if (seen.has(key)) continue;
        seen.add(key);
        freshCount++;

        // 실시간 상태 업데이트
        updateStatus(`[jamfizz] ${label} (${p + 1}${maxPage !== null ? '/' + (maxPage + 1) : ''}p)\n▶ 탐색 중: ${title}`);

        let hasScore = false;
        DIFFS.forEach((d) => {
          try {
            const cell = li.querySelector(d.sel);
            if (!cell) throw new Error('셀 없음: ' + d.sel);
            
            const medal = medalFromImg(cell.querySelector('img[src*="/medal/meda"]'));
            const txt = cell.textContent.trim();
            const score = /^\d+$/.test(txt) ? parseInt(txt, 10) : ''; 
            
            row[`${d.name}_Medal`] = medal;
            row[`${d.name}_Score`] = score;
            
            if (score > 0 || medal) hasScore = true;
          } catch (e) {
            warn(`난이도 파싱 오류 (${label}, ${title}, ${d.name}):`, e.message);
            row[`${d.name}_Medal`] = '';
            row[`${d.name}_Score`] = '';
          }
        });

        out.push(row);
        if (hasScore) scoreCount++;
      }

      addLog(`${label} / ${p + 1}p: ${items.length}곡 처리됨`);
      await randomDelay();

      // 한 페이지 곡 수가 25개 미만이거나 현재 페이지가 최대 페이지에 도달하면 루프 종료
      if (items.length < PAGE_SIZE) break;
      if (maxPage !== null && p >= maxPage) break;
      if (items.length > 0 && freshCount === 0) {
        addLog(`${label}: 이미 수집한 곡만 반복되어 종료합니다.`);
        break;
      }
    }
    addLog(`${label} 완료: 총 ${out.length}곡 (점수 발견 ${scoreCount}곡)`);
    return out;
  }

  // ============================================================
  // [3단계] CSV 생성 및 GitHub 업로드 함수
  // ============================================================
  async function uploadToGitHub(allRecords, token) {
    updateStatus(`CSV 텍스트 생성 중...`);
    const headers = ['Version', 'SongID', 'Title', 'Genre', 'Artist'];
    DIFFS.forEach((d) => headers.push(`${d.name}_Medal`, `${d.name}_Score`));
    
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
      updateStatus("GitHub에서 기존 파일 정보를 조회합니다...");
      // CORS 사전요청(Preflight) 우회를 위해 Cache-Control 헤더 제거하고 fetch 옵션으로 적용
      const getRes = await fetch(apiUrl, {
        method: "GET",
        cache: "no-store",
        headers: apiHeaders
      });

      if (getRes.status === 200) {
        const data = await getRes.json();
        if (!data.sha) throw new Error("GitHub 응답에 파일 SHA가 없습니다.");
        sha = data.sha;
        addLog(`기존 파일 조회 성공 (SHA: ${sha.substring(0,6)}...)`);
      } else if (getRes.status === 404) {
        sha = undefined;
        addLog("기존 파일이 없습니다. 새 파일을 생성합니다.");
      } else {
        const errorText = await getRes.text();
        throw new Error(`SHA 조회 실패: HTTP ${getRes.status} - ${errorText}`);
      }
    } catch (e) {
      console.error("[popn-scraper-JF] SHA 조회 실패:", e);
      alert(`❌ GitHub 파일 조회 실패!\n에러: ${e.message}\nCORS 또는 토큰 문제일 수 있습니다.`);
      setTimeout(() => document.body.removeChild(ui), 5000);
      return;
    }

    updateStatus("GitHub로 갱신된 파일을 전송 중...");
    
    const body = {
      message: `Update JF play data (${new Date().toLocaleDateString()}) - ${allRecords.length} songs`,
      content: encodedContent
    };

    if (sha) body.sha = sha;

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
      alert(`✅ 성공적으로 깃허브에 저장되었습니다!\n(총 ${allRecords.length}곡)`);
    } else {
      const errTxt = await putRes.text();
      updateStatus(`저장 실패: HTTP ${putRes.status}`);
      alert(`❌ 업로드 실패: HTTP ${putRes.status}\n${errTxt}`);
      if (putRes.status === 401 || putRes.status === 403) localStorage.removeItem('GITHUB_PAT');
    }
    setTimeout(() => document.body.removeChild(ui), 5000);
  }

  // 추가 수집 단계(CS/전체) 실패 시: 사용자가 동의해야만 해당 곡 없이 업로드를 계속한다.
  // (그대로 올리면 이전에 저장된 CS/기타 곡이 CSV 에서 사라질 수 있음)
  function confirmContinue(stepName, e, count) {
    warn(`${stepName} 실패:`, e);
    addLog(`${stepName} 실패: ${e.message}`);
    if (!confirm(`${stepName}에 실패했습니다.\n${e.message}\n\n해당 곡 없이 현재까지 수집한 ${count}곡만 업로드할까요?\n(취소하면 업로드하지 않고 종료합니다)`)) {
      throw new Error(`${stepName} 실패로 사용자가 업로드를 취소했습니다.`);
    }
  }

  // ============================================================
  // [메인] 탐색 실행 루프
  // ============================================================
  const all = [];
  try {
    // [A] 버전별(1~28) 수집
    const known = new Set();
    for (let v = VERSION_MIN; v <= VERSION_MAX; v++) {
      const rows = await scanList(v, 0, v, `Ver.${v}`);
      rows.forEach((r) => known.add(rowKey(r)));
      all.push(...rows);
    }
    addLog(`버전별 수집 완료: ${all.length}곡`);

    // [B] CS 버전 (category=2) -> Version 0
    if (CS_ENABLED) {
      try {
        const cs = await scanList(CS_VERSION_PARAM, CS_CATEGORY, CS_VERSION_TAG, 'CS(Ver.0)');
        // 다른 버전에서 이미 수집된 곡은 제외 (SongID 중복 방지 -> 병합 시 행 중복 방지)
        const csNew = cs.filter((r) => !known.has(rowKey(r)));
        addLog(`CS ${cs.length}곡 중 신규 ${csNew.length}곡 추가 (중복 ${cs.length - csNew.length}곡 제외)`);
        csNew.forEach((r) => known.add(rowKey(r)));
        all.push(...csNew);
      } catch (e) {
        if (e.message === 'LOGIN_LOST') throw e;
        confirmContinue('CS 버전 탐색', e, all.length);
      }
    }

    // [C] 전체 풀 스캔 -> 위에서 수집되지 않은 곡만 Version -1 로 추가
    if (FULL_SCAN_ENABLED) {
      try {
        const full = await scanList(FULL_SCAN_VERSION_PARAM, 0, OTHER_VERSION_TAG, '전체(버전 미분류 탐색)');
        if (full.length === 0) {
          throw new Error('전체 탐색 결과가 비어 있습니다. (페이지 구조 불일치 가능)');
        }
        const others = full.filter((r) => !known.has(rowKey(r)));
        addLog(`전체 ${full.length}곡 중 버전 미분류 ${others.length}곡 발견`);
        others.forEach((r) => addLog(`  기타: ${r.Title} (ID ${r.SongID})`));
        all.push(...others);
      } catch (e) {
        if (e.message === 'LOGIN_LOST') throw e;
        confirmContinue('전체 탐색', e, all.length);
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