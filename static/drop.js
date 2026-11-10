'use strict';
// Drag-and-drop / Browse uploads. Depends on app.js globals (S, $, esc, loadVideo, renderResults, selectSong, pause, draw...).
const D = {videoPath: '', queue: [], dropped: new Set(), chain: Promise.resolve()};
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

// ---------- upload / score ----------
function upload(file, kind, onp) {
  return new Promise((res, rej) => {
    const x = new XMLHttpRequest();
    x.open('POST', `/api/upload?kind=${kind}&name=${encodeURIComponent(file.name)}`);
    x.upload.onprogress = e => { if (e.lengthComputable) onp(e.loaded / e.total); };
    x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch (e) {}
      x.status >= 200 && x.status < 300 ? res(j) : rej(new Error(j.detail || ('Upload failed (' + x.status + ')'))); };
    x.onerror = () => rej(new Error('Upload failed (network)'));
    x.send(file);
  });
}
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
const markDropped = r => { if (D.dropped.has(r.path)) r.dropped = true; return r; };
const byScore = (a, b) => (b.total_pct - a.total_pct) || (b.sync_pct - a.sync_pct);

function applyVideo(j) {
  if (j.video && (!S.video || S.videoPath !== j.video_path)) { pause(); loadVideo({video: j.video, video_path: j.video_path}); }
  $('video-path').value = j.video_path || $('video-path').value;
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
      short = (S.short || []).filter(r => !seen.has(r.path)).concat(j.too_short || []);
    }
    results.sort(byScore); S.results = results;
    const first = results.find(r => D.dropped.has(r.path)) || results[0];
    S.sel = hadSel ? (results.find(r => r.path === hadSel) || null) : S.sel;
    renderResults({too_short: short, skipped: (S.skipped || []).concat(j.skipped || [])});
    $('status').textContent = j.message || '';
    if (!S.sel && first) selectSong(first);
    t.close();
    (j.skipped || []).forEach(s => note(`Skipped ${s.name || s.path}: ${s.reason || ''}`, true));
  } catch (e) { t.close(); note(e.message, true); }
}

// ---------- flows ----------
async function doVideo(file) {
  const ring = $('up-ring'), txt = $('up-txt'), fg = $('up-fg'), L = 2 * Math.PI * 38;
  ring.classList.remove('hidden');
  const setP = p => { fg.style.strokeDashoffset = L * (1 - p); txt.textContent = `Uploading ${file.name}: ${Math.round(p * 100)}%`; };
  try {
    setP(0); pause();
    const up = await upload(file, 'video', setP);
    D.videoPath = up.path; $('video-path').value = up.path; D.dropped.add(up.path);
    fg.style.strokeDashoffset = 0; txt.textContent = 'Analyzing...';
    $('empty').classList.add('hidden');
    const known = [...new Set([...S.results, ...(S.short || [])].map(r => r.path).concat(D.queue))];
    if (known.length) { S.video = null; await scoreAndShow(known, true); D.queue = []; }
    else {
      vid.src = '/api/video?path=' + encodeURIComponent(up.path);
      note('Video ready. Drop songs to score them.');
    }
  } catch (e) { note(e.message, true); }
  ring.classList.add('hidden');
}
async function doSongs(files) {
  const paths = []; let n = 0;
  for (const f of files) {
    n++; const t = toast(`${n}/${files.length} ${f.name} 0%`, {pct: 0});
    try {
      const up = await upload(f, 'song', p => t.set(`${n}/${files.length} ${f.name} ${Math.round(p * 100)}%`, p * 100));
      paths.push(up.path); D.dropped.add(up.path);
    } catch (e) { note(`${f.name}: ${e.message}`, true); }
    t.close();
  }
  if (!paths.length) return;
  if (!curVideo()) {
    D.queue = D.queue.concat(paths);
    note('Drop a video first, then your songs will be scored', false);
    $('status').textContent = 'Drop a video first, then your songs will be scored';
    return;
  }
  await scoreAndShow(paths, false);
}
const enqueue = fn => { D.chain = D.chain.then(fn).then(refreshUploads, e => { note(String(e), true); refreshUploads(); }); return D.chain; };

// ---------- folder / file extraction ----------
const readAll = rd => new Promise(res => { const out = [];
  const go = () => rd.readEntries(b => b.length ? (out.push(...b), go()) : res(out), () => res(out)); go(); });
async function walk(entry, out) {
  if (!entry) return;
  if (entry.isFile) await new Promise(r => entry.file(f => { out.push(f); r(); }, r));
  else if (entry.isDirectory) for (const c of await readAll(entry.createReader())) await walk(c, out);
}
async function filesFrom(dt) {
  const out = [], items = dt.items ? [...dt.items] : [];
  const ents = items.filter(i => i.kind === 'file').map(i => i.webkitGetAsEntry && i.webkitGetAsEntry());
  if (ents.length && ents.every(Boolean)) { for (const e of ents) await walk(e, out); return out; }
  return [...(dt.files || [])];
}
function route(files, target) {  // target: 'video' | 'song' | null (anywhere on the page)
  const vids = files.filter(f => kindOf(f.name) === 'video'), songs = files.filter(f => kindOf(f.name) === 'song');
  if (target === 'video' && !vids.length) return note('Not a video: ' + (files[0] ? files[0].name : 'nothing dropped'), true);
  if (target === 'song' && !songs.length) return note('Not an audio file: ' + (files[0] ? files[0].name : 'nothing dropped'), true);
  if (!vids.length && !songs.length) return note('Unsupported file type: ' + (files[0] ? files[0].name : ''), true);
  if (target !== 'song' && vids.length) enqueue(() => doVideo(vids[0]));
  if (target !== 'video' && songs.length) enqueue(() => doSongs(songs));
}

