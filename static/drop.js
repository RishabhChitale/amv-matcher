'use strict';
// Desktop-app media flows (files are used in place by real path; nothing is copied or uploaded). Depends on app.js globals (S, $, esc, loadVideo, renderResults, selectSong, pause, draw...).
const D = {names: {}, videoPath: '', queue: [], dropped: new Set(), chain: Promise.resolve()};
const VEXT = ['.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'], AEXT = ['.mp3', '.wav', '.m4a', '.flac', '.ogg', '.aac'];
const extOf = n => { const i = n.lastIndexOf('.'); return i < 0 ? '' : n.slice(i).toLowerCase(); };
const kindOf = n => VEXT.includes(extOf(n)) ? 'video' : AEXT.includes(extOf(n)) ? 'song' : '';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fmtB = b => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB' : b >= 1e3 ? Math.round(b / 1e3) + ' KB' : b + ' B';
const POST = (u, b) => fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});

// ---------- toasts ----------
function toast(text, opts = {}) {
  const el = document.createElement('div'); el.className = 'toast' + (opts.err ? ' err' : '');
  el.innerHTML = '<span></span><i></i>'; $('toasts').appendChild(el);
  const t = {el, set(x, pct) { el.firstChild.textContent = x; if (pct != null) el.lastChild.style.width = pct + '%'; },
             close() { el.remove(); }};
  t.set(text, opts.pct); if (opts.ttl) setTimeout(t.close, opts.ttl);
  return t;
}
const note = (m, err) => toast(m, {err, ttl: err ? 6000 : 3500});

// ---------- score ----------
async function scoreSongs(videoPath, paths) {
  const r = await POST('/api/score-songs', {video_path: videoPath, song_paths: paths});
  let j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
  const id = j.job_id;
  while (j.status === 'running') {
    await sleep(400); j = await (await fetch('/api/scan/' + id)).json();
  }
  if (j.status === 'error') throw new Error(j.error || 'Scoring failed');
  return j;
}
const curVideo = () => S.video ? S.videoPath : D.videoPath;
const markDropped = r => { shown(r); if (D.dropped.has(r.path)) r.dropped = true; return r; };
const byScore = (a, b) => (b.total_pct - a.total_pct) || (b.sync_pct - a.sync_pct);

const shown = r => { if (r && D.names[r.path]) r.name = D.names[r.path]; return r; };
function setVideoBox(path) { const el = $('video-path'); if (path) el.value = path; el.title = path || ''; }
function applyVideo(j) {
  if (j.video && (!S.video || S.videoPath !== j.video_path)) { pause(); loadVideo({video: j.video, video_path: j.video_path}); }
  if (j.video_path) setVideoBox(j.video_path);
}
// Score `paths` against the current video; replace=true swaps the whole list, otherwise merge by path.
async function scoreAndShow(paths, replace) {
  const vp = curVideo(), t = toast(`Scoring ${paths.length} song${paths.length > 1 ? 's' : ''}...`);
  try {
    const j = await scoreSongs(vp, paths); applyVideo(j);
    const hadSel = S.sel && !replace ? S.sel.path : null;
    let results, short;
    if (replace) { results = (j.results || []).map(markDropped); short = (j.too_short || []).map(markDropped); }
    else {
      const seen = new Set(paths);
      results = S.results.filter(r => !seen.has(r.path)).concat((j.results || []).map(markDropped));
      short = (S.short || []).filter(r => !seen.has(r.path)).concat((j.too_short || []).map(shown));
    }
    results.sort(byScore); S.results = results;
    const first = results.find(r => D.dropped.has(r.path)) || results[0];
    if (replace && S.sel) {   // relink the song kept on A1 to its fresh score; a stale original-audio clip is dropped
      const keep = S.sel.source === 'original' ? null : results.find(r => r.path === S.sel.path);
      if (keep) { S.sel = keep; S.score = keep; if (!S.manual) S.offset = keep.best_offset_s || 0; } else { S.sel = null; S.wf = null; }
    }
    S.sel = hadSel ? (results.find(r => r.path === hadSel) || null) : S.sel;
    renderResults({too_short: short, skipped: (S.skipped || []).concat((j.skipped || []).map(shown))});
    $('status').textContent = j.message || '';
    if (!S.sel && first) selectSong(first);
    t.close();
    (j.skipped || []).forEach(s => note(`Skipped ${s.name || s.path}: ${s.reason || ''}`, true));
  } catch (e) { t.close(); note(e.message, true); }
}

