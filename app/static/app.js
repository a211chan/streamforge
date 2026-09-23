'use strict';

const $ = (id) => document.getElementById(id);
const el = {
  url: $('url'), verdict: $('verdict'), start: $('start'), stop: $('stop'),
  preview: $('preview'), command: $('command'), error: $('error'),
  monitor: $('monitor'), jobId: $('job-id'), jobStatus: $('job-status'),
  timeline: $('timeline'), caps: $('caps'), history: $('history').querySelector('tbody'),
  params: $('params'), warnings: $('warnings'), override: $('protocol-override'),
  label: $('label'), test: $('test'), testResult: $('test-result'),
  targets: $('targets').querySelector('tbody'), targetsEmpty: $('targets-empty'),
  tplChips: $('tpl-chips'), tplTable: $('templates').querySelector('tbody'),
  tplNote: $('tpl-note'), settings: $('settings'),
  ovClock: $('ov-clock'), ovClockMode: $('ov-clock-mode'), ovClockPos: $('ov-clock-pos'),
  ovInfo: $('ov-info'), ovInfoPos: $('ov-info-pos'), ovSummary: $('ov-summary'),
  clockStatus: $('clock-status'),
  srcType: $('src-type'), srcFile: $('src-file'), srcCatalog: $('src-catalog'),
  srcUrl: $('src-url'), srcNote: $('src-note'), autoRestart: $('auto-restart'),
  diagnoses: $('diagnoses'), media: $('media').querySelector('tbody'),
  mediaEmpty: $('media-empty'), mediaDir: $('media-dir'), upload: $('upload'),
  bearer: $('bearer'), tokenField: $('token-field'), adaptations: $('adaptations'),
  suggestion: $('suggestion'), webrtcDelivery: $('webrtc-delivery'),
  prebuffer: $('prebuffer'), durationNote: $('duration-note'),
  bench: $('bench'), benchResult: $('bench-result'),
  downloadReport: $('download-report'),
  selfview: $('selfview'), selfviewImg: $('selfview-img'), selfviewAge: $('selfview-age'),
  composerBody: $('composer-body'), composerFolded: $('composer-folded'),
  composerReopen: $('composer-reopen'), startNote: $('start-note'),
  conditions: $('conditions').querySelector('tbody'), jobClock: $('job-clock'),
  timelineDiag: $('timeline-diag'), timelineOnlyDiag: $('timeline-only-diag'),
  vault: $('vault'), vaultToggle: $('vault-toggle'), vaultBody: $('vault-body'),
  vaultChips: $('vault-chips'), openMonitor: $('open-monitor'),
  systemDetail: $('system-detail'), composer: $('composer'),
};

// 監視だけの画面（/jobs/123）。組み立ての操作は出さない。
// 人に配るURLではなく、同じ端末の別タブ・ブックマーク用
const MONITOR_PATH = location.pathname.match(/^\/jobs\/(\d+)\/?$/);
const MONITOR_ONLY = MONITOR_PATH !== null;

const POSITIONS = [
  ['top-left', '左上'], ['top-center', '中央上'], ['top-right', '右上'],
  ['bottom-left', '左下'], ['bottom-center', '中央下'], ['bottom-right', '右下'],
];
for (const sel of document.querySelectorAll('select.pos')) {
  sel.innerHTML = POSITIONS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
}

let resolution = null;   // 直近のURL判定結果
let source = null;       // SSE接続
let currentJobId = null;
let selectedTargetId = null;   // 保存済み送出先から選んだ場合、URLは平文で持たない
let templates = [];
let selectedTemplateId = null; // フォームを手で触ると null（＝カスタム）に戻る
let selfviewTimer = null;      // セルフプレビューの取得タイマー
// 疎通確認の結果。none（未実施）/ ok / ng。送出開始ボタンの文言に使う
let testState = 'none';
let counts = { history: 0, targets: 0, templates: 0, media: 0 };
// 直前に終わったジョブ。フォームを触るまでは「同じ条件で再送出」を主ボタンにする
let lastFinishedJobId = null;

// --------------------------------------------------------------- 共通

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new Error(body?.detail || `${res.status} ${res.statusText}`);
  return body;
}

function showError(message) {
  el.error.textContent = message;
  el.error.classList.remove('hidden');
}
function clearError() { el.error.classList.add('hidden'); }

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ------------------------------------------------- 設定の読み書き

function readForm() {
  const durationRaw = parseInt($('duration').value, 10);
  return {
    target_url: selectedTargetId ? '' : el.url.value.trim(),
    target_id: selectedTargetId,
    target_label: el.label.value.trim(),
    bearer_token: el.bearer.value,
    webrtc_delivery: el.webrtcDelivery.checked,
    encode: {
      mode: 'encode',
      width: +$('width').value,
      height: +$('height').value,
      fps: +$('fps').value,
      bitrate_kbps: +$('bitrate').value,
      gop_sec: +$('gop').value,
      preset: $('preset').value,
      video_encoder: $('encoder').value,
    },
    source: readSource(),
    auto_restart: el.autoRestart.checked,
    // テンプレートを選んだだけなら id を送る。手で触っていたらフォームの値が優先される
    template_id: selectedTemplateId,
    overlay: {
      clock_enabled: el.ovClock.checked,
      clock_mode: el.ovClockMode.value,
      clock_position: el.ovClockPos.value,
      info_enabled: el.ovInfo.checked,
      info_position: el.ovInfoPos.value,
    },
    duration_sec: Number.isNaN(durationRaw) ? null : durationRaw,
  };
}

function readSource() {
  const type = el.srcType.value;
  if (type === 'file') return { type: 'file', path: el.srcFile.value, loop: true };
  if (type === 'live_url') {
    // 先読みはライブHLSにしか効かないので、ここでだけ送る
    const prebuffer = Math.max(0, parseInt(el.prebuffer.value, 10) || 0);
    return { type: 'live_url', url: el.srcUrl.value.trim(), reconnect: true, prebuffer_sec: prebuffer };
  }
  return { type: 'testsrc', pattern: $('pattern').value, with_tone: $('tone').checked };
}

