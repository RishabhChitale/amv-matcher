'use strict';
// Media panel (Resolve-like pool). Depends on drop.js/native.js globals (D, POST, note, fmtB, enqueue, doVideo, doSongs, item) and app.js ($, esc).
const M = {roots: [], cur: '', parent: null, folders: [], files: [], view: 'list', q: '', sort: 'name', states: {}, since: 0, mock: false, sel: null};
const MV = new Set(['video']);
const fmtDur = s => !s && s !== 0 ? '' : (s >= 3600 ? Math.floor(s / 3600) + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') : Math.floor(s / 60)) + ':' + String(Math.floor(s % 60)).padStart(2, '0');
const domainOf = u => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return ''; } };
const kindFrom = f => f.kind || (VEXT.includes(extOf(f.name)) ? 'video' : 'audio');

async function mapi(url, opts) {   // real endpoint; on 404 fall back to the mock (backend not landed yet)
  if (!M.mock) {
    let r; try { r = await fetch(url, opts); } catch (e) { r = null; }
    if (r && r.status !== 404) { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.statusText); return j; }
    if (!window.MOCK_MEDIA) throw new Error('media endpoints unavailable');
    M.mock = true; $('media').classList.add('mock'); $('m-mock').classList.remove('hidden');
  }
  const u = new URL(url, location.origin), p = u.searchParams.get('path');
  if (u.pathname === '/api/media/roots') { if ((opts || {}).method === 'POST') { M.roots.push({path: JSON.parse(opts.body).path, name: 'Added'}); } return MOCK_MEDIA.roots(); }
  if (u.pathname === '/api/fs/list') return MOCK_MEDIA.list(p);
  if (u.pathname === '/api/media/link') return MOCK_MEDIA.link(JSON.parse(opts.body).path);
  return {};
}
const thumbUrl = (f, t) => M.mock ? MOCK_MEDIA.thumb(f.path, t) : `/api/thumb?path=${encodeURIComponent(f.path)}${t != null ? '&t=' + t.toFixed(2) : ''}`;

