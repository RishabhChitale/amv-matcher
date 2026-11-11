'use strict';
// F4: "Auto-sync all" for the scan list. Talks to the proposed batch API (docs/API.md "Auto-sync all"); with ?mockbatch=1 (or
// window.ASA_MOCK = true) a timer-driven mock answers instead, so the UI works before the server endpoint exists.
// Per song: {status: queued|running|done|error|cancelled, progress 0..1, natural_pct, auto_pct}. Finished songs also write
// x.r.auto_sync_cached = {total_pct}, so the existing "After auto-sync" sort and chip keep working.
(function () {
  const $$ = id => document.getElementById(id);
  const mock = () => window.ASA_MOCK || /[?&]mockbatch=1/.test(location.search);
  const A = {id: '', status: 'idle', total: 0, done: 0, songs: new Map(), timer: 0, mockT: 0};
  const rows = () => (window.MATCH ? [...window.MATCH.M.rows.values()].filter(x => x.state === 'done') : []);
  const redraw = () => { paint(); if (window.MATCH && window.MATCH.render) window.MATCH.render(); };
  const busy = () => A.status === 'running';

  // ---- transport (real + mock) ----
  async function apiStart(video, paths) {
    if (mock()) return mockStart(paths);
    const r = await POST('/api/auto-sync-batch', {video_path: video, song_paths: paths}), j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.statusText); return j;
  }
  async function apiGet() { if (mock()) return mockGet(); const r = await fetch('/api/auto-sync-batch/' + A.id); return r.json(); }
  async function apiCancel() { if (mock()) return mockCancel(); const r = await POST('/api/auto-sync-batch/' + A.id + '/cancel', {}); return r.json(); }
  const MK = {songs: [], t0: 0, cancelled: false};
  function mockStart(paths) {
    const by = new Map(rows().map(x => [x.path, x]));
    MK.songs = paths.map((p, i) => { const n = by.get(p).r.total_pct; return {path: p, natural_pct: n, auto_pct: Math.min(99, n + 4 + (i * 7) % 15), dur: 1800 + (i % 4) * 700}; });
    MK.t0 = Date.now(); MK.cancelled = false; return {batch_id: 'mock', total: paths.length, status: 'running'};
  }
  function mockView() {
    const el = Date.now() - MK.t0; let t = 0;   // songs run two at a time
    const songs = MK.songs.map((s, i) => {
      const lane = i % 2, start = MK.songs.filter((q, k) => k < i && k % 2 === lane).reduce((a, q) => a + q.dur, 0), pr = Math.max(0, Math.min(1, (el - start) / s.dur));
      let st = pr >= 1 ? 'done' : pr > 0 ? 'running' : 'queued';
      if (MK.cancelled && st !== 'done') st = 'cancelled';
      return {path: s.path, status: st, progress: MK.cancelled && st === 'cancelled' ? 0 : pr, natural_pct: s.natural_pct, auto_pct: st === 'done' ? s.auto_pct : null};
    });
    const done = songs.filter(s => s.status === 'done').length;
    return {batch_id: 'mock', status: MK.cancelled ? 'cancelled' : done === songs.length ? 'done' : 'running', total: songs.length, done, songs};
  }
  const mockGet = () => mockView();
  function mockCancel() { MK.cancelled = true; return mockView(); }

  // ---- state ----
  function apply(j) {
    A.status = j.status; A.total = j.total; A.done = j.done != null ? j.done : j.songs.filter(s => s.status === 'done').length;
    j.songs.forEach(s => {
      A.songs.set(s.path, s);
      const x = window.MATCH && window.MATCH.M.rows.get(s.path);
      if (x && x.r && s.status === 'done' && s.auto_pct != null) x.r.auto_sync_cached = Object.assign({}, x.r.auto_sync_cached, {total_pct: s.auto_pct});
    });
  }
  async function poll() {
    clearTimeout(A.timer);
    try { apply(await apiGet()); } catch (e) { A.status = 'error'; }
    redraw(); if (busy()) A.timer = setTimeout(poll, mock() ? 250 : 800);
  }
  async function start() {
    if (busy() || !window.MATCH) return;
    const list = rows(); if (!list.length) return;
    A.songs.clear(); A.status = 'running'; A.total = list.length; A.done = 0;
    list.forEach(x => A.songs.set(x.path, {path: x.path, status: 'queued', progress: 0, natural_pct: x.r.total_pct, auto_pct: null}));
    redraw();
    try { const j = await apiStart(window.MATCH.M.videoPath, list.map(x => x.path)); A.id = j.batch_id; poll(); }
    catch (e) { A.status = 'error'; A.songs.clear(); if (window.note) note(e.message, true); redraw(); }
  }
  async function cancel() { if (!busy()) return; try { apply(await apiCancel()); } catch (e) {} clearTimeout(A.timer); redraw(); }

  // ---- row hooks (used by match.js rowHTML / sig) ----
  const sig = x => { const s = A.songs.get(x.path); return s ? s.status + ':' + Math.round(s.progress * 20) + ':' + s.auto_pct : ''; };
  function rowHTML(x) {
    const s = A.songs.get(x.path); if (!s || s.status === 'cancelled') return '';
    const nat = Math.round(s.natural_pct), pct = Math.round(s.progress * 100);
    if (s.status === 'done') return `<span class="asa-row done" title="Natural fit, then fit after auto-sync"><span class="asa-nat">${nat}%</span><span class="asa-arrow">→</span><b class="asa-auto">${Math.round(s.auto_pct)}%</b></span>`;
    if (s.status === 'error') return `<span class="asa-row err">auto-sync failed</span>`;
    return `<span class="asa-row ${s.status}"><span class="asa-nat">${nat}%</span><span class="asa-arrow">→</span><span class="asa-bar" role="progressbar" aria-valuenow="${pct}"><i style="width:${pct}%"></i></span><span class="asa-pct">${s.status === 'queued' ? 'queued' : pct + '%'}</span></span>`;
  }

  // ---- toolbar (button + overall progress), injected into #mr-top ----
  function paint() {
    const go = $$('mr-asall'), pb = $$('mr-asp'); if (!go) return;
    const n = rows().length, run = busy();
    go.textContent = run ? 'Cancel auto-sync all' : 'Auto-sync all'; go.classList.toggle('asa-cancel', run); go.disabled = !run && !n;
    pb.classList.toggle('hidden', A.status === 'idle');
    const pct = A.total ? Math.round(A.done / A.total * 100) : 0;
    pb.querySelector('i').style.width = pct + '%';
    pb.querySelector('span').textContent = A.status === 'cancelled' ? `Cancelled · ${A.done}/${A.total} done` : A.status === 'error' ? 'Auto-sync all failed' : `Auto-sync all · ${A.done}/${A.total} done`;
  }
  function init() {
    const top = document.querySelector('#mr .mr-top'); if (!top || $$('mr-asall')) return;
    const b = document.createElement('button'); b.id = 'mr-asall'; b.className = 'mini'; b.type = 'button'; b.title = 'Auto-sync every analyzed song, one after another';
    top.insertBefore(b, $$('mr-new')); b.onclick = () => (busy() ? cancel() : start());
    const p = document.createElement('div'); p.id = 'mr-asp'; p.className = 'asa-prog hidden'; p.innerHTML = '<span></span><div class="asa-trk"><i></i></div>';
    top.parentNode.insertBefore(p, $$('mr-list')); paint();
  }
  window.ASA = {start, cancel, rowHTML, sig, state: A, init, paint};
  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', init);
})();