function onSourceTypeChange() {
  const type = el.srcType.value;
  el.srcFile.classList.toggle('hidden', type !== 'file');
  el.srcCatalog.classList.toggle('hidden', type !== 'live_url');
  el.srcUrl.classList.toggle('hidden', type !== 'live_url');

  // ソースに属する設定は、そのソースを選んだときだけ出す。
  // エンコード設定に混ざっていると、テストパターンを選んでいないのに
  // パターンやトーンの指定が見えて紛らわしい
  $('opt-pattern').classList.toggle('hidden', type !== 'testsrc');
  $('opt-tone').classList.toggle('hidden', type !== 'testsrc');
  $('opt-prebuffer').classList.toggle('hidden', type !== 'live_url');

  if (type === 'file') {
    el.srcNote.textContent = el.srcFile.options.length
      ? 'ループ再生します。継ぎ目でタイムスタンプを張り直すので、長時間でも崩れません。'
      : 'media ディレクトリにファイルがありません。下の「メディアファイル」から追加してください。';
  } else if (type === 'live_url') {
    el.srcNote.innerHTML =
      'カタログ以外のURLを使う場合、<strong>その配信の利用規約の確認は利用者の責任</strong>です。'
      + '自動再接続は http(s) 入力でのみ働きます。';
  } else {
    el.srcNote.textContent = '';
  }
  updateStartButton();
}

function fillForm(defaults) {
  const e = defaults.encode;
  $('width').value = e.width;
  $('height').value = e.height;
  $('fps').value = e.fps;
  $('bitrate').value = e.bitrate_kbps;
  $('gop').value = e.gop_sec;
  $('preset').value = e.preset;
  $('encoder').value = e.video_encoder;
  $('pattern').value = defaults.source.pattern;
  $('tone').checked = defaults.source.with_tone;
  $('duration').value = defaults.duration_sec;
  el.prebuffer.value = defaults.source?.prebuffer_sec ?? 0;
  updateDurationNote();
  selectedTemplateId = defaults.template_id ?? null;
  const o = defaults.overlay || {};
  el.ovClock.checked = o.clock_enabled ?? true;
  el.ovClockMode.value = o.clock_mode || 'both';
  el.ovClockPos.value = o.clock_position || 'top-center';
  el.ovInfo.checked = o.info_enabled ?? true;
  el.ovInfoPos.value = o.info_position || 'bottom-left';
  updateOverlaySummary();

  const src = defaults.source || {};
  el.srcType.value = src.type || 'testsrc';
  if (src.type === 'live_url') el.srcUrl.value = src.url || '';
  if (src.type === 'file' && src.path) pendingFilePath = src.path;
  onSourceTypeChange();
}

let pendingFilePath = null;
let catalogByUrl = {};

// 時系列は指摘とログを1本にまとめる。「指摘の前後で何が起きたか」を読めるようにするため。
//
// 全件表示は DOM に積み上げない。行を配列で持って上限で切り、描画はフレームにまとめる。
// 毎行 textContent += すると、秒間数十行で描画が破綻してタブごと固まる（実際に起きた）。
// 指摘だけに絞った表示は件数が少ないので、そちらだけ DOM で組む
const LOG_MAX_LINES = 500;
const DIAG_MAX_LINES = 60;
let logLines = [];
let diagLines = [];
let logDirty = false;

function pushLog(line) {
  logLines.push(line);
  if (logLines.length > LOG_MAX_LINES) logLines = logLines.slice(-LOG_MAX_LINES);
  if (!logDirty) {
    logDirty = true;
    requestAnimationFrame(flushLog);
  }
}

// 指摘・状態の変化も同じ並びに入れる。頭の印で、ログ本文と区別できるようにする
function pushEvent(mark, text) {
  const line = `${mark} ${text}`;
  pushLog(line);
  diagLines.push(line);
  if (diagLines.length > DIAG_MAX_LINES) diagLines = diagLines.slice(-DIAG_MAX_LINES);
  renderDiagOnly();
}

function flushLog() {
  logDirty = false;
  const atBottom = el.timeline.scrollHeight - el.timeline.scrollTop - el.timeline.clientHeight < 40;
  el.timeline.textContent = logLines.join('\n');
  if (atBottom) el.timeline.scrollTop = el.timeline.scrollHeight;
}

function renderDiagOnly() {
  el.timelineDiag.innerHTML = diagLines.length
    ? diagLines.map((l) => `<div>${escapeHtml(l)}</div>`).join('')
    : '<p class="muted">まだ指摘はありません。</p>';
}

function applyTimelineFilter() {
  const onlyDiag = el.timelineOnlyDiag.checked;
  el.timeline.classList.toggle('hidden', onlyDiag);
  el.timelineDiag.classList.toggle('hidden', !onlyDiag);
}

function resetLog() {
  logLines = [];
  diagLines = [];
  el.timeline.textContent = '';
  renderDiagOnly();
}

function updateDurationNote() {
  // 「勝手に止まった」を防ぐため、何分で止まるのかを常に見せる
  const sec = parseInt($('duration').value, 10);
  el.durationNote.textContent = !sec
    ? '無期限で送出します'
    : `約 ${Math.round(sec / 60)} 分で自動停止します`;
}

function updateOverlaySummary() {
  const on = [];
  if (el.ovClock.checked) on.push({ both: '時計+フレーム番号', wallclock: '壁時計', framecount: 'フレーム番号' }[el.ovClockMode.value]);
  if (el.ovInfo.checked) on.push('設定情報');
  el.ovSummary.textContent = on.length ? ` — ${on.join(' / ')}` : ' — なし';
}

async function loadClockStatus() {
  // 壁時計モードを使うなら、サーバーの時計が合っていることが前提になる
  if (!el.ovClock.checked || el.ovClockMode.value === 'framecount') {
    el.clockStatus.textContent = '';
    return;
  }
  el.clockStatus.textContent = '時刻同期を確認中…';
  try {
    const s = await api('/api/system/clock');
    el.clockStatus.innerHTML = (s.ok ? '' : '<span class="no">⚠ </span>') + escapeHtml(s.message);
    el.clockStatus.style.color = s.ok ? '' : 'var(--warn)';
  } catch {
    el.clockStatus.textContent = '時刻同期の状態を取得できませんでした';
  }
}

// ------------------------------------------------------- URL判定

const resolveUrl = debounce(async () => {
  const url = el.url.value.trim();
  if (!url) {
    hideVerdict();
    resolution = null;
    updateStartButton();
    return;
  }
  try {
    resolution = await api('/api/targets/resolve', {
      method: 'POST', body: JSON.stringify({ url }),
    });
  } catch (err) {
    showError(err.message);
    return;
  }
  renderVerdict(resolution);
  updateStartButton();
}, 250);

function hideVerdict() {
  for (const node of [el.verdict, el.params, el.warnings, el.testResult, el.suggestion]) {
    node.classList.add('hidden');
  }
}

