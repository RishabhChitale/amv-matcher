// A1 stems: per-stem lanes (waveform, onsets, texture tint), M/S/volume, Web Audio stems mix, separate/install controls.
(() => {
const el = id => document.getElementById(id);
const box = el('stems'), lanesEl = el('stems-lanes'), tex = {};
const COL = {drums: '#ff3b4e', bass: '#8b5cf6', vocals: '#22d3ee', other: '#facc15', music: '#8b5cf6'};
const st = {open: false, path: null, status: null, provider: null, stems: [], feats: null, bufs: {}, mute: {}, solo: {}, vol: {}, ctx: null, gains: {}, master: null, srcs: [], srcT0: 0, srcS0: 0, srcRate: 1, loading: 0, job: null, poll: null, peaks: {}};
const post = (u, b) => jpost(u, b || {});
const q = p => encodeURIComponent(p);
const msg = t => { el('stems-msg').textContent = t || ''; };

function mixOn() { return st.open && el('stems-mix').checked && st.stems.length && st.stems.every(n => st.bufs[n]); }
function audible(n) { const anyS = st.stems.some(k => st.solo[k]); return anyS ? !!st.solo[n] : !st.mute[n]; }

// ---------- status / controls ----------
async function refresh() {
  const p = S.sel && S.sel.path; st.path = p || null;
  if (!p) { st.status = null; st.stems = []; st.feats = null; stopSrcs(); render(); return; }
  let s; try { s = await (await fetch('/api/stems/status?song_path=' + q(p))).json(); } catch (e) { return; }
  if (!S.sel || S.sel.path !== p) return;
  st.status = s; render();
  if (s.state === 'ready') loadStems(s.provider, s.stems, p);
  else if (s.state === 'running') pollJob();
  else { st.stems = []; st.feats = null; stopSrcs(); render(); }
}
async function loadStems(prov, names, p) {
  if (st.provider === prov && st.stems.join() === names.join() && st.feats && names.every(n => st.bufs[n])) { render(); return; }
  const tok = ++st.loading; st.provider = prov; st.stems = names.slice(); st.bufs = {}; st.peaks = {}; st.feats = null; stopSrcs(); render();
  try {
    const f = await (await fetch(`/api/stem-features?song_path=${q(p)}&provider=${prov}`)).json();
    if (tok !== st.loading) return; st.feats = f; draw();
    ensureCtx();
    for (const n of names) {
      const ab = await (await fetch(`/api/stem-audio?song_path=${q(p)}&stem=${q(n)}&provider=${prov}`)).arrayBuffer();
      const buf = await st.ctx.decodeAudioData(ab); if (tok !== st.loading) return;
      st.bufs[n] = buf; st.peaks[n] = peaks(buf, 1200); draw();
    }
    render();
  } catch (e) { msg('Could not load stems: ' + e); }
}
function peaks(buf, n) {
  const d = buf.getChannelData(0), out = new Float32Array(n), step = Math.max(1, Math.floor(d.length / n));
  for (let i = 0; i < n; i++) { let m = 0; for (let j = i * step, e = Math.min(d.length, j + step); j < e; j += 4) { const a = Math.abs(d[j]); if (a > m) m = a; } out[i] = m; }
  return out;
}
function ensureCtx() {
  if (st.ctx) return;
  st.ctx = new (window.AudioContext || window.webkitAudioContext)();
  st.master = st.ctx.createGain(); st.master.connect(st.ctx.destination); setMaster();
}
function setMaster() { if (st.master) st.master.gain.value = el('vol').value / 100; }
el('vol').addEventListener('input', setMaster);

function render() {
  const s = st.status, has = !!(S.sel && s);
  el('stems-ctl').classList.toggle('hidden', !st.open || !has);
  lanesEl.classList.toggle('hidden', !st.open || !st.stems.length);
  el('stems-toggle').innerHTML = 'A1 stems ' + (st.open ? '&#x25BE;' : '&#x25B8;');
  el('stems-toggle').setAttribute('aria-expanded', st.open);
  el('stems-info').textContent = !S.sel ? '' : st.stems.length ? `${st.stems.length} stems (${st.provider === 'demucs' ? 'Demucs' : 'built-in'})` : 'not separated';
  if (!has) return;
  const running = s.state === 'running' || (st.job && st.job.status === 'running');
  const inst = s.capabilities && s.capabilities.install || {};
  const demOK = s.capabilities && s.capabilities.demucs_installed;
  el('stems-go').classList.toggle('hidden', running); el('stems-go').textContent = st.stems.length ? 'Re-separate' : 'Separate stems';
  el('stems-cancel').classList.toggle('hidden', !running);
  const needInst = !demOK && inst.status !== 'running';
  el('stems-install').classList.toggle('hidden', !needInst);
  el('stems-prov').querySelector('[value=demucs]').disabled = !demOK;
  el('stems-prog').classList.toggle('hidden', !(running || inst.status === 'running'));
  if (inst.status === 'running') { el('stems-bar').style.width = ((inst.progress || 0) * 100) + '%'; msg(inst.message || 'Installing Demucs...'); }
  if (!lanesEl.dataset.k || lanesEl.dataset.k !== st.stems.join()) buildLanes();
  lanesEl.querySelectorAll('.stem-lane').forEach(l => {
    const n = l.dataset.n; l.classList.toggle('dim', !audible(n));
    l.querySelector('.m').classList.toggle('on', !!st.mute[n]); l.querySelector('.s').classList.toggle('on', !!st.solo[n]);
  });
  applyMuting();
}
function buildLanes() {
  lanesEl.dataset.k = st.stems.join(); lanesEl.innerHTML = '';
  const strip = document.createElement('canvas'); strip.className = 'stems-tex'; strip.id = 'stems-tex'; strip.height = 6; lanesEl.appendChild(strip);
  for (const n of st.stems) {
    const l = document.createElement('div'); l.className = 'stem-lane'; l.dataset.n = n;
    l.innerHTML = `<div class="stem-hd"><b>${n}</b><button class="m" title="Mute">M</button><button class="s" title="Solo">S</button><input type="range" min="0" max="100" value="${st.vol[n] == null ? 100 : st.vol[n]}" title="Volume"></div><canvas></canvas>`;
    l.querySelector('.m').onclick = () => { st.mute[n] = !st.mute[n]; render(); };
    l.querySelector('.s').onclick = () => { st.solo[n] = !st.solo[n]; render(); };
    l.querySelector('input').oninput = e => { st.vol[n] = +e.target.value; applyMuting(); };
    lanesEl.appendChild(l);
  }
  draw();
}
function applyMuting() {
  for (const n of st.stems) { const g = st.gains[n]; if (g) g.gain.value = audible(n) ? (st.vol[n] == null ? 100 : st.vol[n]) / 100 : 0; }
}

// ---------- jobs ----------
async function pollJob() {
  clearTimeout(st.poll);
  const p = st.path; if (!p) return;
  let s; try { s = await (await fetch('/api/stems/status?song_path=' + q(p))).json(); } catch (e) { return; }
  if (st.path !== p) return;
  st.status = s; const j = s.running || s.last;
  if (s.state === 'running' && j) { el('stems-prog').classList.remove('hidden'); el('stems-bar').style.width = ((j.progress || 0) * 100) + '%'; msg(j.message || 'Separating...'); render(); st.poll = setTimeout(pollJob, 500); return; }
  el('stems-prog').classList.add('hidden'); st.job = null;
  if (s.state === 'ready') { msg(''); await loadStems(s.provider, s.stems, p); }
  else msg(j && j.status === 'error' ? 'Separation failed: ' + j.error : j && j.status === 'cancelled' ? 'Cancelled.' : '');
  render();
}
el('stems-go').onclick = async () => {
  if (!S.sel) return; msg('Starting...');
  const r = await post('/api/stems', {song_path: S.sel.path, provider: el('stems-prov').value});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { msg(r.status === 409 ? 'Demucs is not installed - use Install Demucs.' : r.status === 507 ? 'Not enough free disk space.' : (j.detail || 'Failed (' + r.status + ')')); return; }
  st.job = j; if (j.status === 'done') { st.status = null; refresh(); } else { msg(''); pollJob(); }
};
el('stems-cancel').onclick = async () => { if (S.sel) { await post('/api/stems/cancel', {song_path: S.sel.path}); pollJob(); } };
el('stems-install').onclick = async () => {
  const r = await post('/api/stems/install'); if (!r.ok) { msg('Install could not start (' + r.status + ')'); return; }
  const loop = async () => {
    const s = await (await fetch('/api/stems/install')).json();
    el('stems-prog').classList.remove('hidden'); el('stems-bar').style.width = ((s.progress || 0) * 100) + '%'; msg(s.message || 'Installing Demucs...');
    if (s.status === 'running') setTimeout(loop, 1500); else { el('stems-prog').classList.add('hidden'); msg(s.status === 'error' ? 'Install failed: ' + s.error : 'Demucs installed.'); refresh(); }
  }; loop();
};
el('stems-toggle').onclick = () => { st.open = !st.open; if (!st.open) stopSrcs(); render(); if (st.open) { refresh(); } draw(); };
el('stems-mix').onchange = () => { stopSrcs(); applyMuting(); };

// ---------- drawing ----------
const TEXC = ['#3b3f4c', '#8b5cf6', '#ff3b4e', '#22d3ee', '#facc15'];
function draw() {
  if (!st.open || !st.stems.length) return;
  const dur = S.dur || 1, off = spliceActive() ? 0 : S.offset, f = st.feats, now = vid.currentTime;
  lanesEl.querySelectorAll('.stem-lane').forEach(l => {
    const n = l.dataset.n, c = l.querySelector('canvas'), r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr); if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    const g = c.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, r.width, r.height);
    const col = COL[n] || '#8b8f9c', W2 = r.width, H2 = r.height, sd = (f && f.duration) || 1;
    const pk = st.peaks[n];
    // active tint from texture timeline
    if (f && f.texture_sections) for (const s of f.texture_sections) {
      if (s.active && s.active.indexOf(n) >= 0) { g.fillStyle = col + '1f'; const x0 = (s.start - off) / dur * W2, x1 = (s.end - off) / dur * W2; g.fillRect(x0, 0, x1 - x0, H2); }
    }
    if (pk) { g.fillStyle = col + 'cc'; for (let x = 0; x < W2; x++) { const t = x / W2 * dur + off; if (t < 0 || t > sd) continue; const a = pk[Math.min(pk.length - 1, Math.floor(t / sd * pk.length))]; const hh = Math.max(1, a * H2 * 0.9); g.fillRect(x, (H2 - hh) / 2, 1, hh); } }
    const fn = f && f.stems && f.stems[n];
    if (fn && fn.onsets) { g.fillStyle = '#fff'; for (const t of fn.onsets) { const x = (t - off) / dur * W2; if (x >= 0 && x <= W2) g.fillRect(x, 0, 1, 5); } }
    if (f && f.quiet_detail) for (const d of f.quiet_detail) if (d.stem === n) {
      const x = (d.t - off) / dur * W2; if (x < 0 || x > W2) continue;
      const gr = g.createRadialGradient(x, H2 / 2, 0, x, H2 / 2, 9); gr.addColorStop(0, '#ffffffee'); gr.addColorStop(1, col + '00'); g.fillStyle = gr; g.beginPath(); g.arc(x, H2 / 2, 9, 0, 7); g.fill();
    }
    g.fillStyle = '#fff'; g.fillRect(now / dur * W2, 0, 1.5, H2);
  });
  const sc = el('stems-tex');
  if (sc && f && f.texture_sections) {
    const r = sc.getBoundingClientRect(); sc.width = r.width; const g = sc.getContext('2d'); g.clearRect(0, 0, sc.width, 6);
    const names = st.stems;
    for (const s of f.texture_sections) { const k = (s.active || []).length; g.fillStyle = TEXC[Math.min(4, k)]; const x0 = (s.start - off) / dur * sc.width, x1 = (s.end - off) / dur * sc.width; g.fillRect(x0, 0, x1 - x0, 6); }
  }
}

