// ---------------------------------------------------------- 初回ツアー
//
// マニュアルの代わり。初めて開いた端末でだけ、画面の上を順に指して説明する。
// 読んで覚えるより、実物の場所を指したほうが早いので、文書は持たない。
//
// ・見たかどうかは端末ごとに localStorage に覚える（サーバーには送らない）
// ・中身を大きく変えたら ONBOARDING_VERSION を上げる。上げると全員にもう一度出る
// ・ヘッダーの「使い方」からいつでも見直せる
// ・指す先は id だけにする。class で指すと見た目の調整で静かに外れる
//   （tests/test_onboarding.py が、ここの id が index.html にあるかを見る）
// ・色・形は style.css の .tour-* が theme.css のトークンから作る。ここでは触らない

(function () {
  const ONBOARDING_KEY = 'sf.onboarding';
  const ONBOARDING_VERSION = '1';

  const ONBOARDING_STEPS = [
    {
      target: null,
      title: 'StreamForge へようこそ',
      body: '配信先URLを貼って、開始を押す。それだけでテスト送出ができます。<br>'
        + '画面のどこに何があるかを、1分で案内します。',
    },
    {
      target: '#step-target',
      title: '① 送出先 — URLを貼るだけ',
      body: '<code>rtmp://</code> <code>srt://</code> <code>https://</code>(WHIP) を貼ると、'
        + 'プロトコルを自動で判定し、streamid やストリームキーを分解して見せます。<br>'
        + '設定の書き間違い（SRT の latency の単位など）は、貼った時点で警告します。'
        + '「疎通確認」で、送出前に数秒だけ繋がるかを試せます。',
    },
    {
      target: '#step-source',
      title: '② ソース — 何を送るか',
      body: '既定の<b>テストパターン</b>なら、素材を用意しなくても送れます。<br>'
        + '手持ちのファイルをループさせたり、外部のライブ映像（HLS）を中継したりもできます。',
    },
    {
      target: '#step-quality',
      title: '③ 画質 — 既定のままで送れます',
      body: '解像度・ビットレートなどを変えて送出すると、その設定は<b>自動でテンプレートとして保存</b>され、'
        + '次回から上のチップで選べます。<br>'
        + '「オーバーレイ」を開くと、映像にミリ秒の時計と設定値を焼き込めます。'
        + '受信側のスクリーンショット1枚で、遅延と設定がまとめて分かります。',
    },
    {
      target: '#launch',
      title: '送出する',
      body: '<b>送出開始</b>で送り始めます。送出中は状態・fps・ビットレート・エラーの原因説明が下に出ます。<br>'
        + '「コマンドを確認」は実際に流す FFmpeg コマンドを表示するだけ、'
        + '「処理能力を測る」はこのマシンで間に合うかを数秒で測るだけで、どちらも送出はしません。',
    },
    {
      target: '#vault',
      title: '④ 記録と資産',
      body: '過去の送出履歴、保存した送出先、テンプレート、アップロードした素材はここにまとまっています。<br>'
        + '普段は閉じてあり、押すと開きます。',
    },
    {
      target: '#appearance',
      title: '見た目と、この案内',
      body: '画面の配色はここで切り替えられます。<br>'
        + 'この案内は、隣の<b>「使い方」</b>からいつでも見直せます。',
    },
  ];

  let index = 0;
  let nodes = null;          // { scrim, spot, pop, title, body, count, prev, next, skip }
  let lastFocus = null;      // 閉じたらフォーカスを元の場所に返す
  let rafId = 0;

  function seen() {
    try { return localStorage.getItem(ONBOARDING_KEY) === ONBOARDING_VERSION; }
    catch { return false; }
  }

  function markSeen() {
    try { localStorage.setItem(ONBOARDING_KEY, ONBOARDING_VERSION); }
    catch { /* 保存できない端末では、開くたびに出る。閉じれば済むので諦める */ }
  }

  function build() {
    const scrim = document.createElement('div');
    scrim.className = 'tour-scrim';
    // 外側を押しても閉じない。うっかり消えると、もう一度出す手段を知らないまま終わる

    const spot = document.createElement('div');
    spot.className = 'tour-spot';
    spot.setAttribute('aria-hidden', 'true');

    const pop = document.createElement('div');
    pop.className = 'tour-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-modal', 'true');
    pop.setAttribute('aria-labelledby', 'tour-title');
    pop.setAttribute('aria-describedby', 'tour-body');
    pop.innerHTML = `
      <div class="tour-head">
        <h2 id="tour-title"></h2>
        <span class="tour-count muted"></span>
      </div>
      <div id="tour-body" class="tour-body"></div>
      <div class="tour-actions">
        <button type="button" class="ghost tour-skip">閉じる</button>
        <span class="tour-nav">
          <button type="button" class="ghost tour-prev">戻る</button>
          <button type="button" class="primary tour-next">次へ</button>
        </span>
      </div>`;

    document.body.append(scrim, spot, pop);
    nodes = {
      scrim, spot, pop,
      title: pop.querySelector('#tour-title'),
      body: pop.querySelector('#tour-body'),
      count: pop.querySelector('.tour-count'),
      prev: pop.querySelector('.tour-prev'),
      next: pop.querySelector('.tour-next'),
      skip: pop.querySelector('.tour-skip'),
    };
    nodes.prev.addEventListener('click', () => go(index - 1));
    nodes.next.addEventListener('click', () => go(index + 1));
    nodes.skip.addEventListener('click', () => finish());
  }

  function onKey(e) {
    if (!nodes) return;
    if (e.key === 'Escape') { e.preventDefault(); finish(); return; }
    if (e.key === 'ArrowRight') { e.preventDefault(); go(index + 1); return; }
    if (e.key === 'ArrowLeft') { e.preventDefault(); go(index - 1); return; }
    // フォーカスを案内の中に閉じ込める（aria-modal に見合う動きにする）
    if (e.key === 'Tab') {
      const items = [...nodes.pop.querySelectorAll('button:not(.hidden)')];
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }

  function onResize() {
    cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(place);
  }

  function targetOf(step) {
    if (!step.target) return null;
    const node = document.querySelector(step.target);
    // 畳まれている・隠れている場所は指せない。そのときは画面中央で説明だけ出す
    if (!node || node.offsetParent === null) return null;
    return node;
  }

  // 指す先の周りだけ穴を開け、吹き出しを上下の空いているほうに置く
  function place() {
    if (!nodes) return;
    const step = ONBOARDING_STEPS[index];
    const target = targetOf(step);
    const pop = nodes.pop;
    const margin = 12;
    const pad = 6;

    if (!target) {
      nodes.spot.classList.add('hidden');
      nodes.scrim.classList.add('solid');
      pop.classList.add('centered');
      pop.style.top = '';
      pop.style.left = '';
      return;
    }

    nodes.scrim.classList.remove('solid');
    pop.classList.remove('centered');
    nodes.spot.classList.remove('hidden');

    const r = target.getBoundingClientRect();
    Object.assign(nodes.spot.style, {
      top: `${r.top - pad}px`,
      left: `${r.left - pad}px`,
      width: `${r.width + pad * 2}px`,
      height: `${r.height + pad * 2}px`,
    });

    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    const pw = pop.offsetWidth;
    const ph = pop.offsetHeight;

    const below = r.bottom + pad + margin;
    const above = r.top - pad - margin - ph;
    let top;
    if (below + ph <= vh - margin) top = below;
    else if (above >= margin) top = above;
    else top = Math.max(margin, vh - ph - margin);   // 大きい場所は、画面の下端に重ねて置く

    let left = r.left;
    left = Math.min(left, vw - pw - margin);
    left = Math.max(left, margin);

    pop.style.top = `${top}px`;
    pop.style.left = `${left}px`;
  }

  function go(next) {
    if (next < 0) return;
    if (next >= ONBOARDING_STEPS.length) { finish(); return; }
    index = next;
    const step = ONBOARDING_STEPS[index];
    const last = index === ONBOARDING_STEPS.length - 1;

    nodes.title.textContent = step.title;
    nodes.body.innerHTML = step.body;   // 中身はこのファイルに書いた定数だけ。外から入る文字は無い
    nodes.count.textContent = `${index + 1} / ${ONBOARDING_STEPS.length}`;
    nodes.prev.classList.toggle('hidden', index === 0);
    nodes.next.textContent = index === 0 ? '案内を始める' : last ? '使い始める' : '次へ';
    nodes.skip.textContent = index === 0 ? 'あとで' : '閉じる';

    const target = targetOf(step);
    if (target) {
      // 吹き出しを置く余白を残して、指す先を画面に入れる
      const r = target.getBoundingClientRect();
      const fits = r.top >= 80 && r.bottom <= window.innerHeight - 220;
      if (!fits) {
        const behavior = window.matchMedia
          && window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
        target.scrollIntoView({ block: 'center', behavior });
      }
    }
    place();
    nodes.next.focus();
  }

  function start() {
    if (nodes) return;
    lastFocus = document.activeElement;
    build();
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onResize);
    window.addEventListener('scroll', onResize, { passive: true });
    go(0);
  }

  // 「あとで」「閉じる」「Esc」「最後まで」のどれでも、見たことにする。
  // 毎回出ると邪魔になり、見直したい人は「使い方」から開ける
  function finish() {
    if (!nodes) return;
    markSeen();
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', onResize);
    window.removeEventListener('scroll', onResize);
    cancelAnimationFrame(rafId);
    nodes.scrim.remove();
    nodes.spot.remove();
    nodes.pop.remove();
    nodes = null;
    if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
  }

  // 初回だけ出す。監視だけの画面（/jobs/<id>）や送出中の画面では出さない
  function maybeStart({ monitorOnly = false } = {}) {
    if (monitorOnly || seen()) return;
    const monitor = document.getElementById('monitor');
    if (monitor && !monitor.classList.contains('hidden')) return;
    start();
  }

  window.SFOnboarding = { start, maybeStart };
})();