function renderVerdict(res) {
  const tag = res.protocol ? res.protocol.toUpperCase() : '判定不能';
  el.verdict.className = `verdict ${res.supported ? 'ok' : 'ng'}`;
  el.verdict.innerHTML = `<span class="tag">${tag}</span>${escapeHtml(res.reason)}`;
  el.verdict.classList.remove('hidden');

  // 分解したパラメータ。伏せ字はサーバー側で済んでいる
  el.params.innerHTML = (res.params || []).map((p) => `
    <span class="param"><span class="pk">${escapeHtml(p.label)}</span>
    <span class="pv">${escapeHtml(p.value || '—')}</span>
    ${p.note ? `<span class="pn">${escapeHtml(p.note)}</span>` : ''}</span>`).join('');
  el.params.classList.toggle('hidden', !(res.params || []).length);

  el.warnings.innerHTML = (res.warnings || []).map((w) => `<div>${escapeHtml(w)}</div>`).join('');
  el.warnings.classList.toggle('hidden', !(res.warnings || []).length);

  // URLを直したほうがよい場合の提案。勝手に書き換えず、押したときだけ適用する
  el.suggestion.classList.toggle('hidden', !res.suggested_url);
  if (res.suggested_url) {
    el.suggestion.className = 'verdict ng';
    el.suggestion.innerHTML = '<span class="tag">修正案</span>このURLなら送出できます：';
    const code = document.createElement('code');
    code.style.cssText = 'display:block;margin:6px 0;word-break:break-all;font-size:12px';
    code.textContent = res.suggested_url;
    const btn = document.createElement('button');
    btn.className = 'ghost';
    btn.textContent = 'このURLに直す';
    btn.onclick = () => { el.url.value = res.suggested_url; el.url.dispatchEvent(new Event('input')); };
    el.suggestion.append(code, btn);
  }

  el.test.disabled = !res.supported;
  // Bearerトークン欄は WHIP のときだけ出す
  el.tokenField.classList.toggle('hidden', res.protocol !== 'whip');
  if (res.known_target?.label && !el.label.value) el.label.value = res.known_target.label;
}

