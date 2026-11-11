'use strict';
// F4: "Auto-sync all" for the scan list: a client-side queue (concurrency 1, list order) over the existing per-song /api/auto-sync job.
// Per song: {status: queued|running|done|error|cancelled, progress 0..1, natural_pct, auto_pct}. Finished songs also write
// x.r.auto_sync_cached = {total_pct}, so the existing "After auto-sync" sort and chip keep working.
(function () {
  const $$ = id => document.getElementById(id);
    const A = {id: '', status: 'idle', total: 0, done: 0, songs: new Map(), cur: '', cancelled: false};
  const rows = () => (window.MATCH ? [...window.MATCH.M.rows.values()].filter(x => x.state === 'done') : []);
  const redraw = () => { paint(); if (window.MATCH && window.MATCH.render) window.MATCH.render(); };
  const busy = () => A.status === 'running';

  // ---- real client-side queue over the existing per-song job (/api/auto-sync), concurrency 1, list order ----
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const finalPct = j => { const f = j && j.result && j.result.final; return f && f.total_pct != null ? f.total_pct : null; };
  async function runSong(x, s) {
    const body = {video_path: window.MATCH.M.videoPath, song_path: x.path};
    const r = await POST('/api/auto-sync', body), j0 = await r.json();
    const id = j0.job_id; if (!id) throw new Error(j0.detail || 'Auto-sync failed to start');
    A.cur = id; s.status = 'running'; redraw();
    try {
      for (;;) {
        const j = await (await fetch('/api/auto-sync/' + id)).json();
        if (A.cancelled || j.status === 'cancelled') { s.status = 'cancelled'; s.progress = 0; return; }
        if (j.status === 'error') throw new Error(j.error || 'Auto-sync failed');
        if (j.status !== 'running') {
          const p = finalPct(j); if (p == null) throw new Error('no result');
          s.status = 'done'; s.progress = 1; s.auto_pct = p;
          if (x.r) x.r.auto_sync_cached = Object.assign({}, x.r.auto_sync_cached, j.result.final);
          return;
        }
        s.progress = j.total ? Math.min(0.99, (j.done || 0) / j.total) : s.progress; redraw();
        await sleep(600);
      }
    } finally { A.cur = ''; }
  }
  async function start() {
    if (busy() || !window.MATCH) return;
    const list = rows(); if (!list.length) return;
    A.songs.clear(); A.status = 'running'; A.total = list.length; A.done = 0; A.cancelled = false;
    list.forEach(x => A.songs.set(x.path, {path: x.path, status: 'queued', progress: 0, natural_pct: x.r.total_pct, auto_pct: null}));
    redraw();
    for (const x of list) {
      const s = A.songs.get(x.path);
      if (A.cancelled) { s.status = 'cancelled'; continue; }
      try { await runSong(x, s); } catch (e) { s.status = 'error'; s.err = e.message; if (window.note) note((e.message || 'Auto-sync failed'), true); }
      A.done = [...A.songs.values()].filter(q => q.status === 'done').length; redraw();
    }
    A.status = A.cancelled ? 'cancelled' : 'done'; redraw();
  }
  async function cancel() {
    if (!busy()) return; A.cancelled = true;
    if (A.cur) { try { await POST('/api/auto-sync/' + A.cur + '/cancel', {}); } catch (e) {} }
    A.songs.forEach(s => { if (s.status === 'queued') s.status = 'cancelled'; }); redraw();
  }

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