function rootsOf(j) { return (j.roots || j || []).map(r => typeof r === 'string' ? {path: r, name: baseOf(r)} : {path: r.path, name: r.name || baseOf(r.path)}); }
async function loadRoots() {
  try { M.roots = rootsOf(await mapi('/api/media/roots')); } catch (e) { $('m-list').innerHTML = `<div class="muted pad">${esc(e.message)}</div>`; return; }
  renderRoots(); if (!M.cur && M.roots[0]) openDir(M.roots[0].path);
}
async function openDir(p) {
  M.cur = p; renderRoots();
  try {
    const j = await mapi('/api/fs/list?path=' + encodeURIComponent(p));
    M.parent = j.parent || null; M.folders = j.folders || []; M.files = (j.files || []).map(f => ({...f, kind: kindFrom(f)}));
  } catch (e) { M.folders = []; M.files = []; note(e.message, true); }
  renderFiles();
}
function renderRoots() {
  $('m-roots').innerHTML = M.roots.map(r => `<button class="m-root${r.path === M.cur ? ' on' : ''}" data-p="${esc(r.path)}" title="${esc(r.path)}">${esc(r.name)}</button>`).join('');
}
function sourceBadge(f) {
  const s = f.source; if (!s || !s.source_url) return '';
  const d = domainOf(s.source_url);
  return `<span class="m-src" data-url="${esc(s.source_url)}" title="${esc(s.source_url)}${s.downloaded_by ? '\nvia ' + esc(s.downloaded_by) : ''}"><svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12"/></svg>${esc(d)}<a class="m-copy" title="Copy source URL">Copy source URL</a></span>`;
}
function filtered() {
  const q = M.q.trim().toLowerCase(), by = {name: (a, b) => a.name.localeCompare(b.name, undefined, {numeric: true}), date: (a, b) => b.mtime - a.mtime, duration: (a, b) => (b.duration || 0) - (a.duration || 0)}[M.sort];
  return M.files.filter(f => !q || f.name.toLowerCase().includes(q)).sort(by);
}
function renderFiles() {
  const box = $('m-list'); box.className = 'm-list ' + M.view;
  const rows = [];
  if (M.parent && !M.q) rows.push(`<div class="m-folder" data-p="${esc(M.parent)}">&#x2190; ..</div>`);
  if (!M.q) M.folders.forEach(d => rows.push(`<div class="m-folder" data-p="${esc(d.path)}">&#x1F4C1;&#xFE0E; ${esc(d.name)}</div>`));
  filtered().forEach(f => {
    const st = M.states[f.path] || 'none';
    rows.push(`<div class="m-item ${f.kind}" draggable="true" data-p="${esc(f.path)}" data-k="${f.kind}" data-n="${esc(f.name)}" data-dur="${f.duration || 0}" data-st="${st}">
      <div class="m-th">${f.kind === 'video' ? `<img loading="lazy" alt="" src="${thumbUrl(f, null)}">` : '<span class="m-note">&#x266B;</span>'}<i class="m-dot" title="${st}"></i><em class="m-dur">${fmtDur(f.duration)}</em></div>
      <div class="m-meta"><b title="${esc(f.name)}">${esc(f.name)}</b><span>${fmtB(f.size || 0)}${f.mtime ? ' &middot; ' + new Date(f.mtime * 1000).toLocaleDateString() : ''}</span>${sourceBadge(f)}</div></div>`);
  });
  box.innerHTML = rows.join('') || '<div class="muted pad">No media here.</div>';
}
function setState(path, st) { M.states[path] = st; const el = [...document.querySelectorAll('.m-item')].find(e => e.dataset.p === path); if (el) { el.dataset.st = st; el.querySelector('.m-dot').title = st; } }
async function pollJobs() {
  if (M.mock) return;
  try {
    const j = await (await fetch('/api/jobs?since=' + M.since)).json();
    (j.events || []).forEach(ev => {
      M.since = Math.max(M.since, ev.seq || ev.id || 0);
      const p = ev.path || (ev.job && ev.job.path); const s = ev.state || ev.status || (ev.job && ev.job.state);
      if (!p || !s) return;
      setState(p, s === 'done' || s === 'ready' ? 'ready' : s === 'queued' ? 'queued' : s === 'running' || s === 'analyzing' ? 'analyzing' : s === 'error' || s === 'failed' ? 'error' : M.states[p] || 'none');
    });
  } catch (e) {}
}
async function link(el) {
  const path = el.dataset.p, kind = el.dataset.k;
  const j = await mapi('/api/media/link', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path})});
  if (M.mock) { note('(mock) would link ' + el.dataset.n); return; }
  const it = item(j.path || path);
  if (kind === 'video') enqueue(() => doVideo(it)); else enqueue(() => doSongs([it]));
}
// ---------- events ----------
const mdoc = document;
$('m-roots').onclick = e => { const b = e.target.closest('.m-root'); if (b) openDir(b.dataset.p); };
$('m-list').onclick = e => {
  if (e.target.closest('.m-copy')) { const u = e.target.closest('.m-src').dataset.url; (navigator.clipboard ? navigator.clipboard.writeText(u) : Promise.reject()).then(() => note('Source URL copied'), () => note(u)); return; }
  const f = e.target.closest('.m-folder'); if (f) return openDir(f.dataset.p);
  const it = e.target.closest('.m-item'); if (it) { M.sel = it.dataset.p; mdoc.querySelectorAll('.m-item.sel').forEach(x => x.classList.remove('sel')); it.classList.add('sel'); }
};
$('m-list').ondblclick = e => { const it = e.target.closest('.m-item'); if (it) link(it).catch(err => note(err.message, true)); };
$('m-list').addEventListener('dragstart', e => { const it = e.target.closest('.m-item'); if (!it) return; e.dataTransfer.setData('application/x-amv-media', JSON.stringify({path: it.dataset.p, kind: it.dataset.k, name: it.dataset.n})); e.dataTransfer.effectAllowed = 'copy'; });
$('m-list').addEventListener('mousemove', e => {   // hover-scrub: x position picks the frame
  const th = e.target.closest('.m-th'); if (!th) return; const it = th.parentElement, img = th.querySelector('img'); if (!img || it.dataset.k !== 'video') return;
  const r = th.getBoundingClientRect(), dur = +it.dataset.dur || 0, step = Math.min(11, Math.max(0, Math.floor((e.clientX - r.left) / r.width * 12)));
  if (it.dataset.step === String(step)) return; it.dataset.step = step;
  const f = {path: it.dataset.p}; img.src = thumbUrl(f, dur ? (step + .5) / 12 * dur : null);
});
$('m-list').addEventListener('mouseout', e => { const th = e.target.closest('.m-th'); if (th && !th.contains(e.relatedTarget)) { const it = th.parentElement; delete it.dataset.step; const img = th.querySelector('img'); if (img) img.src = thumbUrl({path: it.dataset.p}, null); } });
// drop a pool item onto the player / A1 / results (same targets as native OS drops)
['#stage', '#tl-wrap', '#results-panel'].forEach(sel => {
  const el = document.querySelector(sel);
  el.addEventListener('dragover', e => { if ([...(e.dataTransfer.types || [])].includes('application/x-amv-media')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; el.classList.add('m-drop'); } });
  el.addEventListener('dragleave', () => el.classList.remove('m-drop'));
  el.addEventListener('drop', e => {
    const raw = e.dataTransfer && e.dataTransfer.getData('application/x-amv-media'); el.classList.remove('m-drop'); if (!raw) return;
    e.preventDefault(); const d = JSON.parse(raw);
    const want = sel === '#stage' ? 'video' : 'audio';
    if (d.kind !== want) return note(`That is ${d.kind === 'video' ? 'a video' : 'an audio file'}; drop it on the ${d.kind === 'video' ? 'player' : 'A1 or results'}`, true);
    link({dataset: {p: d.path, k: d.kind, n: d.name}}).catch(err => note(err.message, true));
  });
});
$('m-view').onclick = () => { M.view = M.view === 'list' ? 'grid' : 'list'; $('m-view').textContent = M.view === 'list' ? 'Grid' : 'List'; renderFiles(); };
$('m-q').oninput = e => { M.q = e.target.value; renderFiles(); };
$('m-sort').onchange = e => { M.sort = e.target.value; renderFiles(); };
$('m-add').onclick = async () => {
  let p = null;
  if (window.pywebview && pywebview.api && pywebview.api.pick_folder) p = await pywebview.api.pick_folder(); else p = prompt('Folder path to add:');
  if (!p) return;
  try { await mapi('/api/media/roots', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: p})}); await loadRoots(); openDir(p); } catch (e) { note(e.message, true); }
};
loadRoots(); setInterval(pollJobs, 2000);
