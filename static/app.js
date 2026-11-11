'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const vid = $('vid'), aud = $('aud'), cv = $('tl'), ctx = cv.getContext('2d');
const S = {video: null, videoPath: '', results: [], sel: null, wf: null, offset: 0, fps: 30, dur: 0,
           playing: false, rate: 1, loop: false, vidOk: false, score: null,
           inUse: null, selClip: null, linked: true, undo: null, a1Empty: false, sp: {on: false, max: 2, plan: null, url: '', path: '', seq: 0, busy: false, cache: {}}, a1label: '', cutMarks: 0};
window.S = S;   // top-level const is not a window property; other scripts read window.S
const jpost = (u, b) => fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
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
function spliceActive() {  // spliced audio (rendered wav) is playing in place of the single trim
  const p = S.sp.plan; return !!(S.sp.on && p && p.segments && p.cuts && p.cuts.length && p.improvement_pct >= 1 && S.sp.url && S.sel && S.sel === S.inUse && S.sp.path === S.sel.path);
}
function songTime() { const a = window.asSongT && window.asSongT(); if (a != null) return a; return spliceActive() ? vid.currentTime : vid.currentTime + S.offset; }  // song clock = video clock + offset (spliced wav starts at video time 0)
// Audio element follows the video clock. Hard seeks restart buffering (WebKit then plays only short bursts), so they are
// rate-limited (max 1 per 2 s unless the user seeks), skipped while the element is still seeking/buffering, and the drift
// threshold is generous; small drift is trimmed with a gentle playbackRate nudge instead. __audLog keeps the last events.
const AUD = {lastSeek: -1e9, lastRate: -1e9, log: [], seeks: 0, rates: 0, nudging: 0};
// Pitch must never follow playbackRate (WebView2 sounds pitchy/static otherwise): force it on both media elements.
[vid, aud].forEach(m => { m.preservesPitch = true; m.webkitPreservesPitch = true; m.mozPreservesPitch = true; });
window.__aud = AUD;
function audLog(k, v) { AUD.log.push([Math.round(performance.now()), k, v]); if (AUD.log.length > 300) AUD.log.shift(); }
function audSeek(t) { AUD.nudging = 0; AUD.lastSeek = performance.now(); AUD.seeks++; audLog('seek', +t.toFixed(3)); try { aud.currentTime = t; } catch (e) {} }
function audRate(r) { if (Math.abs(aud.playbackRate - r) < 1e-3) return; AUD.lastRate = performance.now(); AUD.rates++; audLog('rate', +r.toFixed(3)); aud.playbackRate = r; }
function syncAudio(force) {
  if (!S.sel) return;
  const st = songTime();
  if (st < 0 || st > (aud.duration || 1e9)) { if (!aud.paused) aud.pause(); return; }
  const d = aud.currentTime - st, ad = Math.abs(d), now = performance.now();   // d > 0: audio ahead of video
  if (force) { if (ad > 0.05 || aud.paused) audSeek(st); audRate(S.rate); }
  else if (aud.seeking || aud.readyState < 2) return;   // still buffering from the last seek: leave it alone
  else if (ad > 0.3 && now - AUD.lastSeek > 2000) { audSeek(st); audRate(S.rate); }
  else if (S.playing && S.rate === 1 && (ad > 0.15 || (AUD.nudging && ad > 0.03))) {
    // hysteresis: start trimming only on real drift (>150 ms), hold one constant rate until back within 30 ms, then restore 1.0
    const want = 1 - Math.sign(d) * 0.03;
    if (!AUD.nudging || Math.abs(aud.playbackRate - want) > 1e-3) { AUD.nudging = 1; audRate(want); }
  }
  else if (aud.playbackRate !== S.rate && (ad <= 0.03 || !S.playing || S.rate !== 1)) { AUD.nudging = 0; audRate(S.rate); }
  else if (ad <= 0.03) AUD.nudging = 0;
  if (S.playing && aud.paused && !aud.seeking) aud.play().catch(() => {});
}
function play() {
  if (!S.dur || !S.video) return;
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
// Only a genuine decode/format failure on a real src shows "can't preview". Clearing the src
// (load() with no src raises code 4) and transient network stalls (code 2) must not; those retry once.
let vidRetry = 0;
vid.addEventListener('error', () => {
  const src = vid.getAttribute('src'), code = vid.error && vid.error.code;
  if (!src) return;
  if (code === 2 && vidRetry++ < 2) { const t = vid.currentTime; vid.load(); vid.addEventListener('loadedmetadata', () => { vid.currentTime = t; }, { once: true }); return; }
  S.vidOk = false; $('vid-msg').classList.remove('hidden');
});
const vidGood = () => { vidRetry = 0; S.vidOk = true; $('vid-msg').classList.add('hidden'); };
['loadeddata', 'canplay', 'playing'].forEach(n => vid.addEventListener(n, vidGood));
document.addEventListener('keydown', e => {
  const t = e.target, tag = t.tagName || '';
  if (t.isContentEditable || (/INPUT|TEXTAREA/.test(tag) && t.type !== 'range' && t.type !== 'checkbox')) return;
  const k = e.key, mod = e.metaKey || e.ctrlKey, field = /INPUT|TEXTAREA/.test(tag);
  if (mod && !e.altKey && !e.shiftKey && (k === 'z' || k === 'Z')) { if (field) return; e.preventDefault(); undoRemoval(); return; }
  if (mod && e.altKey && e.code === 'KeyL') { e.preventDefault(); toggleLink(); return; }
  if (mod || e.altKey) return;
  if (k === 'Backspace' || k === 'Delete') { if (field) return; e.preventDefault(); deleteSel(); }
  else if (k === 'Escape') selectClip(null);
  else if (k === ' ') { e.preventDefault(); $('b-play').click(); }
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
  clearInterval(timer); pause(); clearUndo();
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
  let shownPartial = 0;
  const tick = async () => {
    try {
      const j = await (await fetch('/api/scan/' + id)).json();
      if (j.video && !S.video) loadVideo(j);
      $('progress-bar').style.width = (j.total ? 100 * j.done / j.total : 5) + '%';
      $('status').textContent = j.message || '';
      if (j.status === 'error') return fail(j.error);
      if (j.status === 'running' && (j.partial || []).length !== shownPartial) {   // fill the list as songs score (final ranking still comes with 'done')
        shownPartial = j.partial.length;
        S.results = j.partial.filter(r => r.eligible !== false).sort((a, b) => (b.natural_total_pct - a.natural_total_pct) || (b.sync_pct - a.sync_pct));
        renderResults({too_short: [], skipped: []});
      }
      if (j.status === 'done') {
        clearInterval(timer); $('scan').disabled = false; $('progress').classList.add('hidden');
        if (!S.video) loadVideo(j);
        S.results = j.results || []; S.inUse = null; renderResults(j); if (S.results[0]) selectSong(S.results[0]);
      }
    } catch (e) { fail(e.message); }
  };
  S.video = null; timer = setInterval(tick, 400); tick();
}
function loadVideo(j) {
  S.linked = true; S.video = j.video; S.videoPath = j.video_path; S.dur = j.video.duration; S.fps = j.video.fps || 30;
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
const metaText = r => (r.bpm ? r.bpm.toFixed(0) + ' BPM · ' : '') + (r.source === 'original' ? 'original, start locked at 0:00' : 'starts at ' + fmtMS(r.trim_start_s))
  + (r.video_use && r.video_use.label ? ' · ' + r.video_use.label : '');
function renderResults(j) {
  if (S.inUse) S.inUse = S.results.find(r => r.path === S.inUse.path) || null;
  S.short = j.too_short || []; S.skipped = j.skipped || [];
  const box = $('results'); box.innerHTML = '';
  if (!S.results.length) box.innerHTML = '<div class="muted pad">No readable audio (every file was unreadable).</div>';
  const order = S.results.map((r, i) => i).sort((a, b) => (S.results[b].source === 'original') - (S.results[a].source === 'original'));
  let rank = 0;
  order.forEach(i => {
    const r = S.results[i], orig = r.source === 'original';
    const d = document.createElement('div'); d.className = 'res' + (orig ? ' orig' : ''); d.dataset.i = i;
    d.innerHTML = ring(r.total_pct) + `<div class="rtxt"><div class="rname" title="${esc(r.name)}">${orig ? 'Original audio' : (++rank) + '. ' + esc(r.name)}${orig ? '<span class="chip">from video</span>' : ''}${r.dropped ? '<span class="chip">dropped</span>' : ''}</div>${orig ? `<div class="rsub">${esc((r.name.match(/\((.*)\)\s*$/) || [, ''])[1])}</div>` : ''}
      <div class="rmeta">${metaText(r)}</div></div><div class="pill"><span class="pp">${Math.round(r.total_pct)}%</span><button class="use" title="Use this audio for playback, splice and export">Use this audio</button></div>`;
    d.onclick = () => selectSong(r);
    d.querySelector('.use').onclick = e => { e.stopPropagation(); useAudio(r); };
    box.appendChild(d);
  });
  requestAnimationFrame(() => box.querySelectorAll('.fg').forEach(c => { c.style.strokeDashoffset = C * (1 - c.dataset.p / 100); }));
  markRows();
  const sh = j.too_short || [];
  $('short-wrap').classList.toggle('hidden', !sh.length); $('short-n').textContent = sh.length;
  $('short').innerHTML = sh.map(r => `<div>${esc(r.name)} <span class="muted">(${fmtMS(r.song_duration)} vs video ${fmtMS(r.video_duration)})</span></div>`).join('');
  const sk = j.skipped || [];
  $('skipped-wrap').classList.toggle('hidden', !sk.length); $('skip-n').textContent = sk.length;
  $('skipped').innerHTML = sk.map(r => `<div>${esc(r.name || r.path || r)} <span class="muted">${esc(r.reason || '')}</span></div>`).join('');
  if (!S.video && S.results.length) noVideoUI();
  updateFit();
  if (window.asAfterResults) window.asAfterResults();
}
function markRows() {
  document.querySelectorAll('.res').forEach(e => {
    const r = S.results[e.dataset.i], u = r === S.inUse;
    e.classList.toggle('sel', r === S.sel); e.classList.toggle('use', u);
    const b = e.querySelector('.use'); if (b) b.textContent = u ? '✓ In use' : 'Use this audio';
  });
  selUI();
}
async function selectSong(r) {
  pause(); S.sel = r; S.score = r; S.a1Empty = false;
  S.offset = r.best_offset_s || 0; S.manual = false;
  markRows(); applyAudioSrc();
  try { S.wf = await (await fetch('/api/waveform?path=' + encodeURIComponent(r.path))).json(); } catch (e) { S.wf = null; }
  if (S.sel !== r) return;
  buildPump(); readout(); seek(0); draw(); drawOv(); updateFit();
}
function useAudio(r) {
  S.inUse = r; S.sp.plan = null; S.sp.url = ''; markRows(); staleExport();
  const go = () => { updateFit(); if (S.sp.on && r.source !== 'original') runSplice(); };
  if (S.sel !== r) selectSong(r).then(go); else go();
}
function applyAudioSrc() {
  if (!S.sel) return;
  const t = (window.asAudioUrl && window.asAudioUrl()) || (spliceActive() ? S.sp.url : '/api/audio?path=' + encodeURIComponent(S.sel.path));
  if (aud.dataset.cur !== t) { aud.dataset.cur = t; aud.src = t; }
}

// ---------- smart splice / export ----------
const segKey = (path, segs) => path + '|' + JSON.stringify(segs.map(s => [s.src_start, s.src_end, s.dst_start]));
async function renderAudio(path, segs) {
  const key = segKey(path, segs); if (S.sp.cache[key]) return S.sp.cache[key];
  const r = await jpost('/api/render-audio', {video_path: S.videoPath, song_path: path, segments: segs, crossfade_ms: 40});
  const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
  return S.sp.cache[key] = j;
}
async function runSplice() {
  const u = S.inUse, my = ++S.sp.seq; S.sp.plan = null; S.sp.url = ''; staleExport();
  if (!S.sp.on || !u || u.source === 'original' || !S.videoPath) { applyAfterSplice(); return; }
  S.sp.busy = true; updateFit(); $('fit-msg').textContent = 'Finding the best cuts...';
  try {
    const r = await jpost('/api/splice', {video_path: S.videoPath, song_path: u.path, max_cuts: S.sp.max, crossfade_ms: 40});
    const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
    if (my !== S.sp.seq) return;
    S.sp.plan = j; S.sp.path = u.path;
    if (j.eligible !== false && j.segments && j.cuts && j.cuts.length && j.improvement_pct >= 1) {
      const rd = await renderAudio(u.path, j.segments); if (my !== S.sp.seq) return;
      S.sp.url = rd.url;
    }
  } catch (e) { if (my !== S.sp.seq) return; S.sp.plan = null; note(e.message, true); }
  S.sp.busy = false; applyAfterSplice();
}
function applyAfterSplice() {
  const was = !aud.paused; pause(); applyAudioSrc(); buildPump(); readout(); seek(vid.currentTime); draw(); drawOv(); updateFit();
  if (was) play();
}
function updateFit() {
  const u = S.inUse, orig = !!u && u.source === 'original', off = !u || orig || !S.video;
  $('sp-on').disabled = off; $('sp-on').checked = S.sp.on && !off;
  $('fit-src').textContent = u ? u.name : '';
  const tip = orig ? 'Original audio: start locked at 0:00, splicing is off (use Auto-sync to fit it)' : (!u ? 'Press "Use this audio" on a source first' : '');
  $('sp-sw').title = tip; $('sp-max').title = tip;
  document.querySelectorAll('#sp-max button').forEach(b => { b.disabled = off || !S.sp.on; b.classList.toggle('on', +b.dataset.n === S.sp.max); });
  $('ex-wav').disabled = $('ex-mp4').disabled = !u || !S.video || S.exporting; staleExport();
  const m = $('fit-msg'), p = S.sp.plan; let t;
  if (!u) t = S.a1Empty ? 'No audio on A1' : 'Press "Use this audio" on a source to fit and export it.';
  else if (!S.video) t = 'Needs a video: drop one to fit and export.';
  else if (orig) t = 'Original audio: start locked at 0:00. Run Auto-sync to retime the video to it.';
  else if (S.sp.busy) t = 'Finding the best cuts...';
  else if (!S.sp.on || !p) t = `Single trim: ${fmtMS(S.offset)} to ${fmtMS(S.offset + S.dur)} (${Math.round(u.total_pct)}%)`;
  else if (p.eligible === false) t = 'This song has no readable audio to splice.';
  else if (!(p.improvement_pct >= 1) || !p.cuts || !p.cuts.length) t = 'No splice needed: a single trim is already best';
  else t = `Fit: ${Math.round(p.baseline.total_pct)}% → ${Math.round(p.total_pct)}% with ${p.cuts.length} cut${p.cuts.length === 1 ? '' : 's'} (+${Math.round(p.improvement_pct)})`;
  m.textContent = t;
}
$('sp-on').onchange = () => { S.sp.on = $('sp-on').checked; runSplice(); };
document.querySelectorAll('#sp-max button').forEach(b => b.onclick = () => { S.sp.max = +b.dataset.n; if (S.sp.on) runSplice(); else updateFit(); });
function exportSegs() {
  if (spliceActive()) return S.sp.plan.segments;
  return [{src_start: S.offset, src_end: S.offset + S.dur, dst_start: 0}];
}
const baseName = p => String(p).split('/').pop();
function exLink(j, what) {
  $('ex-out').innerHTML = `<a class="dl" id="ex-link" href="${esc(j.url)}" download>Download ${esc(baseName(j.path))}</a><div class="muted">Saved to .exports/${esc(baseName(j.path))}</div>`;
  $('ex-out').dataset.kind = what; $('ex-out').dataset.sig = EXSIG;
}
function exSig() {
  const u = S.inUse; if (!u) return '';
  return [S.videoPath, u.path, JSON.stringify(spliceActive() ? S.sp.plan.segments : [S.offset, S.dur]), JSON.stringify(fxBody())].join('|');
}
let EXSIG = '';
function staleExport() {
  const o = $('ex-out'); if (!o.dataset.sig || S.exporting) return;
  if (o.dataset.sig !== exSig()) { o.textContent = ''; delete o.dataset.sig; delete o.dataset.kind; }
}
async function doExport(kind) {
  const u = S.inUse; if (!u || S.exporting) return;
  EXSIG = exSig(); S.exporting = true; updateFit(); $('ex-out').textContent = ''; $('ex-prog').classList.remove('hidden'); $('ex-bar').style.width = '8%';
  try {
    const rd = await renderAudio(u.path, exportSegs());
    if (kind === 'wav') { $('ex-bar').style.width = '100%'; exLink(rd, 'wav'); }
    else {
      const r = await jpost('/api/export-mp4', {video_path: S.videoPath, audio_path: rd.path, ...fxBody()});
      let j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
      const id = j.job_id;
      while (j.status === 'running') {
        await new Promise(z => setTimeout(z, 400)); j = await (await fetch('/api/scan/' + id)).json();
        $('ex-bar').style.width = Math.max(10, j.done || 0) + '%';
      }
      if (j.status === 'error') throw new Error(j.error || 'Export failed');
      $('ex-bar').style.width = '100%'; exLink(j, 'mp4');
    }
  } catch (e) { $('ex-out').textContent = e.message; note(e.message, true); }
  S.exporting = false; updateFit();
  setTimeout(() => $('ex-prog').classList.add('hidden'), 900);
  if (typeof refreshExports === 'function') refreshExports();
}
$('ex-wav').onclick = () => doExport('wav');
$('ex-mp4').onclick = () => doExport('mp4');

// ---------- effects (pump) ----------
let reduce = false; try { reduce = matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}
const fxBody = () => FX.boom > 0 ? {boom: FX.boom, decay: FX.decay, drops_only: FX.drops, shake: FX.shake} : {};   // baked into exports only when Boom is on
const FX = {boom: reduce ? 0 : 40, decay: 180, drops: false, shake: false};
window.FX = FX;
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
  if (spliceActive()) {  // map song-time events onto the assembled (video-time) clock
    const out = []; S.sp.plan.segments.forEach(g => EV.forEach(e => { if (e.t >= g.src_start && e.t < g.src_end) out.push({...e, t: g.dst_start + e.t - g.src_start}); })); EV = out;
  }
  EV.sort((a, b) => a.t - b.t);
}
function pumpAt(st) {
  const tau = FX.decay / 1000, boom = FX.boom / 100; let sc = 0, br = 0, kd = 0, di = -1;
  for (let i = 0; i < EV.length; i++) {
    const e = EV[i]; if (e.t > st) break; const k = Math.exp(-(st - e.t) / tau); if (k < 0.01) continue;
    const a = 0.03 * boom * e.s * (e.d ? 4 : 1) * k; if (a > sc) sc = a;
    if (e.d && k > br) { br = k; di = i; }
  }
  return {scale: 1 + sc, bright: 1 + 0.35 * boom * br, k: br, di};
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
    lastWall = now; if (window.asFrame) window.asFrame(); syncAudio(false);
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
function emptyBand(y, h, text) {
  ctx.save(); ctx.strokeStyle = 'rgba(255,255,255,.22)'; ctx.setLineDash([6, 5]); ctx.strokeRect(6.5, y + 4.5, W - 13, h - 9); ctx.setLineDash([]);
  ctx.fillStyle = '#8b8f9c'; ctx.font = '12px Inter,sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(text, W / 2, y + h / 2); ctx.restore();
}
// A1 colour = playback state: Original = green, Auto-synced = purple (tokens --a1-orig / --a1-synced).
function a1Synced() { return !!(window.AS && AS.mode === 'synced' && AS.curRun && AS.curRun()); }
function a1Pal() {
  return a1Synced() ? { hi: '#a78bfa', lo: '#5b21b6', tint: 'rgba(139,92,246,.10)', edge: 'rgba(167,139,250,.9)' }
                    : { hi: '#34d399', lo: '#0f766e', tint: 'rgba(52,211,153,.08)', edge: 'rgba(52,211,153,.8)' };
}
window.a1Synced = a1Synced;
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
  else emptyBand(y0, V1H, 'Drop video here');
  if (v && window.asDrawV1) window.asDrawV1(ctx, y0, V1H, t2x);
  // A1
  const a0 = RULER + V1H + 4, mid = a0 + (A1H - 8) / 2, w = S.wf;
  if (!w) emptyBand(a0, A1H - 8, 'Drop songs here, or click Use this audio');
  if (w) {
    const sp = spliceActive(), off = S.offset, eo = sp ? 0 : off;
    const segs = sp ? S.sp.plan.segments : [{src_start: off, src_end: off + D, dst_start: 0}];
    const g = ctx.createLinearGradient(0, a0, 0, a0 + A1H); const pal = a1Pal(); g.addColorStop(0, pal.hi); g.addColorStop(1, pal.lo);
    const pk = w.peaks, n = pk.length / 2, amp = (A1H - 12) / 2;
    ctx.save(); ctx.beginPath(); ctx.rect(0, a0, W, A1H - 8); ctx.clip();
    if (!sp) { const x0 = t2x(-off), x1 = t2x(w.duration - off); ctx.fillStyle = pal.tint; ctx.fillRect(x0, a0, x1 - x0, A1H - 8); }
    segs.forEach((sg, k) => {
      const len = sg.src_end - sg.src_start, xa = t2x(sg.dst_start), xb = t2x(sg.dst_start + len);
      if (sp) { ctx.fillStyle = k % 2 ? 'rgba(139,92,246,.20)' : 'rgba(34,211,238,.12)'; ctx.fillRect(xa, a0, xb - xa, A1H - 8); }
      ctx.fillStyle = g;
      for (let px = Math.max(0, Math.floor(xa)); px < Math.min(W, xb); px++) {
        const st = sg.src_start + (x2t(px) - sg.dst_start), st2 = sg.src_start + (x2t(px + 1) - sg.dst_start);
        if (st < 0 || st > w.duration) continue;
        const i0 = Math.floor(st / w.duration * n), i1 = Math.max(i0 + 1, Math.floor(st2 / w.duration * n));
        let mn = 1, mx = -1; for (let i = i0; i < i1 && i < n; i++) { mn = Math.min(mn, pk[2 * i]); mx = Math.max(mx, pk[2 * i + 1]); }
        if (mx >= mn) ctx.fillRect(px, mid - mx * amp, 1, Math.max(1, (mx - mn) * amp));
      }
      ctx.strokeStyle = 'rgba(255,255,255,.28)';
      (w.beat_times || []).forEach(bt => { if (bt < sg.src_start || bt >= sg.src_end) return; const x = t2x(sg.dst_start + bt - sg.src_start); ctx.beginPath(); ctx.moveTo(x, a0 + A1H - 18); ctx.lineTo(x, a0 + A1H - 8); ctx.stroke(); });
      ctx.strokeStyle = pal.edge; ctx.strokeRect(xa + .5, a0 + .5, xb - xa - 1, A1H - 9);
    });
    ctx.restore();
    ctx.shadowBlur = 0;
    if (sp) (S.sp.plan.cuts || []).forEach(c => {  // crossfade wedge + scissors at each cut
      const x = t2x(c.dst_time), ww = Math.max(6, t2x(0.04 * 2));
      ctx.fillStyle = 'rgba(255,255,255,.55)'; ctx.beginPath(); ctx.moveTo(x - ww / 2, a0 + A1H - 8); ctx.lineTo(x + ww / 2, a0 + A1H - 8); ctx.lineTo(x, a0 + A1H - 22); ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x, a0); ctx.lineTo(x, a0 + A1H - 8); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = '#fff'; ctx.font = '13px sans-serif'; ctx.fillText('\u2702', x - 6, a0 + 13);
    });
    // pump lane
    const p0 = RULER + V1H + A1H + 2, ph = PUMPH - 6; ctx.fillStyle = '#8b8f9c'; ctx.font = '9px sans-serif'; ctx.fillText('pump', 4, p0 + 9);
    const maxA = 0.12 * FX.boom / 100 || 1;
    EV.forEach(e => { const x = t2x(e.t - eo); if (x < 0 || x > W || FX.boom === 0) return;
      const h = ph * Math.min(1, (0.03 * FX.boom / 100 * e.s * (e.d ? 4 : 1)) / maxA);
      ctx.fillStyle = e.d ? '#22d3ee' : '#8b5cf6'; ctx.fillRect(x - 1, p0 + ph - h, 2, h); });
  }
  if (window.asDrawA1) window.asDrawA1(ctx, a0, A1H, t2x, W);
  if (window.mkDraw) window.mkDraw(ctx, RULER, V1H, A1H, t2x, W);  // F7: detected markers (legend.js)
  if (S.selClip) {  // selection outline
    ctx.save(); ctx.shadowColor = '#22d3ee'; ctx.shadowBlur = 14; ctx.strokeStyle = '#67e8f9'; ctx.lineWidth = 2;
    if (S.selClip !== 'a') ctx.strokeRect(1, RULER + 1, W - 2, V1H - 2);
    if (S.selClip !== 'v') ctx.strokeRect(1, a0 + 1, W - 2, A1H - 10);
    ctx.restore();
  }
  // playhead
  const px = t2x(vid.currentTime); ctx.shadowColor = '#ff3b4e'; ctx.shadowBlur = 10; ctx.strokeStyle = '#ff3b4e'; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke(); ctx.lineWidth = 1; ctx.shadowBlur = 0;
  ctx.fillStyle = '#ff3b4e'; ctx.beginPath(); ctx.moveTo(px - 6, 0); ctx.lineTo(px + 6, 0); ctx.lineTo(px, 9); ctx.fill();
}
// drag: ruler/V1 = seek; A1 clip = offset
let drag = null, scoreTimer = null;
const locked = () => !S.sel || S.sel.source === 'original' || spliceActive();
function setOffset(o) {
  S.offset = clampOff(o); S.manual = Math.abs(S.offset - (S.sel.best_offset_s || 0)) > 0.005;
  syncAudio(true); readout(); draw(); drawOv(); staleExport();
  clearTimeout(scoreTimer); scoreTimer = setTimeout(rescore, 120);
}
cv.addEventListener('pointerdown', e => {
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  const inV1 = y >= RULER && y < RULER + V1H, inA1 = y >= RULER + V1H && y < RULER + V1H + A1H;
  if (y >= RULER) selectClip(inV1 ? 'v' : inA1 ? 'a' : null);   // ruler clicks only seek
  if (inA1 && S.wf && !locked()) drag = {kind: 'clip', x, off: S.offset};
  else { drag = {kind: 'seek'}; seek(x2t(x)); }
  cv.setPointerCapture(e.pointerId);
});
cv.addEventListener('pointermove', e => {
  if (!drag) return; const x = e.clientX - cv.getBoundingClientRect().left;
  if (drag.kind === 'seek') return seek(x2t(x));
  setOffset(drag.off - x2t(x - drag.x));
});
cv.addEventListener('pointerup', () => { drag = null; });
function clampOff(o) { const mx = S.wf ? Math.max(0, S.wf.duration - S.dur) : 0; return Math.min(mx, Math.max(0, o)); }
function partsText(segs) {
  const f = segs.map(g => fmtMS(g.src_start) + '-' + fmtMS(g.src_end));
  return 'Song parts: ' + f.slice(0, 2).join(', ') + (f.length > 2 ? ', +' + (f.length - 2) : '');
}
function readout() {
  const r = S.sel, orig = r && r.source === 'original';
  $('vc-off').textContent = !r ? '--' : orig ? 'Original audio, start locked at 0:00' : spliceActive() ? 'Spliced from ' + (S.sp.plan.cuts.length + 1) + ' clips' : 'Starts at ' + fmtMS(S.offset);
  $('vc-chip').textContent = !r || orig || spliceActive() ? '' : S.manual ? 'manual' : 'auto-picked';
  $('vc-chip').classList.toggle('hidden', !$('vc-chip').textContent);
  $('vc-reset').classList.toggle('hidden', !r || orig || !S.manual);
  $('vc-trim').textContent = !r ? '--' : orig ? 'Whole track' : spliceActive() ? partsText(S.sp.plan.segments) : 'Use ' + fmtMS(S.offset) + ' to ' + fmtMS(S.offset + S.dur);
  { const lb = $('a1-lbl'); lb.dataset.mode = a1Synced() ? 'synced' : 'orig'; }
  let l = ''; if (r) l = orig ? 'Original audio (from video)' : spliceActive() ? 'Spliced song, ' + (S.sp.plan.cuts.length + 1) + ' clips' : `Song ${fmtMS(S.offset)} to ${fmtMS(S.offset + S.dur)} (${S.manual ? 'manual' : 'auto'} start)`;
  $('a1-lbl').textContent = l; $('a1-lbl').classList.toggle('hidden', !l);
  const hint = orig ? 'Original audio: start locked at 0:00, so there is no offset to drag.' : spliceActive() ? 'Smart splice is on: turn it off in Fit to drag the start.' : 'Drag the song clip on A1 (or the bracket in Song overview) to set a manual start. Keys: Space play/pause, \u2190/\u2192 frame, Shift+arrows 1 s, J/K/L shuttle.';
  $('tl-hint').textContent = hint + ' Backspace/Delete removes the selected clip, Cmd+Z undoes.';
}
$('vc-reset').onclick = () => { if (!S.sel) return; S.manual = false; S.offset = S.sel.best_offset_s || 0; syncAudio(true); readout(); draw(); drawOv(); staleExport(); rescore(); };
async function rescore() {
  if (!S.sel || !S.videoPath) return;
  const r0 = S.sel;
  try {
    const r = await jpost('/api/score', {video_path: S.videoPath, song_path: r0.path, offset: S.offset});
    const j = await r.json(); if (!r.ok) return; Object.assign(r0, j, {best_offset_s: r0.best_offset_s});
    const el = [...document.querySelectorAll('.res')].find(e => S.results[e.dataset.i] === r0);
    if (el) { const sp = el.querySelector('.pp'); if (sp) sp.textContent = Math.round(j.total_pct) + '%'; el.querySelector('.rmeta').textContent = `${r0.bpm ? r0.bpm.toFixed(0) + ' BPM \u00b7 ' : ''}starts at ${fmtMS(j.trim_start_s)}${S.manual ? ' (manual)' : ''}`; el.querySelector('.ring b').textContent = Math.round(j.total_pct) + '%'; const c = el.querySelector('.fg'); if (c) c.style.strokeDashoffset = C * (1 - j.total_pct / 100); }
    updateFit();
  } catch (e) {}
}

// ---------- song overview lane ----------
const ov = $('ov'), octx = ov.getContext('2d'); let OW = 0, OH = 0, odrag = null, ovShowAll = false;
function resizeOv() { const r = ov.getBoundingClientRect(), dpr = window.devicePixelRatio || 1; OW = r.width; OH = r.height; ov.width = OW * dpr; ov.height = OH * dpr; octx.setTransform(dpr, 0, 0, dpr, 0, 0); drawOv(); }
window.addEventListener('resize', resizeOv);
function ovView() {
  const T = S.wf.duration; if (!spliceActive() || ovShowAll) return [0, T, false];
  const g = S.sp.plan.segments, lo = Math.min(...g.map(x => x.src_start)), hi = Math.max(...g.map(x => x.src_end)), pad = (hi - lo) * 0.1;
  const v0 = Math.max(0, lo - pad), v1 = Math.min(T, hi + pad);
  return (v1 - v0) < T * 0.98 ? [v0, v1, true] : [0, T, false];
}
function drawOv() {
  const zb = $('ov-zoom'); zb.classList.add('hidden');
  if (!OW) return; octx.clearRect(0, 0, OW, OH); const w = S.wf; if (!w || !S.dur) return;
  const T = w.duration, [v0, v1, zoomed] = ovView(), V = v1 - v0, tx = t => (t - v0) / V * OW, mid = OH / 2, amp = OH / 2 - 4, pk = w.peaks, n = pk.length / 2;
  const sp = spliceActive(), segs = sp ? S.sp.plan.segments : [{src_start: S.offset, src_end: S.offset + S.dur}];
  if (sp) { zb.classList.remove('hidden'); zb.textContent = zoomed ? `Zoomed: ${fmtMS(v0)}-${fmtMS(v1)} · show all` : 'Showing all · zoom to parts'; zb.dataset.zoomed = zoomed ? '1' : ''; }
  const inWin = x => { const t = v0 + x / OW * V; return segs.some(g => t >= g.src_start && t <= g.src_end); };
  for (let px = 0; px < OW; px++) {
    const i0 = Math.floor((v0 + px / OW * V) / T * n), i1 = Math.max(i0 + 1, Math.floor((v0 + (px + 1) / OW * V) / T * n));
    let mn = 1, mx = -1; for (let i = i0; i < i1 && i < n; i++) { mn = Math.min(mn, pk[2 * i]); mx = Math.max(mx, pk[2 * i + 1]); }
    if (mx < mn) continue; octx.fillStyle = inWin(px) ? '#34d399' : 'rgba(150,160,180,.28)';
    octx.fillRect(px, mid - mx * amp, 1, Math.max(1, (mx - mn) * amp));
  }
  octx.fillStyle = '#e0faff'; (w.drops || []).forEach(d => { if (d < v0 || d > v1) return; const x = tx(d); octx.beginPath(); octx.moveTo(x, 2); octx.lineTo(x + 4, 6); octx.lineTo(x, 10); octx.lineTo(x - 4, 6); octx.fill(); });
  const labels = [], rowEnd = [];
  octx.font = 'bold 10px sans-serif';
  segs.forEach((g, k) => {
    const xa = tx(g.src_start), xb = tx(g.src_end);
    octx.fillStyle = 'rgba(34,211,238,.10)'; octx.fillRect(xa, 0, xb - xa, OH);
    octx.strokeStyle = sp ? (k % 2 ? '#8b5cf6' : '#22d3ee') : '#22d3ee'; octx.lineWidth = 2;
    octx.beginPath(); octx.moveTo(xa + 4, 1); octx.lineTo(xa + 1, 1); octx.lineTo(xa + 1, OH - 1); octx.lineTo(xa + 4, OH - 1); octx.moveTo(xb - 4, 1); octx.lineTo(xb - 1, 1); octx.lineTo(xb - 1, OH - 1); octx.lineTo(xb - 4, OH - 1); octx.stroke(); octx.lineWidth = 1;
    if (sp) {
      const txt = String(k + 1), lw = octx.measureText(txt).width + 4, lx = xa + 5; let row = 0;
      while (rowEnd[row] !== undefined && lx < rowEnd[row] + 2) row++;
      rowEnd[row] = lx + lw; const ly = 2 + row * 12;
      octx.fillStyle = 'rgba(10,12,20,.8)'; octx.fillRect(lx - 2, ly, lw, 11);
      octx.fillStyle = '#fff'; octx.fillText(txt, lx, ly + 9);
      labels.push({n: k + 1, x: lx - 2, y: ly, w: lw, h: 11});
    }
  });
  window.OVX = {brackets: sp ? segs.length : 0, labels, zoomed, view: [v0, v1]};
}
$('ov-zoom').onclick = () => { ovShowAll = !ovShowAll; drawOv(); };
const ovX = e => e.clientX - ov.getBoundingClientRect().left;
ov.addEventListener('pointerdown', e => {
  if (!S.wf || locked()) return; const T = S.wf.duration, t = ovX(e) / OW * T;
  const inside = t >= S.offset && t <= S.offset + S.dur;
  odrag = {x: ovX(e), off: S.offset, jump: !inside}; if (!inside) setOffset(t - S.dur / 2), odrag.off = S.offset;
  ov.setPointerCapture(e.pointerId);
});
ov.addEventListener('pointermove', e => { if (!odrag) return; setOffset(odrag.off + (ovX(e) - odrag.x) / OW * S.wf.duration); });
ov.addEventListener('pointerup', () => { odrag = null; });
const scrubTo = e => { const r = $('scrub').getBoundingClientRect(); seek((e.clientX - r.left) / r.width * S.dur); };
$('scrub').addEventListener('pointerdown', e => { $('scrub').setPointerCapture(e.pointerId); $('scrub').dragging = true; scrubTo(e); });
$('scrub').addEventListener('pointermove', e => { if ($('scrub').dragging) scrubTo(e); });
$('scrub').addEventListener('pointerup', () => { $('scrub').dragging = false; });

// ---------- select / delete / undo / link ----------
const linkable = () => !!(S.video && S.sel && S.sel.source === 'original');   // only a video + its own audio can be linked
const linkOn = () => S.linked && linkable();
function selectClip(c) {
  if (c === 'a' && !S.sel) c = null;
  if (c === 'v' && !S.video) c = null;
  if (c && linkOn()) c = 'both';
  S.selClip = c; selUI(); draw();
}
let hov = null;
function placeX(b, top, on) { b.style.left = (cv.offsetLeft + W - 26) + 'px'; b.style.top = (cv.offsetTop + top) + 'px'; b.classList.toggle('show', !!on); }
function xUI() {
  const c = S.selClip;
  placeX($('x-v'), RULER + 4, S.video && (c === 'v' || c === 'both' || hov === 'v'));
  placeX($('x-a'), RULER + V1H + 8, S.sel && (c === 'a' || c === 'both' || hov === 'a'));
}
function selUI() {
  let c = S.selClip;
  if ((c === 'v' && !S.video) || (c === 'a' && !S.sel)) c = null;
  if (c === 'both' && !(S.video && S.sel)) c = S.video ? 'v' : S.sel ? 'a' : null;
  if (c === 'both' && !linkOn()) c = 'a';
  S.selClip = c;
  const lk = linkOn();
  document.querySelector('.lbl.v1').classList.toggle('sel', c === 'v' || c === 'both');
  document.querySelector('.lbl.a1').classList.toggle('sel', c === 'a' || c === 'both');
  document.querySelectorAll('.lk').forEach(e => e.classList.toggle('hidden', !lk));
  const b = $('b-link'); b.classList.toggle('on', lk); b.disabled = !linkable();
  b.title = linkable() ? 'Linked selection (Cmd+Opt+L)' : 'Only a video and its own audio can be linked';
  xUI();
}
function toggleLink() {
  if (!linkable()) return;
  S.linked = !S.linked; if (!S.linked && S.selClip === 'both') S.selClip = 'a';
  selUI(); draw();
}
$('b-link').onclick = toggleLink;
cv.addEventListener('pointermove', e => {
  if (drag) return; const y = e.clientY - cv.getBoundingClientRect().top;
  hov = y >= RULER && y < RULER + V1H ? 'v' : y >= RULER + V1H && y < RULER + V1H + A1H ? 'a' : null; xUI();
});
cv.addEventListener('pointerleave', e => { if (!(e.relatedTarget && e.relatedTarget.classList && e.relatedTarget.classList.contains('x'))) { hov = null; xUI(); } });
['x-v', 'x-a'].forEach(id => { $(id).onpointerleave = () => { hov = null; xUI(); }; $(id).onclick = e => { e.stopPropagation(); selectClip(id === 'x-v' ? 'v' : 'a'); deleteSel(); }; });
$('stage').addEventListener('pointerdown', () => { if (S.video) selectClip('v'); });
document.addEventListener('pointerdown', e => {
  if (S.selClip && e.target.closest && !e.target.closest('#tl-wrap,#stage,.res,button,input,select,label,.toast,#ov-wrap')) selectClip(null);
});
window.addEventListener('resize', xUI);

function noVideoUI() {   // greys the scores: they were computed against a video that is gone
  document.querySelectorAll('.res').forEach(e => {
    e.classList.add('nov');
    if (!e.querySelector('.nv')) { const d = document.createElement('div'); d.className = 'nv'; d.textContent = 'needs a video'; e.querySelector('.rtxt').appendChild(d); }
  });
}
function clearUndo() { if (S.undo && S.undo.t) S.undo.t.close(); S.undo = null; }
function snapState(kind) {
  const vb = $('video-path');
  return {kind, video: S.video, videoPath: S.videoPath, dur: S.dur, fps: S.fps, sel: S.sel, score: S.score, wf: S.wf, offset: S.offset, manual: S.manual,
          inUse: S.inUse, results: S.results.slice(), short: S.short, skipped: S.skipped, linked: S.linked,
          sp: {on: S.sp.on, plan: S.sp.plan, url: S.sp.url, path: S.sp.path}, dvp: D.videoPath, vbox: vb.value, vreal: vb.dataset.real, vtitle: vb.title};
}
function dropAudio() {
  pause(); S.sel = null; S.score = null; S.wf = null; S.inUse = null; S.sp.plan = null; S.sp.url = ''; S.a1Empty = true;
  aud.removeAttribute('src'); aud.load(); aud.dataset.cur = '';
  vid.style.transform = ''; vid.style.filter = '';
  buildPump(); readout(); seek(0); draw(); drawOv(); updateFit(); markRows();
}
function dropVideo() {
  pause(); const keep = S.sel && S.sel.source === 'original' ? S.inUse : null;
  S.video = null; S.videoPath = ''; D.videoPath = ''; S.vidOk = false;
  vid.removeAttribute('src'); vid.load(); vid.style.transform = ''; vid.style.filter = '';
  $('vid-msg').classList.add('hidden'); $('empty').textContent = 'Drop video here'; $('empty').classList.remove('hidden');
  const vb = $('video-path'); vb.value = ''; delete vb.dataset.real; vb.title = '';
  ['vc-dur', 'vc-bpm', 'vc-cuts'].forEach(i => { $(i).textContent = '--'; }); $('vinfo').textContent = '';
  S.results = S.results.filter(r => r.source !== 'original');
  renderResults({too_short: S.short, skipped: S.skipped});
  if (keep) S.inUse = keep;
  seek(0); markRows(); updateFit(); readout();
}
function pushUndo(sn, text) {
  clearUndo();
  const t = toast(text, {ttl: 6000}), b = document.createElement('button');
  b.className = 'toast-undo'; b.textContent = 'Undo'; b.onclick = undoRemoval; t.el.insertBefore(b, t.el.lastChild);
  S.undo = {sn, t};
}
function deleteSel() {
  const c = S.selClip; if (!c) return;
  if ((c !== 'a' && !S.video) || (c !== 'v' && !S.sel)) return;
  const sn = snapState(c), vn = S.video ? (D.names[S.videoPath] || baseName(S.videoPath)) : '', an = S.sel ? (S.sel.source === 'original' ? 'original audio' : S.sel.name) : '';
  let msg;
  if (c === 'a') { dropAudio(); msg = `Removed ${an} from A1 ·`; }
  else if (c === 'v') { dropVideo(); msg = `Removed ${vn} from V1 ·`; }
  else { dropAudio(); dropVideo(); msg = `Removed ${vn} and its original audio ·`; }
  S.selClip = null; selUI(); draw(); pushUndo(sn, msg);
}
function undoRemoval() {
  const u = S.undo; if (!u) return; const sn = u.sn; clearUndo(); pause();
  if (sn.kind !== 'a') {
    S.results = sn.results; S.short = sn.short; S.skipped = sn.skipped;
    loadVideo({video: sn.video, video_path: sn.videoPath}); S.linked = sn.linked;
    D.videoPath = sn.dvp; const vb = $('video-path'); vb.value = sn.vbox; vb.title = sn.vtitle || '';
    if (sn.vreal) vb.dataset.real = sn.vreal; else delete vb.dataset.real;
  }
  if (sn.kind !== 'v') {
    S.sel = sn.sel; S.score = sn.score; S.wf = sn.wf; S.offset = sn.offset; S.manual = sn.manual; S.inUse = sn.inUse; S.a1Empty = false;
    Object.assign(S.sp, sn.sp); aud.dataset.cur = ''; applyAudioSrc();
  } else if (S.sel === sn.sel && sn.sel && sn.sel.source === 'original') S.inUse = sn.inUse;
  renderResults({too_short: S.short, skipped: S.skipped});
  buildPump(); readout(); seek(0); draw(); drawOv(); updateFit(); markRows();
  selectClip(sn.kind);
}
resize(); resizeOv(); selUI();
