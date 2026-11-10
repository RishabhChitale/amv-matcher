'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const vid = $('vid'), aud = $('aud'), cv = $('tl'), ctx = cv.getContext('2d');
const S = {video: null, videoPath: '', results: [], sel: null, wf: null, offset: 0, fps: 30, dur: 0,
           playing: false, rate: 1, loop: false, vidOk: false, score: null};
const fmtMS = s => { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };

// ---------- timecode / transport ----------
function tcStr(t) {
  const fps = Math.round(S.fps) || 30; const f = Math.floor(t * fps + 1e-6);
  const fr = f % fps, s = Math.floor(f / fps);
  const p = n => String(n).padStart(2, '0');
  return `${p(Math.floor(s / 3600))}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}:${p(fr)}`;
}
function seek(t) {
  t = Math.min(Math.max(0, t), S.dur || 0);
  vid.currentTime = t; syncAudio(true); update();
}
function songTime() { return vid.currentTime + S.offset; }  // song clock = video clock + offset
function syncAudio(force) {
  if (!S.sel) return;
  const st = songTime();
  if (st < 0 || st > (aud.duration || 1e9)) { if (!aud.paused) aud.pause(); return; }
  if (force || Math.abs(aud.currentTime - st) > 0.03) aud.currentTime = st;
  if (S.playing && aud.paused) aud.play().catch(() => {});
}
function play() {
  if (!S.dur) return;
  S.playing = true; $('b-play').innerHTML = '&#x23F8;';
  if (vid.currentTime >= S.dur - 0.05) vid.currentTime = 0;
  vid.playbackRate = aud.playbackRate = S.rate;
  if (S.rate > 0) vid.play().catch(() => {});
  syncAudio(true); lastWall = performance.now();
}
function pause() { S.playing = false; $('b-play').innerHTML = '&#x25B6;'; vid.pause(); aud.pause(); }
function stop() { pause(); S.rate = 1; seek(0); }
$('b-play').onclick = () => S.playing ? pause() : (S.rate = 1, play());
$('b-stop').onclick = stop;
$('b-start').onclick = () => seek(0);
$('b-end').onclick = () => seek(S.dur);
$('b-back').onclick = () => { pause(); seek(vid.currentTime - 1 / S.fps); };
$('b-fwd').onclick = () => { pause(); seek(vid.currentTime + 1 / S.fps); };
$('b-loop').onclick = () => { S.loop = !S.loop; $('b-loop').classList.toggle('on', S.loop); };
$('vol').oninput = () => { aud.volume = $('vol').value / 100; };
aud.volume = 0.8;
vid.addEventListener('ended', () => { if (S.loop) { seek(0); play(); } else pause(); });
vid.addEventListener('error', () => { S.vidOk = false; $('vid-msg').classList.remove('hidden'); });
vid.addEventListener('loadeddata', () => { S.vidOk = true; $('vid-msg').classList.add('hidden'); });
document.addEventListener('keydown', e => {
  if (/INPUT|TEXTAREA/.test(e.target.tagName) && e.target.type !== 'range' && e.target.type !== 'checkbox') return;
  const k = e.key;
  if (k === ' ') { e.preventDefault(); $('b-play').click(); }
  else if (k === 'ArrowLeft') { e.preventDefault(); pause(); seek(vid.currentTime - (e.shiftKey ? 1 : 1 / S.fps)); }
  else if (k === 'ArrowRight') { e.preventDefault(); pause(); seek(vid.currentTime + (e.shiftKey ? 1 : 1 / S.fps)); }
  else if (k === 'k' || k === 'K') pause();
  else if (k === 'l' || k === 'L') { S.rate = S.rate > 0 ? Math.min(S.rate * 2, 4) : 1; if (S.rate < 1) S.rate = 1; play(); }
  else if (k === 'j' || k === 'J') { S.rate = Math.max(0.5, S.rate / 2); play(); }
});
let lastWall = 0;