// ---------- Web Audio mix ----------
function stopSrcs() { for (const s of st.srcs) { try { s.stop(); } catch (e) {} try { s.disconnect(); } catch (e) {} } st.srcs = []; applyAudMute(); }
function applyAudMute() { aud.muted = st.srcs.length > 0; }
function startSrcs(songT) {
  stopSrcs(); ensureCtx(); if (st.ctx.state === 'suspended') st.ctx.resume();
  if (songT < 0) return;
  st.gains = {}; st.srcRate = S.rate;
  for (const n of st.stems) {
    const b = st.bufs[n]; if (songT >= b.duration) continue;
    const src = st.ctx.createBufferSource(); src.buffer = b; src.playbackRate.value = S.rate;
    const g = st.ctx.createGain(); st.gains[n] = g; src.connect(g); g.connect(st.master); src.start(0, songT); st.srcs.push(src);
  }
  st.srcT0 = st.ctx.currentTime; st.srcS0 = songT; applyMuting(); applyAudMute();
}
function sync() {
  try {
    const on = mixOn() && S.playing && S.rate > 0 && !spliceActive() && S.sel && S.sel.source !== undefined;
    if (on) {
      const sT = songTime();
      if (!st.srcs.length || st.srcRate !== S.rate) startSrcs(sT);
      else { const pos = st.srcS0 + (st.ctx.currentTime - st.srcT0) * st.srcRate; if (Math.abs(pos - sT) > 0.08) startSrcs(sT); }
    } else if (st.srcs.length) stopSrcs();
  } catch (e) { st.err = String(e && e.stack || e).slice(0, 300); }
}
// rAF is throttled or paused in background/occluded tabs, so the audio sync also runs on a timer
setInterval(sync, 60);
function tick() {
  sync();
  try { if (st.open) draw(); } catch (e) {}
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

// refresh when the selected song changes
let lastPath = null;
setInterval(() => { const p = S.sel && S.sel.path || null; if (p !== lastPath) { lastPath = p; st.bufs = {}; st.stems = []; st.provider = null; st.feats = null; msg(''); stopSrcs(); if (st.open || p) refresh(); } }, 400);
window.addEventListener('resize', draw);
window.__stems = st;
})();