function updateStartButton() {
  if (MONITOR_ONLY) return;
  const ready = selectedTargetId !== null || (resolution && resolution.supported);
  const running = currentJobId !== null;
  el.start.disabled = !ready || running;

  // 見逃しがあるときは止めずに、文言と地模様で伝える。押せなくすると現場が困る。
  // 文言は事実を述べてから操作名を出す（押す人を責める語は入れない）
  const warnCount = (resolution?.warnings || []).length;
  let label = '送出開始';
  let caution = false;
  let note = '';
  if (!ready) {
    label = '送出開始';
  } else if (testState === 'ng') {
    label = '疎通NG — このまま送出開始';
    caution = true;
    note = '疎通確認は通っていません。それでも送出はできます。';
  } else if (warnCount) {
    label = `警告${warnCount}件あり — このまま送出開始`;
    caution = true;
    note = '上の警告を直さずに送出できます。修正案を押せば表示は消えます。';
  } else if (lastFinishedJobId !== null) {
    // 直前と同じ条件のまま。この画面で一番多い次の操作を主ボタンにする
    label = '同じ条件で再送出';
    note = `#${lastFinishedJobId} と同じ条件です。設定を触ると通常の送出開始に戻ります。`;
  } else if (testState === 'none') {
    label = '疎通未確認 — このまま送出開始';
    note = '「疎通確認」を押すと、最小構成で数秒だけ送ってハンドシェイクの成否を確かめます。';
  }
  el.start.textContent = running ? '送出中' : label;
  el.start.classList.toggle('caution', caution && !running);
  el.startNote.textContent = running ? '' : note;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// --------------------------------------------------------- ジョブ操作

function renderAdaptations(list) {
  // プロトコルの制約で設定を変えた場合、黙って直さず何を変えたか出す
  el.adaptations.innerHTML = (list || []).map((a) => `<div>${escapeHtml(a)}</div>`).join('');
  el.adaptations.classList.toggle('hidden', !(list || []).length);
}

async function startJob() {
  clearError();
  lastFinishedJobId = null;
  el.start.disabled = true;
  try {
    // 送出前に、プロトコル制約による読み替えを表示しておく
    await api('/api/jobs/preview', { method: 'POST', body: JSON.stringify(readForm()) })
      .then((p) => renderAdaptations(p.adaptations)).catch(() => {});
    const job = await api('/api/jobs', { method: 'POST', body: JSON.stringify(readForm()) });
    el.command.textContent = job.resolved_command;
    el.command.classList.remove('hidden');
    attachToJob(job.id, 'running');
  } catch (err) {
    showError(err.message);
  }
  updateStartButton();
  refreshHistory();
  refreshTargets();
  refreshTemplates();
}

async function stopJob() {
  if (currentJobId === null) return;
  el.stop.disabled = true;
  try {
    await api(`/api/jobs/${currentJobId}`, { method: 'DELETE' });
  } catch (err) {
    showError(err.message);
  }
  el.stop.disabled = false;
}

async function runBenchmark() {
  // 「落ちるか」を目安ではなく実測で答える。送出はしない
  clearError();
  el.bench.disabled = true;
  el.benchResult.className = 'verdict';
  el.benchResult.textContent = '計測中…（数秒かかります。送出はしません）';
  el.benchResult.classList.remove('hidden');
  try {
    const r = await api('/api/jobs/benchmark', { method: 'POST', body: JSON.stringify(readForm()) });
    const tone = { comfortable: 'ok', tight: 'ng', insufficient: 'ng' }[r.verdict] || 'ng';
    el.benchResult.className = `verdict ${r.ok ? tone : 'ng'}`;
    el.benchResult.innerHTML =
      `<span class="tag">${r.ok ? `${r.speed}x` : 'NG'}</span>${escapeHtml(r.message)}`;
  } catch (err) {
    el.benchResult.className = 'verdict ng';
    el.benchResult.innerHTML = `<span class="tag">NG</span>${escapeHtml(err.message)}`;
  }
  el.bench.disabled = false;
}

async function previewCommand() {
  clearError();
  try {
    const res = await api('/api/jobs/preview', { method: 'POST', body: JSON.stringify(readForm()) });
    el.command.textContent = res.command;
    el.command.classList.remove('hidden');
    renderAdaptations(res.adaptations);
  } catch (err) {
    showError(err.message);
  }
}

async function runConnectionTest() {
  clearError();
  el.test.disabled = true;
  el.testResult.className = 'verdict';
  el.testResult.textContent = '疎通確認中…（最小構成で3秒送ります）';
  el.testResult.classList.remove('hidden');
  try {
    const res = await api('/api/targets/test', {
      method: 'POST',
      body: JSON.stringify({
        url: selectedTargetId ? '' : el.url.value.trim(),
        target_id: selectedTargetId,
        bearer_token: el.bearer.value,
    webrtc_delivery: el.webrtcDelivery.checked,
      }),
    });
    testState = res.ok ? 'ok' : 'ng';
    el.testResult.className = `verdict ${res.ok ? 'ok' : 'ng'}`;
    el.testResult.innerHTML =
      `<span class="tag">${res.ok ? 'OK' : 'NG'}</span>${escapeHtml(res.message)}` +
      (res.ok || !res.log ? '' : `<pre class="command">${escapeHtml(res.log.slice(-1200))}</pre>`);
  } catch (err) {
    testState = 'ng';
    el.testResult.className = 'verdict ng';
    el.testResult.innerHTML = `<span class="tag">NG</span>${escapeHtml(err.message)}`;
  }
  el.test.disabled = false;
  updateStartButton();
}

async function applyProtocolOverride() {
  const value = el.override.value;
  const url = el.url.value.trim();
  if (!value || !url || selectedTargetId) return;
  clearError();
  try {
    // 手動上書きは保存して覚えさせる。次に同じURLを貼ったらこの判定が優先される
    await api('/api/targets', {
      method: 'POST',
      body: JSON.stringify({ url, label: el.label.value.trim(), protocol: value }),
    });
  } catch (err) {
    showError(err.message);
    return;
  }
  await refreshTargets();
  resolveUrl();
}

// ------------------------------------------------------------- SSE

function attachToJob(jobId, status) {
  currentJobId = jobId;
  el.monitor.classList.remove('hidden');
  el.jobId.textContent = `#${jobId}`;
  el.downloadReport.onclick = () => { window.location.href = reportUrl(jobId); };
  el.openMonitor.onclick = () => { window.open(`/jobs/${jobId}`, '_blank', 'noopener'); };
  setStatusBadge(status);
  el.stop.classList.remove('hidden');
  resetLog();
  el.benchResult.classList.add('hidden');
  el.diagnoses.innerHTML = '';
  el.diagnoses.classList.add('hidden');
  foldComposer(true);
  // 「何で送っているか」を送出中も読めるようにする。設定カードを開き直さずに済む
  loadConditions(jobId);
  updateStartButton();

  if (source) source.close();
  source = new EventSource(`/api/jobs/${jobId}/events`);

  source.addEventListener('snapshot', (ev) => {
    const snap = JSON.parse(ev.data);
    resetLog();
    for (const line of snap.log || []) pushLog(line);
    if (snap.snapshot_enabled) startSelfview(jobId); else stopSelfview();
    if (snap.progress?.frame !== undefined) renderProgress(snap.progress);
    // 途中から開いても、いま出ている指摘が見えるようにする
    for (const d of snap.diagnoses || []) renderDiagnosis(d);
  });
  source.addEventListener('progress', (ev) => renderProgress(JSON.parse(ev.data)));
  source.addEventListener('log', (ev) => pushLog(JSON.parse(ev.data).line));
  source.addEventListener('diagnosis', (ev) => renderDiagnosis(JSON.parse(ev.data)));
  source.addEventListener('status', (ev) => {
    const data = JSON.parse(ev.data);
    if (data.job_id !== currentJobId) return;
    if (data.status === 'reconnecting') {
      setStatusBadge(`再接続待ち (${data.attempt}回目 / ${data.delay_sec}秒後)`);
      // 静かに直さない。何回目で、次はいつ試すのかを記録に残す
      pushEvent('↻', `切断 — 再接続を試行（${data.attempt}回目 / ${data.delay_sec}秒後）`);
      return;
    }
    setStatusBadge(data.status);
    if (data.status !== 'running') {
      pushEvent('■', `送出終了（${data.status}）`);
      detachJob(data);
    }
  });
  source.onerror = () => { /* EventSource は自動再接続する */ };
}

// ------------------------------------------------- セルフプレビュー

const SELFVIEW_INTERVAL_MS = 2000;   // サーバー側は毎秒1枚。倍の間隔で十分
const SELFVIEW_STALE_SEC = 15;       // これ以上古かったら古いと見せる

function startSelfview(jobId) {
  stopSelfview();
  el.selfview.classList.remove('hidden');
  el.selfviewImg.classList.remove('ready');
  fetchSelfview(jobId);
  selfviewTimer = setInterval(() => fetchSelfview(jobId), SELFVIEW_INTERVAL_MS);
}

function stopSelfview() {
  if (selfviewTimer) { clearInterval(selfviewTimer); selfviewTimer = null; }
  el.selfview.classList.add('hidden');
  el.selfviewImg.classList.remove('ready');
  el.selfviewImg.removeAttribute('src');
  el.selfviewAge.textContent = '';
}

function fetchSelfview(jobId) {
  // タブが裏に回っている間は取りに行かない。遠隔（Tailscale越し）で開いたまま
  // 放置されると、プレビューが送出と同じ上り回線を延々と食う
  if (document.hidden || jobId !== currentJobId) return;
  // 同じURLだとブラウザが取り直さないので、毎回変える
  el.selfviewImg.src = `/api/jobs/${jobId}/snapshot.jpg?t=${Date.now()}`;
}

el.selfviewImg.addEventListener('load', () => el.selfviewImg.classList.add('ready'));
// まだ1枚目が出ていない（404）。絵を消さずに待つ
el.selfviewImg.addEventListener('error', () => { /* 前の絵を残す */ });
// 裏から戻ってきたら、間隔を待たずに取り直す
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && currentJobId && selfviewTimer) fetchSelfview(currentJobId);
});

function renderSelfviewAge(age) {
  // 「いつの絵か」を隠さない。更新が止まったこと自体が、映像が出ていない証拠になる
  if (age == null) {
    el.selfviewAge.textContent = '（まだ1枚も出ていません）';
    el.selfview.classList.add('stale');
    return;
  }
  el.selfviewAge.textContent = age < 2 ? '（最新）' : `（${age.toFixed(0)}秒前）`;
  el.selfview.classList.toggle('stale', age > SELFVIEW_STALE_SEC);
}

function renderDiagnosis(d) {
  // 掲示は解消したら消すが、時系列には出たことを残す。
  // 「掲示が消えた＝何も起きなかった」にしないため
  const existing = el.diagnoses.querySelector(`[data-key="${CSS.escape(d.key)}"]`);
  if (d.resolved) {
    if (existing) pushEvent('✓', `解消 ${d.message}`);
    existing?.remove();
  } else {
    if (!existing) pushEvent(d.severity === 'error' ? '⚠' : 'ℹ', `指摘 ${d.message}`);
    const div = existing || document.createElement('div');
    div.dataset.key = d.key;
    div.textContent = (d.severity === 'error' ? '⚠ ' : 'ℹ ') + d.message;
    if (!existing) el.diagnoses.appendChild(div);
  }
  el.diagnoses.classList.toggle('hidden', !el.diagnoses.children.length);
}