// ---------- scan ----------
let timer = null;
$('scan').onclick = async () => {
  clearInterval(timer); pause();
  $('scan').disabled = true; $('progress').classList.remove('hidden'); $('progress-bar').style.width = '0';
  $('status').textContent = 'Starting...';
  try {
    const r = await fetch('/api/scan', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({video_path: $('video-path').value.trim(), music_dir: $('music-dir').value.trim(), recursive: $('recursive').checked})});
    const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
    poll(j.job_id);
  } catch (e) { fail(e.message); }
};
function fail(m) { clearInterval(timer); $('scan').disabled = false; $('progress').classList.add('hidden'); $('status').textContent = m; $('status').title = m; }
function poll(id) {
  const tick = async () => {
    try {
      const j = await (await fetch('/api/scan/' + id)).json();
      if (j.video && !S.video) loadVideo(j);
      $('progress-bar').style.width = (j.total ? 100 * j.done / j.total : 5) + '%';
      $('status').textContent = j.message || '';
      if (j.status === 'error') return fail(j.error);
      if (j.status === 'done') {
        clearInterval(timer); $('scan').disabled = false; $('progress').classList.add('hidden');
        if (!S.video) loadVideo(j);
        S.results = j.results || []; renderResults(j); if (S.results[0]) selectSong(S.results[0]);
      }
    } catch (e) { fail(e.message); }
  };
  S.video = null; timer = setInterval(tick, 400); tick();
}
function loadVideo(j) {
  S.video = j.video; S.videoPath = j.video_path; S.dur = j.video.duration; S.fps = j.video.fps || 30;
  $('empty').classList.add('hidden'); $('vid-msg').classList.add('hidden');
  vid.src = '/api/video?path=' + encodeURIComponent(j.video_path);
  $('vinfo').textContent = `${fmtMS(S.dur)} | ${j.video.motion_tempo_bpm.toFixed(0)} BPM motion`;
  $('vc-dur').textContent = fmtMS(S.dur) + ' (' + S.dur.toFixed(1) + 's)'; $('vc-bpm').textContent = j.video.motion_tempo_bpm.toFixed(1) + ' BPM';
  $('vc-cuts').textContent = j.video.cut_count ?? (j.video.cut_times || []).length;
  update();
}

// ---------- results ----------
const C = 2 * Math.PI * 22;
function ring(p) {
  return `<div class="ring"><svg width="52" height="52"><defs><linearGradient id="g"><stop offset="0" stop-color="#8b5cf6"/><stop offset="1" stop-color="#22d3ee"/></linearGradient></defs>
  <circle class="bg" cx="26" cy="26" r="22"/><circle class="fg" cx="26" cy="26" r="22" stroke-dasharray="${C}" stroke-dashoffset="${C}" data-p="${p}"/></svg><b>${Math.round(p)}%</b></div>`;
}
function bars(r) {
  return `<div class="bars"><span>Tempo</span><div class="bar"><i style="width:${r.tempo_pct}%"></i></div>
  <span>Sync</span><div class="bar"><i style="width:${r.sync_pct}%"></i></div>
  <span>Arc %</span><div class="bar"><i style="width:${r.arc_pct}%"></i></div></div>`;
}
function renderResults(j) {
  const box = $('results'); box.innerHTML = '';
  if (!S.results.length) box.innerHTML = '<div class="muted pad">No eligible songs (all shorter than the video, or unreadable).</div>';
  S.results.forEach((r, i) => {
    const d = document.createElement('div'); d.className = 'res'; d.dataset.i = i;
    d.innerHTML = ring(r.total_pct) + `<div><div class="rname" title="${esc(r.name)}">${i + 1}. ${esc(r.name)}</div>
      <div class="rmeta">${r.bpm.toFixed(0)} BPM | Use ${fmtMS(r.trim_start_s)} to ${fmtMS(r.trim_end_s)}</div>${bars(r)}</div>`;
    d.onclick = () => selectSong(r); box.appendChild(d);
  });
  requestAnimationFrame(() => box.querySelectorAll('.fg').forEach(c => { c.style.strokeDashoffset = C * (1 - c.dataset.p / 100); }));
  const sh = j.too_short || [];
  $('short-wrap').classList.toggle('hidden', !sh.length); $('short-n').textContent = sh.length;
  $('short').innerHTML = sh.map(r => `<div>${esc(r.name)} <span class="muted">(${fmtMS(r.song_duration)} vs video ${fmtMS(r.video_duration)})</span></div>`).join('');
  const sk = j.skipped || [];
  $('skipped-wrap').classList.toggle('hidden', !sk.length); $('skip-n').textContent = sk.length;
  $('skipped').innerHTML = sk.map(r => `<div>${esc(r.name || r.path || r)} <span class="muted">${esc(r.reason || '')}</span></div>`).join('');
}
async function selectSong(r) {
  pause(); S.sel = r; S.score = r;
  document.querySelectorAll('.res').forEach(e => e.classList.toggle('sel', S.results[e.dataset.i] === r));
  S.offset = r.best_offset_s || 0;
  aud.src = '/api/audio?path=' + encodeURIComponent(r.path);
  try { S.wf = await (await fetch('/api/waveform?path=' + encodeURIComponent(r.path))).json(); } catch (e) { S.wf = null; }
  buildPump(); readout(); seek(0); draw();
}