// ---------- drag feedback ----------
const ZONES = [['#stage', 'video', 'Drop video here', 'Not a video'], ['#tl-wrap', 'song', 'Drop songs here', 'Not an audio file'],
               ['#results-panel', 'song', 'Drop songs here', 'Not an audio file']];
ZONES.forEach(([sel, , lab, bad]) => { const el = document.querySelector(sel); el.dataset.dz = lab; el.dataset.bad = bad; });
let depth = 0;
const hasFiles = e => e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
function mark(e) {
  const items = e && e.dataTransfer && e.dataTransfer.items ? [...e.dataTransfer.items].filter(i => i.kind === 'file') : [];
  const types = items.map(i => i.type).filter(Boolean);
  ZONES.forEach(([sel, want]) => {
    const el = document.querySelector(sel); el.classList.add('dz');
    const hover = e && e.target && e.target.closest && e.target.closest(sel);
    const wrong = hover && types.length === items.length && items.length && !types.some(t => t.startsWith(want === 'video' ? 'video/' : 'audio/'));
    el.classList.toggle('bad', !!wrong);
  });
}
function endDrag() { depth = 0; document.body.classList.remove('dragging'); document.querySelectorAll('.dz').forEach(el => el.classList.remove('dz', 'bad')); }
window.addEventListener('dragenter', e => { if (!hasFiles(e)) return; e.preventDefault(); depth++; document.body.classList.add('dragging'); mark(e); });
window.addEventListener('dragover', e => { e.preventDefault(); if (hasFiles(e)) { document.body.classList.add('dragging'); mark(e); e.dataTransfer.dropEffect = 'copy'; } });
window.addEventListener('dragleave', e => { if (!hasFiles(e)) return; if (--depth <= 0) endDrag(); });
window.addEventListener('dragend', endDrag);
window.addEventListener('drop', async e => {
  e.preventDefault(); endDrag();
  const t = e.target && e.target.closest ? e.target : document.body;
  const target = t.closest('#stage') ? 'video' : t.closest('#tl-wrap,#results-panel') ? 'song' : null;
  route(await filesFrom(e.dataTransfer), target);
});

// ---------- browse ----------
$('b-video').onclick = () => $('f-video').click();
$('b-songs').onclick = () => $('f-songs').click();
$('f-video').onchange = e => { const f = [...e.target.files]; e.target.value = ''; if (f.length) route(f, 'video'); };
$('f-songs').onchange = e => { const f = [...e.target.files]; e.target.value = ''; if (f.length) route(f, 'song'); };

// ---------- uploads control ----------
let upInfo = {count: 0, bytes: 0};
async function refreshUploads() {
  try { upInfo = await (await fetch('/api/uploads')).json(); } catch (e) { return; }
  $('up-size').textContent = fmtB(upInfo.bytes); $('up-clear').disabled = !upInfo.count;
}
$('up-clear').onclick = () => {
  $('up-pop-t').textContent = `Delete ${upInfo.count} uploaded file${upInfo.count === 1 ? '' : 's'} (${fmtB(upInfo.bytes)})? Dropped videos and songs will be removed from this page.`;
  $('up-pop').classList.remove('hidden');
};
$('up-no').onclick = () => $('up-pop').classList.add('hidden');
$('up-yes').onclick = async () => {
  $('up-pop').classList.add('hidden');
  try {
    const r = await fetch('/api/uploads', {method: 'DELETE'}); const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
    const gone = p => (p || '').includes('/.uploads/');
    if (S.sel && gone(S.sel.path)) { pause(); S.sel = null; S.score = null; aud.removeAttribute('src'); }
    S.results = S.results.filter(r => !r.dropped && !gone(r.path));
    if (gone(S.videoPath) || gone(D.videoPath)) {
      pause(); vid.removeAttribute('src'); vid.load(); S.video = null; S.videoPath = ''; S.dur = 0; $('empty').classList.remove('hidden');
    }
    D.videoPath = ''; D.queue = []; D.dropped.clear();
    renderResults({too_short: (S.short || []).filter(r => !gone(r.path)), skipped: S.skipped || []});
    note(`Freed ${fmtB(j.freed || 0)}`);
    if (typeof draw === 'function') draw();
  } catch (e) { note(e.message, true); }
  refreshUploads();
};
$('scan').addEventListener('click', () => { D.videoPath = ''; D.queue = []; D.dropped.clear(); });
refreshUploads();
