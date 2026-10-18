(async () => {
  // ============================================================
  // [설정] 필요 시 이 부분만 수정하세요
  // ============================================================
  const REPO_OWNER = 'betapa';         // 깃허브 계정명
  const REPO_NAME = 'popn_total';      // 레포지토리 이름
  const FILE_PATH = 'popn_score_scraper/popn_playdata_hc.csv'; // 저장할 깃허브 경로

  const BASE_PATH = '/game/popn/popn29/playdata/mu_top.html';
  const VERSION_MIN = 0;
  const VERSION_MAX = 29;
  const PAGE_SIZE = 25;
  const DELAY_MIN = 800;
  const DELAY_MAX = 1500;
  const MAX_RETRY = 3;
  const DIFFS = ['Big', 'Light', 'Normal', 'Hyper', 'Ex'];

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
  // [유틸] 딜레이 / 로그 / 파싱 보조 함수
  // ============================================================
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
    unknownMedals.add(key); // 미등록 메달은 파일명 그대로 기록
    return key;
  }

  const csvCell = (v) => {
    const s = String(v ?? '');
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };

  // UTF-8 문자열을 Base64로 인코딩 (GitHub API 요구사항)
  function utf8ToBase64(str) {
    return btoa(encodeURIComponent(str).replace(/%([0-9A-F]{2})/g, function(match, p1) {
      return String.fromCharCode('0x' + p1);
    }));
  }

  // ============================================================
  // [1단계] 페이지 요청 (재시도 + 로그인 풀림 감지 포함)
  // ============================================================
  async function fetchPage(version, page) {
    const url = `${BASE_PATH}?page=${page}&version=${version}&bemani=0&category=0&keyword=&sort=music&sort_type=up`;
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

  // ============================================================
  // [2단계] 한 페이지의 HTML 파싱 -> 곡 배열
  // ============================================================
  function parseRows(doc, version) {
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

        const row = { Version: version, SongID: songId, Title: title, Genre: genre, Artist: artist };

        DIFFS.forEach((d, i) => {
          try {
            const cell = cols[i + 1];
            const medal = medalFromImg(cell.querySelector('img[src*="/medal/meda"]'));
            const txt = (cell.querySelector('p')?.textContent || '').trim();
            const score = /^\d+$/.test(txt) ? parseInt(txt, 10) : ''; 
            row[`${d}_Medal`] = medal;
            row[`${d}_Score`] = score;
          } catch (e) {
            warn(`난이도 파싱 오류 (v${version}, ${title}, ${d}):`, e.message);
            row[`${d}_Medal`] = '';
            row[`${d}_Score`] = '';
          }
        });
        rows.push(row);
      } catch (e) {
        warn(`곡 파싱 오류 (v${version}, 행 ${idx}) -> 건너뜀:`, e.message);
      }
    });
    return { rows, rawCount: items.length };
  }

  // ============================================================
  // [3단계] CSV 텍스트 생성 및 GitHub 업로드
  // ============================================================
  async function uploadToGitHub(allRecords, token) {
    log(`CSV 텍스트 생성 중...`);
    const headers = ['Version', 'SongID', 'Title', 'Genre', 'Artist'];
    DIFFS.forEach((d) => headers.push(`${d}_Medal`, `${d}_Score`));
    
    let csvContent = headers.join(',') + '\n';
    allRecords.forEach(r => {
      csvContent += headers.map(h => csvCell(r[h])).join(',') + '\n';
    });

    log(`GitHub 업로드 준비 중... (총 ${allRecords.length}곡)`);
    const apiUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/contents/${FILE_PATH}`;

    let sha = "";
    
    // [🔥 핵심 수정 1] 기존 파일의 SHA 값 가져오기 (CORS 에러 방지)
    try {
      const getRes = await fetch(apiUrl, {
        method: 'GET',
        cache: 'no-store', // 헤더에서 Cache-Control을 제거하고 fetch 옵션 사용
        headers: { 
          'Authorization': `token ${token}`,
          'Accept': 'application/vnd.github+json'
        }
      });
      
      if (getRes.ok) {
        const getData = await getRes.json();
        sha = getData.sha;
      } else if (getRes.status === 404) {
        log("기존 파일이 존재하지 않습니다. 새 파일로 생성합니다.");
      } else {
        throw new Error(`HTTP ${getRes.status}`);
      }
    } catch(e) {
      // [🔥 핵심 수정 2] SHA 조회 실패 시 진행 중단 (422 에러 방지)
      console.error("기존 파일 SHA 조회 실패:", e);
      alert(`❌ GitHub 파일 조회 실패!\n에러: ${e.message}\nCORS 또는 토큰 문제일 수 있습니다.`);
      return; 
    }

    const encodedContent = utf8ToBase64('\uFEFF' + csvContent); // 한글 깨짐 방지 BOM 추가

    const putRes = await fetch(apiUrl, {
      method: 'PUT',
      headers: {
        'Authorization': `token ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        message: `Update HC play data (${new Date().toLocaleDateString()}) - ${allRecords.length} songs`,
        content: encodedContent,
        sha: sha ? sha : undefined
      })
    });

    if (putRes.ok) {
      alert(`✅ 성공적으로 깃허브에 저장되었습니다!\n(${allRecords.length}곡 수집 완료)`);
      log('업로드 완료!');
    } else {
      const errTxt = await putRes.text();
      alert(`❌ 업로드 실패: HTTP ${putRes.status}\n${errTxt}`);
      if (putRes.status === 401 || putRes.status === 403) localStorage.removeItem('GITHUB_PAT');
    }
  }

  // ============================================================
  // [메인] 탐색 실행 루프
  // ============================================================
  const all = [];
  try {
    // 0. 토큰 확인
    let token = localStorage.getItem('GITHUB_PAT');
    if (!token) {
      token = prompt("GitHub Personal Access Token (PAT)을 입력하세요:\n(repo 권한 필요, 이 브라우저에 저장됩니다)");
      if (!token) return alert("토큰이 없어 취소합니다.");
      localStorage.setItem('GITHUB_PAT', token);
    }

    for (let v = VERSION_MIN; v <= VERSION_MAX; v++) {
      for (let p = 0; ; p++) {
        const doc = await fetchPage(v, p);
        const { rows, rawCount } = parseRows(doc, v);
        all.push(...rows);
        log(`버전 ${v} / 페이지 ${p}: ${rawCount}곡 (누적 ${all.length}곡)`);
        
        await randomDelay(); // 서버 부하 및 봇 탐지 방지
        if (rawCount < PAGE_SIZE) break; 
      }
    }
    log('전체 탐색 완료. 업로드를 시작합니다.');
    
    // 업로드 함수 호출
    if (all.length > 0) {
      await uploadToGitHub(all, token);
    } else {
      alert("수집된 데이터가 없습니다.");
    }
    
  } catch (e) {
    if (e.message === 'LOGIN_LOST') {
      alert('로그인이 풀렸습니다. 재로그인 후 다시 실행하세요.');
    } else {
      alert(`수집 중 오류 발생: ${e.message}`);
    }
  } finally {
    if (unknownMedals.size) warn('MEDAL_MAP에 없는 메달 파일명 발견:', [...unknownMedals]);
  }
})();