// ---------- effects (pump) ----------
let reduce = false; try { reduce = matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}
const FX = {boom: reduce ? 0 : 40, decay: 180, drops: false, shake: false};
try { const s = JSON.parse(localStorage.getItem('amv-fx') || 'null'); if (s) Object.assign(FX, s); } catch (e) {}
function saveFx() { try { localStorage.setItem('amv-fx', JSON.stringify(FX)); } catch (e) {} }
function fxUI() {
  $('fx-boom').value = FX.boom; $('fx-decay').value = FX.decay; $('fx-drops').checked = FX.drops; $('fx-shake').checked = FX.shake;
  $('fx-boom-v').textContent = FX.boom; $('fx-decay-v').textContent = FX.decay + ' ms';
}
[['fx-boom', 'boom', Number], ['fx-decay', 'decay', Number]].forEach(([id, k, f]) => $(id).oninput = () => { FX[k] = f($(id).value); saveFx(); fxUI(); buildPump(); draw(); });
$('fx-drops').onchange = () => { FX.drops = $('fx-drops').checked; saveFx(); buildPump(); draw(); };
$('fx-shake').onchange = () => { FX.shake = $('fx-shake').checked; saveFx(); };
fxUI();
/* PUMP FORMULA (identical to amv_matcher/pump.py):
   beat i at b_i, strength s_i (drops: 1): amp = 0.03*(boom/100)*s (drop: x4 -> 0.12*boom/100);
   k(t) = exp(-(t-b)/(decay_ms/1000)) for t>=b; scale = 1 + max(amp*k); drop brightness = 1 + 0.35*max(k);
   shake (drops only) = random +-(2..6) px * k.  drops_only ignores ordinary beats; boom=0 => scale 1. */
let EV = [];
function buildPump() {
  EV = []; const w = S.wf; if (!w) return;
  const dr = w.drops || [], bt = w.beat_times || [], st = w.beat_strengths || [];
  bt.forEach((b, i) => { const d = dr.some(x => Math.abs(x - b) <= 0.15); if (d || !FX.drops) EV.push({t: b, s: d ? 1 : (st[i] ?? 1), d}); });
  dr.forEach(x => { if (!EV.some(e => Math.abs(e.t - x) <= 0.15)) EV.push({t: x, s: 1, d: true}); });
  EV.sort((a, b) => a.t - b.t);
}
function pumpAt(st) {
  const tau = FX.decay / 1000, boom = FX.boom / 100; let sc = 0, br = 0, kd = 0, di = -1;
  for (let i = 0; i < EV.length; i++) {
    const e = EV[i]; if (e.t > st) break; const k = Math.exp(-(st - e.t) / tau); if (k < 0.01) continue;
    const a = 0.03 * boom * e.s * (e.d ? 4 : 1) * k; if (a > sc) sc = a;
    if (e.d && k > br) { br = k; di = i; }
  }
  return {scale: 1 + sc, bright: 1 + 0.35 * (boom / 100) * br, k: br, di};
}
function applyPump() {
  if (!S.sel || !S.wf || FX.boom === 0) { vid.style.transform = ''; vid.style.filter = ''; return; }
  const p = pumpAt(songTime()); let tx = '';
  if (FX.shake && p.k > 0.02) {
    const r = (i, n) => { const x = Math.sin(i * 12.9898 + n * 78.233) * 43758.5453; return x - Math.floor(x); };
    const mag = (2 + 4 * r(p.di, 1)) * p.k; tx = ` translate(${((r(p.di, 2) - .5) * 2 * mag).toFixed(1)}px,${((r(p.di, 3) - .5) * 2 * mag).toFixed(1)}px)`;
  }
  vid.style.transform = `scale(${p.scale.toFixed(4)})${tx}`; vid.style.filter = p.bright > 1.001 ? `brightness(${p.bright.toFixed(3)})` : '';
}