// ---------- flows (items are {name, path}: real files used in place) ----------
const baseOf = p => String(p).split(/[\\/]/).pop();
const item = p => ({name: baseOf(p), path: p});
const OVERLAY = {show(msg) { const r = $('up-ring'); if (r) { r.classList.remove('hidden'); $('up-txt').textContent = msg; } }, hide() { const r = $('up-ring'); if (r) r.classList.add('hidden'); }};
async function doVideo(file) {
  const path = file.path;
  OVERLAY.show('Analyzing...');
  try {
    pause(); clearUndo();
    D.videoPath = path; setVideoBox(path); D.dropped.add(path);
    $('empty').classList.add('hidden');
    const known = [...new Set([...S.results, ...(S.short || [])].map(r => r.path).concat(D.queue))];
    if (known.length) { S.video = null; await scoreAndShow(known, true); D.queue = []; }
    else {
      try {
        const r = await POST('/api/analyze-video', {video_path: path});
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error || j.detail || `analyze-video ${r.status}`);
        applyVideo(j); S.video = j.video; S.videoPath = j.video_path;
        note('Video analyzed. Add songs to score them.');
      } catch (e) {
        vid.src = '/api/video?path=' + encodeURIComponent(path);
        note(e.message + ' (video loaded without analysis)', true);
      }
    }
  } catch (e) { note(e.message, true); }
  OVERLAY.hide();
}
async function doSongs(files) {
  const paths = files.map(f => f.path);
  paths.forEach(p => D.dropped.add(p));
  if (!paths.length) return;
  if (!curVideo()) {
    D.queue = D.queue.concat(paths);
    note('Pick a video first, then your songs will be scored', false);
    $('status').textContent = 'Pick a video first, then your songs will be scored';
    return;
  }
  await scoreAndShow(paths, false);
}
const enqueue = fn => { D.chain = D.chain.then(fn).then(refreshExports, e => { note(String(e), true); refreshExports(); }); return D.chain; };

// ---------- exports control ("Exports N MB · Clear exports") ----------
let exInfo = {exports_count: 0, exports_bytes: 0};
async function refreshExports() {
  try { exInfo = await (await fetch('/api/uploads')).json(); } catch (e) { return; }
  $('ex-size').textContent = fmtB(exInfo.exports_bytes ?? 0); $('ex-clear').disabled = !(exInfo.exports_count > 0);
}
$('ex-clear').onclick = () => {
  $('ex-pop-t').textContent = `Delete ${exInfo.exports_count} exported file${exInfo.exports_count === 1 ? '' : 's'} (${fmtB(exInfo.exports_bytes || 0)})? Your original videos and songs are not touched.`;
  $('ex-pop').classList.remove('hidden');
};
$('ex-no').onclick = () => $('ex-pop').classList.add('hidden');
$('ex-yes').onclick = async () => {
  $('ex-pop').classList.add('hidden');
  try {
    const r = await fetch('/api/exports', {method: 'DELETE'}); const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
    $('ex-out').innerHTML = '';
    note(`Freed ${fmtB(j.exports_freed ?? j.freed ?? 0)}`);
  } catch (e) { note(e.message, true); }
  refreshExports();
};
$('scan').addEventListener('click', () => { D.videoPath = ''; D.queue = []; D.dropped.clear(); });
refreshExports();
