// Demucs stems-pack install card (T-STEMSUI). Plain JS; server strings only via textContent.
// Hooks (read-only): #stems container, #stems-toggle, window.__stems.status (capabilities.free_gb, running.stage/message).
(() => {
const host = document.getElementById('stems');
if (!host) return;
{ const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = '/static/css/stems-install.css'; document.head.appendChild(l); }
const STAGES = ['venv', 'downloading', 'installing', 'verifying'];
const STAGE_LABEL = {venv: 'Environment', downloading: 'Downloading', installing: 'Installing', verifying: 'Verifying'};
const SEP_STAGES = ['downloading_weights', 'separating'];
const SEP_LABEL = {downloading_weights: 'Downloading Demucs weights (first time only)', separating: 'Separating'};
const st = {pack: null, job: null, err: null, poll: null, freeGb: null, busy: false};

function mk(tag, cls, txt, attrs) {
  const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt;
  for (const k in (attrs || {})) e.setAttribute(k, attrs[k]);
  return e;
}
const card = mk('div', 'si-card hidden', null, {id: 'si-card', role: 'region', 'aria-label': 'Demucs install'});
const title = mk('b', 'si-title'), note = mk('div', 'si-note muted'), steps = mk('ol', 'si-steps');
const bar = mk('div', 'si-bar', null, {role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0', 'aria-label': 'Install progress'});
const fill = mk('i'); bar.appendChild(fill);
const pkg = mk('div', 'si-pkg muted'), errEl = mk('div', 'si-err', null, {role: 'alert'});
const row = mk('div', 'si-row');
const bGo = mk('button', null, 'Install Demucs (~1 GB)', {type: 'button', id: 'si-go'});
const bCancel = mk('button', null, 'Cancel', {type: 'button', id: 'si-cancel'});
const bRetry = mk('button', null, 'Retry', {type: 'button', id: 'si-retry'});
row.append(bGo, bCancel, bRetry);
for (const s of STAGES) steps.appendChild(mk('li', null, STAGE_LABEL[s], {'data-s': s}));
card.append(title, note, steps, bar, pkg, errEl, row);
host.appendChild(card);
const sep = mk('div', 'si-sep muted hidden', null, {id: 'si-sep', 'aria-live': 'polite'});
host.appendChild(sep);

const q = p => fetch(p).then(r => r.json());
const running = () => !!(st.job && st.job.status === 'running');
const freeGb = () => { const s = window.__stems && window.__stems.status; const g = s && s.capabilities && s.capabilities.free_gb; return g != null ? g : st.freeGb; };
const panelOpen = () => { const t = document.getElementById('stems-toggle'); return !t || t.getAttribute('aria-expanded') === 'true'; };

function render() {
  const p = st.pack || {}, run = running(), done = p.installed && !run;
  const show = panelOpen() && st.pack && (!p.installed || run || st.err);
  const showInstalled = panelOpen() && st.pack && done;
  card.classList.toggle('hidden', !(show || showInstalled));
  if (!st.pack) return;
  if (showInstalled && !st.err) {
    title.textContent = 'Installed: Demucs ' + (p.version || '') + (p.size_mb != null ? ', ' + p.size_mb + ' MB' : '');
    note.textContent = p.weights_cached === false ? 'Weights download on first use.' : '';
  } else {
    title.textContent = 'Install Demucs (~1 GB)';
    const f = freeGb();
    note.textContent = (p.half_installed ? 'A previous install was incomplete and will be rebuilt. ' : '')
      + 'Needs about 1 GB of downloads and 3 GB free.' + (f != null ? ' Free disk: ' + f + ' GB.' : '');
  }
  const j = st.job, stage = j && j.stage, idx = STAGES.indexOf(stage);
  steps.classList.toggle('hidden', !run);
  steps.querySelectorAll('li').forEach((li, i) => { li.className = !run ? '' : i < idx ? 'done' : i === idx ? 'cur' : ''; if (run && i === idx) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current'); });
  bar.classList.toggle('si-off', !run);
  const pct = run ? Math.round((j.progress || 0) * 100) : 0;
  fill.style.width = pct + '%'; bar.setAttribute('aria-valuenow', String(pct));
  pkg.textContent = run ? ((j.package ? j.package + ' · ' : '') + (j.message || '')) : '';
  errEl.textContent = st.err ? st.err.text : '';
  const canGo = !p.installed && !run && !(st.err && st.err.retry);
  bGo.classList.toggle('hidden', !canGo); bGo.disabled = st.busy;
  bGo.textContent = p.half_installed ? 'Rebuild Demucs (~1 GB)' : 'Install Demucs (~1 GB)';
  bCancel.classList.toggle('hidden', !run);
  bRetry.classList.toggle('hidden', !(st.err && st.err.retry && !run));
}
function fail(r, d) {
  const m = (d && d.detail) || (d && d.error) || ('HTTP ' + r.status);
  if (r.status === 507) { const n = /([\d.]+)\s*GB needed/.exec(m); const f = /([\d.]+)\s*GB free/.exec(m); st.err = {retry: true, text: 'Not enough disk space: ' + (n ? n[1] : '3') + ' GB needed' + (f ? ', ' + f[1] + ' GB free' : '') + '. Free some space, then retry.'}; }
  else if (r.status === 501) st.err = {retry: false, text: 'No system Python found. Install Python 3.9, 3.10, 3.11 or 3.12 (download from https://www.python.org/downloads/), then reopen the app.'};
  else st.err = {retry: true, text: 'Install failed: ' + m};
}
async function loadPack() {
  try { const d = await q('/api/stems/install'); st.pack = d.pack || {installed: !!d.installed}; if (d.installed != null) st.pack.installed = !!d.installed; st.job = d.job || (d.status ? d : null);
    if (d.frozen && d.message && !st.pack.installed) { st.pack.unavailable = d.message; }
  } catch (e) { return; }
  if (st.job && st.job.status === 'error') st.err = {retry: true, text: 'Install failed: ' + (st.job.error || st.job.message || 'unknown error')};
  if (running()) poll();
  render();
}
function poll() {
  clearTimeout(st.poll);
  if (!running()) return;
  st.poll = setTimeout(async () => {
    try { st.job = await q('/api/stems/install/' + encodeURIComponent(st.job.id)); } catch (e) { poll(); return; }
    if (running()) { render(); poll(); return; }
    if (st.job.status === 'error') st.err = {retry: true, text: 'Install failed: ' + (st.job.error || st.job.message || 'unknown error')};
    try { st.pack = await q('/api/stems/pack'); } catch (e) {}
    render();
    if (st.job.status === 'done' && window.__stems && document.getElementById('stems-toggle')) { /* let stems.js re-read capabilities on next refresh */ }
  }, 1200);
}
async function start() {
  st.err = null; st.busy = true; render();
  try {
    const r = await fetch('/api/stems/install', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { if (r.status === 409) await loadPack(); else fail(r, d); }
    else if (d.id == null && d.status === 'done') st.pack = await q('/api/stems/pack');
    else { st.job = d; poll(); }
  } catch (e) { st.err = {retry: true, text: 'Install failed: ' + e}; }
  st.busy = false; render();
}
async function cancel() {
  if (!running()) return; bCancel.disabled = true;
  try { await fetch('/api/stems/install/' + encodeURIComponent(st.job.id) + '/cancel', {method: 'POST'}); st.job = await q('/api/stems/install/' + encodeURIComponent(st.job.id)); } catch (e) {}
  bCancel.disabled = false; clearTimeout(st.poll); if (running()) poll(); else { st.err = null; try { st.pack = await q('/api/stems/pack'); } catch (e) {} } render();
}
bGo.onclick = start; bRetry.onclick = start; bCancel.onclick = cancel;

// Separation stages (downloading_weights -> separating), read from stems.js's own status; no extra network.
setInterval(() => {
  const s = window.__stems && window.__stems.status, j = s && s.state === 'running' && (s.running || s.last);
  const stage = j && j.stage;
  const on = SEP_STAGES.indexOf(stage) >= 0 && panelOpen();
  sep.classList.toggle('hidden', !on);
  if (on) sep.textContent = SEP_LABEL[stage] + (stage === 'separating' ? '' : '...');
  if (panelOpen() && !card.dataset.ready) { card.dataset.ready = '1'; loadPack(); }
}, 700);
const tg = document.getElementById('stems-toggle');
if (tg) tg.addEventListener('click', () => setTimeout(() => { if (panelOpen()) loadPack(); else render(); }, 0));
window.__stemsInstall = st;
})();