// ---------- main loop ----------
function update() {
  $('tc').textContent = tcStr(vid.currentTime);
  const f = S.dur ? vid.currentTime / S.dur : 0;
  $('scrub-fill').style.width = (f * 100) + '%'; $('scrub-head').style.left = (f * 100) + '%';
  draw();
}
function frame(now) {
  if (S.playing) {
    if (!S.vidOk || S.rate < 0) { vid.currentTime = Math.min(S.dur, vid.currentTime + (now - lastWall) / 1000 * S.rate); }
    lastWall = now; syncAudio(false);
    if (vid.currentTime >= S.dur - 0.02 && S.dur) { if (S.loop) { seek(0); } else pause(); }
    update();
  }
  applyPump(); requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---------- timeline canvas ----------
const PAD = 0, RULER = 26, V1H = 84, A1H = 84, PUMPH = 28;
let W = 0, H = 0;
function resize() {
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  W = r.width; H = r.height; cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); draw();
}
window.addEventListener('resize', resize);
const x2t = x => x / W * S.dur, t2x = t => t / (S.dur || 1) * W;
function draw() {
  if (!W) return; ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = 'rgba(0,0,0,.25)'; ctx.fillRect(0, RULER, W, V1H + A1H + PUMPH);
  const D = S.dur || 0; if (!D) return;
  // ruler
  ctx.fillStyle = '#8b8f9c'; ctx.font = '10px ui-monospace,Menlo,monospace'; ctx.strokeStyle = 'rgba(255,255,255,.15)';
  const step = D > 600 ? 60 : D > 120 ? 20 : D > 40 ? 5 : 1;
  for (let t = 0; t <= D; t += step) { const x = t2x(t); ctx.beginPath(); ctx.moveTo(x, 16); ctx.lineTo(x, RULER); ctx.stroke(); ctx.fillText(fmtMS(t), x + 3, 12); }
  // V1
  const v = S.video, y0 = RULER;
  if (v) {
    (v.segments || []).forEach(s => {
      ctx.fillStyle = s.label === 'peak' ? 'rgba(255,59,78,.16)' : s.label === 'calm' ? 'rgba(34,211,238,.10)' : 'rgba(139,92,246,.10)';
      ctx.fillRect(t2x(s.start), y0, t2x(s.end) - t2x(s.start), V1H);
    });
    const ec = v.energy_curve || [], hz = v.energy_hz || 10;
    const g = ctx.createLinearGradient(0, y0, 0, y0 + V1H); g.addColorStop(0, 'rgba(139,92,246,.85)'); g.addColorStop(1, 'rgba(34,211,238,.15)');
    ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(0, y0 + V1H);
    ec.forEach((e, i) => ctx.lineTo(t2x(i / hz), y0 + V1H - e * (V1H - 6))); ctx.lineTo(t2x(ec.length / hz), y0 + V1H); ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,.5)'; (v.cut_times || []).forEach(t => { const x = t2x(t); ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y0 + 10); ctx.stroke(); });
  }
  // A1
  const a0 = RULER + V1H + 4, mid = a0 + (A1H - 8) / 2, w = S.wf;
  if (w) {
    const off = S.offset, x0 = t2x(-off), x1 = t2x(w.duration - off);
    ctx.save(); ctx.beginPath(); ctx.rect(0, a0, W, A1H - 8); ctx.clip();
    ctx.fillStyle = 'rgba(34,211,238,.07)'; ctx.fillRect(x0, a0, x1 - x0, A1H - 8);
    const g = ctx.createLinearGradient(0, a0, 0, a0 + A1H); g.addColorStop(0, '#34d399'); g.addColorStop(1, '#0f766e');
    ctx.fillStyle = g; const pk = w.peaks, n = pk.length / 2, amp = (A1H - 12) / 2;
    for (let px = 0; px < W; px++) {
      const st = x2t(px) + off; if (st < 0 || st > w.duration) continue;
      const i0 = Math.floor(st / w.duration * n), i1 = Math.max(i0 + 1, Math.floor((x2t(px + 1) + off) / w.duration * n));
      let mn = 1, mx = -1; for (let i = i0; i < i1 && i < n; i++) { mn = Math.min(mn, pk[2 * i]); mx = Math.max(mx, pk[2 * i + 1]); }
      if (mx >= mn) ctx.fillRect(px, mid - mx * amp, 1, Math.max(1, (mx - mn) * amp));
    }
    ctx.strokeStyle = 'rgba(255,255,255,.28)'; (w.beat_times || []).forEach(b => { const x = t2x(b - off); if (x < 0 || x > W) return; ctx.beginPath(); ctx.moveTo(x, a0 + A1H - 18); ctx.lineTo(x, a0 + A1H - 8); ctx.stroke(); });
    ctx.restore();
    ctx.shadowColor = '#22d3ee'; ctx.shadowBlur = 12; ctx.fillStyle = '#e0faff';
    (w.drops || []).forEach(d => { const x = t2x(d - off); if (x < 0 || x > W) return; ctx.beginPath(); ctx.moveTo(x, mid - 7); ctx.lineTo(x + 6, mid); ctx.lineTo(x, mid + 7); ctx.lineTo(x - 6, mid); ctx.fill(); });
    ctx.shadowBlur = 0;
    ctx.strokeStyle = 'rgba(139,92,246,.8)'; ctx.strokeRect(x0 + .5, a0 + .5, x1 - x0 - 1, A1H - 9);
    // pump lane
    const p0 = RULER + V1H + A1H + 2, ph = PUMPH - 6; ctx.fillStyle = '#8b8f9c'; ctx.font = '9px sans-serif'; ctx.fillText('pump', 4, p0 + 9);
    const maxA = 0.12 * FX.boom / 100 || 1;
    EV.forEach(e => { const x = t2x(e.t - off); if (x < 0 || x > W || FX.boom === 0) return;
      const h = ph * Math.min(1, (0.03 * FX.boom / 100 * e.s * (e.d ? 4 : 1)) / maxA);
      ctx.fillStyle = e.d ? '#22d3ee' : '#8b5cf6'; ctx.fillRect(x - 1, p0 + ph - h, 2, h); });
  }
  // playhead
  const px = t2x(vid.currentTime); ctx.shadowColor = '#ff3b4e'; ctx.shadowBlur = 10; ctx.strokeStyle = '#ff3b4e'; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke(); ctx.lineWidth = 1; ctx.shadowBlur = 0;
  ctx.fillStyle = '#ff3b4e'; ctx.beginPath(); ctx.moveTo(px - 6, 0); ctx.lineTo(px + 6, 0); ctx.lineTo(px, 9); ctx.fill();
}
// drag: ruler/V1 = seek; A1 clip = offset
let drag = null, scoreTimer = null;
cv.addEventListener('pointerdown', e => {
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  if (y > RULER + V1H && y < RULER + V1H + A1H && S.wf) drag = {kind: 'clip', x, off: S.offset};
  else { drag = {kind: 'seek'}; seek(x2t(x)); }
  cv.setPointerCapture(e.pointerId);
});
cv.addEventListener('pointermove', e => {
  if (!drag) return; const x = e.clientX - cv.getBoundingClientRect().left;
  if (drag.kind === 'seek') return seek(x2t(x));
  S.offset = clampOff(drag.off - x2t(x - drag.x)); syncAudio(true); readout(); draw();
  clearTimeout(scoreTimer); scoreTimer = setTimeout(rescore, 120);
});
cv.addEventListener('pointerup', () => { drag = null; });
function clampOff(o) { const mx = S.wf ? Math.max(0, S.wf.duration - S.dur) : 0; return Math.min(mx, Math.max(0, o)); }
function readout() {
  $('vc-off').textContent = S.sel ? S.offset.toFixed(2) + ' s' : '--';
  $('vc-trim').textContent = S.sel ? 'Use ' + fmtMS(S.offset) + ' to ' + fmtMS(S.offset + S.dur) : '--';
}
async function rescore() {
  if (!S.sel || !S.videoPath) return;
  try {
    const r = await fetch('/api/score', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({video_path: S.videoPath, song_path: S.sel.path, offset: S.offset})});
    const j = await r.json(); if (!r.ok) return; Object.assign(S.sel, j, {best_offset_s: S.sel.best_offset_s});
    const el0 = document.querySelector('.res.sel'); if (el0) { const b = el0.querySelectorAll('.bar i'); [j.tempo_pct, j.sync_pct, j.arc_pct].forEach((v, k) => { if (b[k]) b[k].style.width = v + '%'; }); }
    const el = document.querySelector('.res.sel'); if (el) { const i = el.dataset.i; el.querySelector('.rmeta').innerHTML = `${S.sel.bpm.toFixed(0)} BPM | Use ${fmtMS(j.trim_start_s)} to ${fmtMS(j.trim_end_s)} (manual)`; el.querySelector('.ring b').textContent = Math.round(j.total_pct) + '%'; }
  } catch (e) {}
}
const scrubTo = e => { const r = $('scrub').getBoundingClientRect(); seek((e.clientX - r.left) / r.width * S.dur); };
$('scrub').addEventListener('pointerdown', e => { $('scrub').setPointerCapture(e.pointerId); $('scrub').dragging = true; scrubTo(e); });
$('scrub').addEventListener('pointermove', e => { if ($('scrub').dragging) scrubTo(e); });
$('scrub').addEventListener('pointerup', () => { $('scrub').dragging = false; });
resize();