function detachJob(data) {
  if (data?.error) showError(data.error);
  stopSelfview();
  const finishedId = currentJobId;
  currentJobId = null;
  lastFinishedJobId = finishedId;
  // 画面を開き直した直後に停止した場合、フォームは空のまま。
  // 終わったジョブの条件を戻して、すぐ送り直せるようにする
  if (finishedId !== null && !el.url.value.trim() && selectedTargetId === null) {
    restoreFromJob(finishedId);
  }
  el.stop.classList.add('hidden');
  if (source) { source.close(); source = null; }
  // 止めたら①②③が値を保ったまま戻る。もう一度押せばすぐ送り直せる
  foldComposer(false);
  updateStartButton();
  refreshHistory();
}

// ------------------------------------------------- ①②③ の畳み／送出条件

function foldComposer(folded) {
  if (MONITOR_ONLY) return;
  el.composerBody.classList.toggle('hidden', folded);
  el.composerFolded.classList.toggle('hidden', !folded);
}

const SOURCE_LABELS = { testsrc: 'テストパターン', file: 'ファイル（ループ）', live_url: '外部ライブ' };

function sourceSummary(src) {
  if (!src || !src.type) return '—';
  if (src.type === 'file') return `${SOURCE_LABELS.file} ${src.path || ''}`.trim();
  if (src.type === 'live_url') {
    const pre = src.prebuffer_sec ? ` ／ 先読み ${src.prebuffer_sec}秒` : '';
    return `${SOURCE_LABELS.live_url} ${src.url || ''}${pre}`.trim();
  }
  return `${src.pattern || 'testsrc2'}${src.with_tone ? ' ＋1kHzトーン' : '（無音）'}`;
}

function overlaySummary(ov) {
  if (!ov) return '—';
  const parts = [];
  if (ov.clock_enabled) {
    const mode = { both: '時計+フレーム番号', wallclock: '壁時計', framecount: 'フレーム番号' }[ov.clock_mode];
    parts.push(`${mode}（${ov.clock_position}）`);
  }
  if (ov.info_enabled) parts.push(`設定情報（${ov.info_position}）`);
  return parts.length ? parts.join(' ／ ') : 'なし';
}

function renderConditions(job) {
  const e = job.encode || {};
  const rows = [
    ['送出先', `${(job.protocol || '').toUpperCase()} ${job.target_url || ''}`],
    ['ラベル', job.target_label || '—'],
    ['ソース', sourceSummary(job.source)],
    ['解像度 / fps', `${e.width}×${e.height} / ${e.fps}`],
    ['bitrate / GOP', `${e.bitrate_kbps}k / ${e.gop_sec}s`],
    ['encoder / preset', `${e.video_encoder || '—'} / ${e.preset || '—'}`],
    ['オーバーレイ', overlaySummary(job.overlay)],
    ['タイムアウト', job.duration_sec ? `${Math.round(job.duration_sec / 60)}分で自動停止` : '無期限'],
    ['自動再接続', job.auto_restart ? `ON（これまで${job.restart_count ?? 0}回）` : 'OFF'],
  ];
  el.conditions.innerHTML = rows.map(
    ([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(v)}</td></tr>`).join('');
}

// 終わったジョブの条件をフォームに戻す。送出先は伏せ字なので id で選び直す
async function restoreFromJob(jobId) {
  let job;
  try { job = await api(`/api/jobs/${jobId}`); } catch { return; }
  applyEncode(job.encode, job.source);
  applySource(job.source);
  const o = job.overlay || {};
  if (Object.keys(o).length) {
    el.ovClock.checked = !!o.clock_enabled;
    el.ovClockMode.value = o.clock_mode || 'both';
    el.ovClockPos.value = o.clock_position || 'top-center';
    el.ovInfo.checked = !!o.info_enabled;
    el.ovInfoPos.value = o.info_position || 'bottom-left';
    updateOverlaySummary();
  }
  if (job.target_id) {
    selectTarget({
      id: job.target_id, display_url: job.target_url,
      protocol: job.protocol, label: job.target_label, params: [],
    });
  }
  // 実際に繋がって送出できたジョブなので、疎通は確認済みとして扱う
  if (job.status === 'stopped' || job.status === 'completed') testState = 'ok';
  lastFinishedJobId = jobId;
  updateStartButton();
}

async function loadConditions(jobId) {
  try { renderConditions(await api(`/api/jobs/${jobId}`)); } catch { /* 表示できなくても送出は続く */ }
}

function setStatusBadge(status) {
  el.jobStatus.textContent = status;
  el.jobStatus.className = `badge ${status}`;
}

function renderProgress(p) {
  $('s-time').textContent = formatDuration(p.out_time_sec || 0);
  $('s-fps').textContent = (p.fps ?? 0).toFixed(1);
  $('s-bitrate').textContent = `${Math.round(p.bitrate_kbps || 0)}k`;
  // 累積平均ではなく「今の」レートを出す。累積は序盤の落ち込みを引きずる。
  // さらに映像基準を優先する。out_time 基準だと、映像が止まっていても音声さえ
  // 流れていれば 1.00x と表示されてしまう
  const rate = p.video_rate ?? p.rate ?? p.speed ?? 0;
  $('s-speed').textContent = `${rate.toFixed(2)}x`;
  // 異常は色だけでなく形（二重下線）でも出す。受信側を撮った写真と並べるため
  const drop = p.drop_frames ?? 0;
  $('s-drop').textContent = drop;
  $('s-drop').classList.toggle('alarm', drop > 0);
  $('s-speed').classList.toggle('alarm', rate > 0 && rate < 0.95);
  $('s-frame').textContent = p.frame ?? 0;

  // 映像が音声エンコーダよりどれだけ後ろにいるか。送出される中身のずれでは
  // ないので（-max_interleave_delta 0 が待たせる）、色は付けず参考値として出す
  const lead = p.av_skew_sec;
  $('s-skew').textContent = lead == null ? '–' : `${lead.toFixed(1)}s`;

  // キーが無ければセルフプレビューを出していない（passthrough や設定で無効）
  if ('snapshot_age_sec' in p) renderSelfviewAge(p.snapshot_age_sec);
}

function formatDuration(sec) {
  const s = Math.floor(sec % 60), m = Math.floor(sec / 60) % 60, h = Math.floor(sec / 3600);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

// ----------------------------------------------------------- 履歴

function reportUrl(jobId) {
  return `/api/jobs/${jobId}/report`;
}

async function refreshHistory() {
  let data;
  try { data = await api('/api/jobs?limit=20'); } catch { return; }

  counts.history = data.jobs.length;
  renderVaultChips();
  el.history.innerHTML = '';
  for (const job of data.jobs) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${job.id}</td>
      <td><span class="badge ${job.status}">${job.status}</span></td>
      <td>${job.protocol.toUpperCase()}</td>
      <td class="url">${escapeHtml(job.target_url)}</td>
      <td>${new Date(job.started_at).toLocaleString('ja-JP')}</td>
      <td></td>`;
    const dl = document.createElement('a');
    dl.className = 'ghost';
    dl.textContent = 'JSON';
    dl.href = reportUrl(job.id);
    dl.download = '';
    dl.className = 'ghost report-link';
    tr.lastElementChild.appendChild(dl);

    const btn = document.createElement('button');
    btn.className = 'ghost';
    btn.textContent = 'この条件で';
    btn.onclick = () => {
      applyEncode(job.encode, job.source);
      applySource(job.source);
      if (job.target_id) {
        selectTarget({ id: job.target_id, display_url: job.target_url, protocol: job.protocol, label: job.target_label, params: [] });
      } else {
        el.url.value = job.target_url;
        resolveUrl();
      }
      backToComposer();
    };
    tr.lastElementChild.appendChild(btn);
    el.history.appendChild(tr);
  }

  // API を再起動した直後などに、稼働中ジョブへ復帰する
  if (data.running_job_id && data.running_job_id !== currentJobId) {
    attachToJob(data.running_job_id, 'running');
  }
}

function applySource(src) {
  if (!src) return;
  el.srcType.value = src.type || 'testsrc';
  if (src.type === 'live_url') el.srcUrl.value = src.url || '';
  onSourceTypeChange();
  if (src.type === 'file' && src.path) el.srcFile.value = src.path;
}

function applyEncode(encode, src) {
  if (!encode) return;
  el.settings.open = encode.mode !== 'passthrough';
  $('width').value = encode.width;
  $('height').value = encode.height;
  $('fps').value = encode.fps;
  $('bitrate').value = encode.bitrate_kbps;
  $('gop').value = encode.gop_sec;
  $('preset').value = encode.preset;
  $('encoder').value = encode.video_encoder;
  if (src) { $('pattern').value = src.pattern; $('tone').checked = src.with_tone; }
}

// ------------------------------------------------------------- ソース

async function refreshSources() {
  let data;
  try { data = await api('/api/sources'); } catch { return; }

  counts.media = data.files.length;
  renderVaultChips();
  el.mediaDir.textContent = data.media_dir;
  el.srcFile.innerHTML = data.files.map((f) =>
    `<option value="${escapeHtml(f.name)}">${escapeHtml(f.name)} (${f.size_human})</option>`).join('');
  if (pendingFilePath) { el.srcFile.value = pendingFilePath; pendingFilePath = null; }

  // 24時間ライブと VOD は用途が違うので分けて見せる
  const group = (label, items) => items.length
    ? `<optgroup label="${label}">` + items.map((c) =>
        `<option value="${escapeHtml(c.url)}" title="${escapeHtml(c.note)}">${c.caution ? '⚠ ' : ''}${escapeHtml(c.name)}</option>`
      ).join('') + '</optgroup>'
    : '';
  el.srcCatalog.innerHTML = '<option value="">カタログから選ぶ…</option>'
    + group('24時間ライブ（耐久試験向け）', data.catalog.filter((c) => c.live))
    + group('VOD（再現性のある比較向け）', data.catalog.filter((c) => !c.live));
  catalogByUrl = Object.fromEntries(data.catalog.map((c) => [c.url, c]));

  el.media.innerHTML = '';
  el.mediaEmpty.classList.toggle('hidden', data.files.length > 0);
  for (const f of data.files) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td class="url">${escapeHtml(f.name)}</td><td>${f.size_human}</td><td></td>`;
    const use = document.createElement('button');
    use.className = 'ghost';
    use.textContent = '使う';
    use.onclick = () => {
      el.srcType.value = 'file';
      onSourceTypeChange();
      el.srcFile.value = f.name;
      backToComposer();
    };
    const del = document.createElement('button');
    del.className = 'ghost';
    del.textContent = '削除';
    del.onclick = async () => {
      if (!confirm(`${f.name} を削除しますか？`)) return;
      await api(`/api/sources/${encodeURIComponent(f.name)}`, { method: 'DELETE' })
        .catch((e) => showError(e.message));
      refreshSources();
    };
    tr.lastElementChild.append(use, del);
    el.media.appendChild(tr);
  }
  onSourceTypeChange();
}

async function uploadFile() {
  const file = el.upload.files[0];
  if (!file) { showError('ファイルを選んでください'); return; }
  clearError();
  const btn = $('upload-btn');
  btn.disabled = true;
  btn.textContent = 'アップロード中…';
  try {
    const body = new FormData();
    body.append('file', file);
    const res = await fetch('/api/sources/upload', { method: 'POST', body });
    const json = await res.json().catch(() => null);
    if (!res.ok) throw new Error(json?.detail || res.statusText);
    el.upload.value = '';
    await refreshSources();
  } catch (err) {
    showError(err.message);
  }
  btn.disabled = false;
  btn.textContent = 'アップロード';
}

// --------------------------------------------------------- テンプレート

async function refreshTemplates() {
  try { templates = (await api('/api/templates')).templates; } catch { return; }
  counts.templates = templates.length;
  renderVaultChips();
  renderTemplateChips();
  renderTemplateTable();
}

function renderTemplateChips() {
  el.tplChips.innerHTML = '';
  for (const t of templates.slice(0, 8)) {
    const b = document.createElement('button');
    b.className = 'chip' + (t.id === selectedTemplateId ? ' active' : '');
    b.innerHTML = (t.is_pinned ? '<span class="pin">★</span> ' : '') + escapeHtml(t.name);
    b.title = t.summary;
    b.onclick = () => applyTemplate(t);
    el.tplChips.appendChild(b);
  }
  if (selectedTemplateId === null) {
    const b = document.createElement('button');
    b.className = 'chip custom active';
    b.textContent = 'カスタム（送出時に自動保存）';
    b.disabled = true;
    el.tplChips.appendChild(b);
  }
}

function applyTemplate(t) {
  selectedTemplateId = t.id;
  applyEncode(t.settings, null);
  renderTemplateChips();
  // Passthrough は testsrc と併用できないので、選んだ時点で理由を見せておく
  el.tplNote.textContent = t.settings.mode === 'passthrough'
    ? 'Passthrough はテストパターンには使えません（P4のファイル／外部ソースが必要）' : '';
}

function renderTemplateTable() {
  el.tplTable.innerHTML = '';
  for (const t of templates) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td></td>
      <td class="tpl-name">${escapeHtml(t.name)}${t.is_builtin ? ' <span class="badge">既定</span>' : ''}${t.is_auto_named ? ' <span class="badge">自動命名</span>' : ''}</td>
      <td class="tpl-sum">${escapeHtml(t.summary)}</td>
      <td>${t.use_count}</td>
      <td></td>`;

    const pin = document.createElement('button');
    pin.className = 'pin-btn';
    pin.textContent = t.is_pinned ? '★' : '☆';
    pin.title = t.is_pinned ? 'ピン留めを外す' : 'ピン留め（自動整理の対象外にする）';
    pin.onclick = async () => {
      await api(`/api/templates/${t.id}`, {
        method: 'PATCH', body: JSON.stringify({ is_pinned: !t.is_pinned }),
      }).catch((e) => showError(e.message));
      refreshTemplates();
    };
    tr.firstElementChild.appendChild(pin);

    const use = document.createElement('button');
    use.className = 'ghost';
    use.textContent = '使う';
    use.onclick = () => { applyTemplate(t); backToComposer(); };

    const rename = document.createElement('button');
    rename.className = 'ghost';
    rename.textContent = '名前';
    rename.onclick = async () => {
      const name = prompt('テンプレート名', t.name);
      if (!name || name === t.name) return;
      await api(`/api/templates/${t.id}`, {
        method: 'PATCH', body: JSON.stringify({ name }),
      }).catch((e) => showError(e.message));
      refreshTemplates();
    };

    tr.lastElementChild.append(use, rename);
    if (!t.is_builtin) {
      const del = document.createElement('button');
      del.className = 'ghost';
      del.textContent = '削除';
      del.onclick = async () => {
        await api(`/api/templates/${t.id}`, { method: 'DELETE' }).catch((e) => showError(e.message));
        if (selectedTemplateId === t.id) selectedTemplateId = null;
        refreshTemplates();
      };
      tr.lastElementChild.appendChild(del);
    }
    el.tplTable.appendChild(tr);
  }
}

// フォームを手で触ったらテンプレート選択を外す。
// 「選んだつもりの設定」と「実際に送る設定」がずれないようにするため
function markCustom() {
  lastFinishedJobId = null;
  updateStartButton();
  if (selectedTemplateId !== null) {
    selectedTemplateId = null;
    el.tplNote.textContent = '';
    renderTemplateChips();
  }
}

// ------------------------------------------------------- 送出先一覧

async function refreshTargets() {
  let data;
  try { data = await api('/api/targets?limit=30'); } catch { return; }

  counts.targets = data.targets.length;
  renderVaultChips();
  el.targets.innerHTML = '';
  el.targetsEmpty.classList.toggle('hidden', data.targets.length > 0);

  for (const t of data.targets) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(t.label || '—')}</td>
      <td><span class="badge">${t.protocol.toUpperCase()}</span>${t.detected_by === 'manual' ? ' <span class="badge">手動</span>' : ''}</td>
      <td class="url">${escapeHtml(t.display_url)}</td>
      <td>${t.use_count}</td>
      <td></td>`;
    const use = document.createElement('button');
    use.className = 'ghost';
    use.textContent = '使う';
    use.onclick = () => selectTarget(t);
    const del = document.createElement('button');
    del.className = 'ghost';
    del.textContent = '削除';
    del.onclick = async () => {
      await api(`/api/targets/${t.id}`, { method: 'DELETE' }).catch((e) => showError(e.message));
      if (selectedTargetId === t.id) clearTargetSelection();
      refreshTargets();
    };
    tr.lastElementChild.append(use, del);
    el.targets.appendChild(tr);
  }
}

function selectTarget(t) {
  // 保存済みを選んだときは平文URLをブラウザに戻さない。id だけを持つ
  selectedTargetId = t.id;
  el.url.value = t.display_url;
  el.url.disabled = true;
  el.label.value = t.label || '';
  resolution = { protocol: t.protocol, supported: true, reason: '保存済みの送出先を使用します', params: t.params, warnings: [] };
  renderVerdict(resolution);
  el.preview.textContent = 'コマンドを確認';
  updateStartButton();
  backToComposer();
}

function clearTargetSelection() {
  selectedTargetId = null;
  el.url.disabled = false;
  el.url.value = '';
  hideVerdict();
  resolution = null;
  updateStartButton();
}

// ----------------------------------------------------------- 起動

async function loadCapabilities() {
  try {
    const caps = await api('/api/system/capabilities');
    const mark = (ok) => ok ? '✓' : '<span class="no">✗</span>';
    el.caps.innerHTML =
      `ffmpeg ${caps.version} — ` +
      `RTMP ${mark(caps.protocols.rtmp)} / SRT ${mark(caps.protocols.srt)} / ` +
      `WHIP ${mark(caps.whip_ready)} / オーバーレイ ${mark(caps.overlay_ready)}` +
      (caps.font ? ` <span title="${escapeHtml(caps.font.path)}">(${escapeHtml(caps.font.origin)})</span>` : '');
    if (el.systemDetail) {
      el.systemDetail.innerHTML = [
        `<div>FFmpeg — ${escapeHtml(caps.version)}</div>`,
        `<div>RTMP ${mark(caps.protocols.rtmp)} ／ SRT ${mark(caps.protocols.srt)} ／ `
          + `WHIP ${mark(caps.whip_ready)} ／ drawtext ${mark(caps.overlay_ready)}</div>`,
        caps.font
          ? `<div>フォント — ${escapeHtml(caps.font.origin)}：${escapeHtml(caps.font.path)}</div>`
          : '<div><span class="no">✗</span> フォントが見つかりません（オーバーレイを焼けません）</div>',
        '<div id="system-clock">時刻同期を確認中…</div>',
      ].join('');
      api('/api/system/clock')
        .then((c) => {
          const node = $('system-clock');
          if (node) node.innerHTML = (c.ok ? '' : '<span class="no">⚠ </span>') + escapeHtml(c.message);
        })
        .catch(() => {});
    }
    if (!caps.available) showError(caps.error);
  } catch (err) {
    el.caps.textContent = '能力を取得できませんでした';
  }
}

// ------------------------------------------------------- 配色の切り替え

// 実際の切り替えは <html> の data-scene だけ。CSS 側は theme.css の
// トークンを差し替えるので、ここで色・書体・形そのものには触らない。
// 選択は端末ごとに覚える（サーバーには送らない）
const SCENES = ['light', 'dark', 'terminal', 'tactical', 'typewriter'];

// 選んだことがない間は OS の設定に従う。選んだ時点で追従をやめる
function preferredScene() {
  return window.matchMedia
    && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function currentScene() {
  const attr = document.documentElement.getAttribute('data-scene');
  return SCENES.includes(attr) ? attr : 'light';
}

function applyScene(scene) {
  if (!SCENES.includes(scene)) return;
  document.documentElement.setAttribute('data-scene', scene);
  try { localStorage.setItem('sf.scene', scene); }
  catch { /* 保存できなくても、この画面の間は効いている */ }
  renderSceneSwitch();
}

function renderSceneSwitch() {
  const scene = currentScene();
  for (const b of document.querySelectorAll('#scene-switch button')) {
    b.setAttribute('aria-pressed', String(b.dataset.scene === scene));
  }
}

function initSceneSwitch() {
  for (const b of document.querySelectorAll('#scene-switch button')) {
    b.addEventListener('click', () => applyScene(b.dataset.scene));
  }
  // 自分で選ぶまでは OS の切り替えに追従する
  if (window.matchMedia) {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const follow = () => {
      let saved = null;
      try { saved = localStorage.getItem('sf.scene'); } catch { /* 追従したまま */ }
      if (SCENES.includes(saved)) return;
      document.documentElement.setAttribute('data-scene', preferredScene());
      renderSceneSwitch();
    };
    if (mq.addEventListener) mq.addEventListener('change', follow);
    else if (mq.addListener) mq.addListener(follow);
  }
  renderSceneSwitch();
}

// ---------------------------------------------------- ④ 記録と資産

// 既定は常に閉じる。開閉状態も覚えない。
// 送出に要らないものを初期表示に出さないことを優先し、
// 「前回開いていたから今日も開いている」を作らない
function toggleVault(open) {
  const next = open ?? el.vaultBody.hidden;
  el.vaultBody.hidden = !next;
  el.vaultToggle.setAttribute('aria-expanded', String(next));
  el.vault.setAttribute('open-state', next ? 'open' : 'closed');
}

function selectVaultTab(name) {
  for (const tab of document.querySelectorAll('.tab')) {
    tab.setAttribute('aria-selected', String(tab.dataset.tab === name));
  }
  for (const panel of document.querySelectorAll('.panel')) {
    panel.classList.toggle('hidden', panel.dataset.panel !== name);
  }
}

function renderVaultChips() {
  // 中身を見なくても、何件あるかだけは閉じたまま分かるようにする
  const items = [
    ['履歴', counts.history], ['送出先', counts.targets],
    ['テンプレート', counts.templates], ['メディア', counts.media],
  ];
  el.vaultChips.innerHTML = items
    .map(([k, n]) => `<span>${k} ${n}</span>`).join('');
}

// ④ から「使う」を押したときは、畳んで組み立てに戻す
function backToComposer() {
  toggleVault(false);
  backToComposer();
}

el.url.addEventListener('input', () => {
  if (el.url.disabled) return;
  // URLが変わったら疎通確認の結果は当てにならない。未確認に戻す
  testState = 'none';
  lastFinishedJobId = null;
  el.testResult.classList.add('hidden');
  resolveUrl();
});
el.url.addEventListener('dblclick', () => { if (el.url.disabled) clearTargetSelection(); });
el.start.addEventListener('click', startJob);
el.stop.addEventListener('click', stopJob);
el.preview.addEventListener('click', previewCommand);
el.bench.addEventListener('click', runBenchmark);
el.test.addEventListener('click', runConnectionTest);
el.srcType.addEventListener('change', () => { lastFinishedJobId = null; onSourceTypeChange(); });
$('duration').addEventListener('input', updateDurationNote);
el.srcCatalog.addEventListener('change', () => {
  if (!el.srcCatalog.value) return;
  el.srcUrl.value = el.srcCatalog.value;
  const c = catalogByUrl[el.srcCatalog.value];
  if (c) {
    el.srcNote.textContent = `${c.live ? '24時間ライブ' : 'VOD'} — ${c.note}`;
    el.srcNote.style.color = c.caution ? 'var(--warn)' : '';
  }
});
$('upload-btn').addEventListener('click', uploadFile);
el.webrtcDelivery.addEventListener('change', () => {
  // 何が変わるかは押す前に見せる
  api('/api/jobs/preview', { method: 'POST', body: JSON.stringify(readForm()) })
    .then((p) => renderAdaptations(p.adaptations)).catch(() => {});
});
for (const node of [el.ovClock, el.ovClockMode, el.ovInfo]) {
  node.addEventListener('change', () => { updateOverlaySummary(); loadClockStatus(); });
}
el.override.addEventListener('change', applyProtocolOverride);
el.composerReopen.addEventListener('click', () => foldComposer(false));
el.vaultToggle.addEventListener('click', () => toggleVault());
for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => selectVaultTab(tab.dataset.tab));
}
el.timelineOnlyDiag.addEventListener('change', applyTimelineFilter);

for (const id of ['width', 'height', 'fps', 'bitrate', 'gop', 'preset', 'encoder']) {
  $(id).addEventListener('input', markCustom);
  $(id).addEventListener('change', markCustom);
}

// 監視だけの画面。終わったジョブも同じURLで開ける（履歴からの行き先がここになる）
async function initMonitorPage(jobId) {
  el.composer.classList.add('hidden');
  el.vault.classList.add('hidden');
  el.monitor.classList.remove('hidden');
  $('header-note').textContent = `監視 #${jobId} — 同じ端末の別タブ用。設定の操作はここにはありません。`;
  el.openMonitor.classList.add('hidden');
  el.jobId.textContent = `#${jobId}`;
  el.downloadReport.onclick = () => { window.location.href = reportUrl(jobId); };
  applyTimelineFilter();

  let job;
  try {
    job = await api(`/api/jobs/${jobId}`);
  } catch (err) {
    showError(err.message);
    el.monitor.classList.add('hidden');
    el.error.classList.remove('hidden');
    document.querySelector('main').prepend(el.error);
    return;
  }
  renderConditions(job);
  setStatusBadge(job.status);
  el.jobClock.textContent = job.ended_at
    ? `${new Date(job.started_at).toLocaleString('ja-JP')} 〜 ${new Date(job.ended_at).toLocaleString('ja-JP')}`
    : `${new Date(job.started_at).toLocaleString('ja-JP')} 開始`;

  if (job.status === 'running') {
    attachToJob(jobId, 'running');
  } else {
    // 終わったジョブは記録を読むだけ。SSE は繋がない
    resetLog();
    for (const line of (job.log || '').split('\n')) if (line) pushLog(line);
    el.stop.classList.add('hidden');
    if (job.last_error) showError(job.last_error);
  }
}

(async function init() {
  initSceneSwitch();
  applyTimelineFilter();
  renderVaultChips();
  await loadCapabilities();
  if (MONITOR_ONLY) {
    await initMonitorPage(Number(MONITOR_PATH[1]));
    return;
  }
  try { fillForm(await api('/api/defaults')); } catch { /* 既定値のまま */ }
  await Promise.all([refreshSources(), refreshTemplates(), refreshTargets(), refreshHistory()]);
  updateStartButton();
  loadClockStatus();
})();
