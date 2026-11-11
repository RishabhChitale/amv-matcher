'use strict';
// New-match popup (video + music folder) and live Match-results popup. Depends on app.js/drop.js/native.js globals.
(function () {
  const $$ = id => document.getElementById(id);
  const NM = {video: '', dir: '', busy: false};
  const M = {token: 0, rows: new Map(), order: [], videoName: '', videoPath: '', total: 0, t0: 0, doneN: 0, sort: 'now', q: '', kb: -1, prev: null, prevPath: '', hasMatch: false};
  const dirOf = p => String(p).replace(/[\\/][^\\/]*$/, '');
  const api = () => (window.pywebview && window.pywebview.api) || null;

  // ---------- new match ----------
  function openNew() { closeRes(true); $$('nm').classList.remove('hidden'); $$('nm-close').classList.toggle('hidden', !M.hasMatch && !S.video); nmUI(); }
  function closeNew() { $$('nm').classList.add('hidden'); }
  function nmUI() {
    $$('nm-v').value = NM.video; $$('nm-d').value = NM.dir;
    $$('nm-dv').classList.toggle('filled', !!NM.video); $$('nm-dm').classList.toggle('filled', !!NM.dir);
    $$('nm-vn').textContent = NM.video ? baseOf(NM.video) : 'Drop a video here'; $$('nm-dn').textContent = NM.dir ? baseOf(NM.dir) : 'Drop a music folder here';
    $$('nm-go').disabled = !(NM.video && NM.dir) || NM.busy;
  }
  async function register(paths) {
    const r = await POST('/api/register-paths', {paths}); const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.statusText); return j;
  }
  async function setFromPaths(kind, paths) {
    try {
      if (kind === 'video') {
        const j = await register(paths); if (!j.videos.length) return note('That is not a video file', true);
        NM.video = j.videos[0];
      } else {
        const p = paths[0], j = await register([p]);
        if (!j.songs.length) return note('No songs found there', true);
        NM.dir = kindOf(p) ? dirOf(p) : p;
      }
    } catch (e) { return note(e.message, true); }
    nmUI();
  }
  $$('nm-v').onchange = () => { NM.video = $$('nm-v').value.trim(); nmUI(); };
  $$('nm-d').onchange = () => { NM.dir = $$('nm-d').value.trim(); nmUI(); };
  $$('nm-bv').onclick = async () => { const a = api(); if (!a || !a.pick_video) return $$('nm-v').focus(); const p = await a.pick_video(); if (p && p.length) setFromPaths('video', [].concat(p)); };
  $$('nm-bd').onclick = async () => { const a = api(); if (!a || !a.pick_folder) return $$('nm-d').focus(); const p = await a.pick_folder(); if (p) setFromPaths('dir', [].concat(p)); };
  $$('nm-close').onclick = closeNew;
  $$('b-new').onclick = openNew;
  let dropKind = null;
  window.addEventListener('drop', e => {
    const t = e.target && e.target.closest ? e.target : document.body;
    dropKind = t.closest('#nm-dv') ? 'video' : t.closest('#nm-dm') ? 'dir' : null;
    if (dropKind) e.preventDefault();
  }, true);
  // A native drop while the New-match popup is open but not on one of its zones (the shell only reports paths): fill the zones from what
  // was dropped and, when both a video and music are there, start the match exactly like "Find best songs" (closes the popup, shows the scan list).
  async function dropIntoNew(paths) {
    if (!paths || !paths.length || NM.busy) return;
    try {
      const j = await register(paths);
      if (!j.videos.length && !j.songs.length) return note('Nothing usable in that drop (video: mp4 mov mkv webm avi m4v; audio: mp3 wav m4a flac ogg aac)', true);
      if (j.videos.length) NM.video = j.videos[0];
      if (j.songs.length) NM.dir = paths.find(p => !kindOf(p)) || dirOf(j.songs[0]);
    } catch (e) { return note(e.message, true); }
    nmUI(); if (NM.video && NM.dir) return startMatch();
  }
  // native.js normally runs first; resolve the previous nativeDrop at call time and re-hook on load, so script order can never break drops
  function hookDrop() {
    const prev = window.nativeDrop;
    if (prev && prev.__nm) return;
    const w = paths => { const k = dropKind; dropKind = null; const o = prev || window.nativeDropBase; return k ? setFromPaths(k, paths) : !$$('nm').classList.contains('hidden') ? dropIntoNew(paths) : (typeof o === 'function' ? o(paths) : undefined); };
    w.__nm = true; window.nativeDropBase = prev || window.nativeDropBase; window.nativeDrop = w;
  }
  hookDrop(); window.addEventListener('load', hookDrop);
  ['nm-dv', 'nm-dm'].forEach(id => { const z = $$(id); z.addEventListener('dragover', e => { e.preventDefault(); z.classList.add('over'); }); z.addEventListener('dragleave', () => z.classList.remove('over')); z.addEventListener('drop', () => z.classList.remove('over')); });

  $$('nm-go').onclick = startMatch;
  async function startMatch() {
    if (!NM.video || !NM.dir || NM.busy) return;
    NM.busy = true; nmUI(); const my = ++M.token; stopPrev();
    try {
      const j = await register([NM.dir]), sub = $$('nm-sub').checked, base = NM.dir.replace(/[\\/]+$/, '');
      const songs = j.songs.filter(p => sub || dirOf(p) === base);
      if (!songs.length) throw new Error('No songs in that folder' + (sub ? '' : ' (try including subfolders)'));
      await register([NM.video]);
      S.results = []; S.short = []; S.skipped = []; S.sel = null; S.inUse = null; S.wf = null; D.queue = []; D.names = {};
      renderResults({too_short: [], skipped: []});
      await doVideo({path: NM.video, name: baseOf(NM.video)});
      if (my !== M.token) return;
      M.rows = new Map(); songs.forEach(p => M.rows.set(p, {path: p, name: baseOf(p), state: 'pending'}));
      M.videoName = baseOf(NM.video); M.videoPath = S.videoPath || NM.video; M.total = songs.length; M.doneN = 0; M.t0 = Date.now(); M.q = ''; M.kb = -1; M.hasMatch = true;
      $$('mr-q').value = ''; $$('b-mres').classList.remove('hidden');
      closeNew(); openRes(); renderRes();
      runQueue(my, songs.slice());
    } catch (e) { note(e.message, true); }
    NM.busy = false; nmUI();
  }
  async function runQueue(my, list) {
    const work = async () => {
      while (list.length && my === M.token) {
        const p = list.shift(), row = M.rows.get(p);
        try {
          const j = await scoreSongs(M.videoPath, [p]);
          if (my !== M.token) return;
          (j.results || []).forEach(r => { if (r.source === 'original') { if (!M.rows.has(r.path)) M.rows.set(r.path, {path: r.path, name: r.name, state: 'done', r}); } else Object.assign(row, {state: 'done', r}); });
          (j.too_short || []).forEach(r => { if (r.source !== 'original') Object.assign(row, {state: 'short', r}); });
          (j.skipped || []).forEach(s => { if (s.source !== 'original') Object.assign(row, {state: 'skipped', reason: s.reason || ''}); });
          if (row.state === 'pending') Object.assign(row, {state: 'skipped', reason: 'No result'});
        } catch (e) { if (my !== M.token) return; Object.assign(row, {state: 'skipped', reason: e.message}); }
        M.doneN++; syncSide(); renderRes();
      }
    };
    await Promise.all([work(), work()]);
    if (my === M.token) { renderRes(); }
  }
  function syncSide() {   // keep the sidebar list in step with the popup
    const rows = [...M.rows.values()];
    S.results = rows.filter(x => x.state === 'done').map(x => x.r).sort(byScore);
    const sh = rows.filter(x => x.state === 'short').map(x => x.r), sk = rows.filter(x => x.state === 'skipped').map(x => ({name: x.name, path: x.path, reason: x.reason}));
    renderResults({too_short: sh, skipped: sk});
  }

  // ---------- results popup ----------
  const after = r => (r && r.auto_sync_cached && r.auto_sync_cached.total_pct != null) ? r.auto_sync_cached.total_pct : null;
  const key = x => M.sort === 'after' ? (after(x.r) != null ? after(x.r) : -1) + (x.r.total_pct / 1000) : x.r.total_pct + x.r.sync_pct / 1000;
  function openRes() { if (!M.hasMatch) return; closeNew(); $$('mr').classList.remove('hidden'); renderRes(); $$('mr-list').focus(); }
  function closeRes(quiet) { stopPrev(); $$('mr').classList.add('hidden'); }
  $$('mr-close').onclick = () => closeRes();
  $$('b-mres').onclick = openRes;
  $$('mr-new').onclick = openNew;
  $$('mr').addEventListener('mousedown', e => { if (e.target === $$('mr')) closeRes(); });
  $$('mr-q').oninput = () => { M.q = $$('mr-q').value.trim().toLowerCase(); renderRes(); };
  $$('mr-sort').onchange = () => { M.sort = $$('mr-sort').value; renderRes(); };
  const fmtETA = s => s < 90 ? Math.max(1, Math.round(s)) + ' s' : Math.round(s / 60) + ' min';
  function visible() {
    const all = [...M.rows.values()].filter(x => !M.q || x.name.toLowerCase().includes(M.q));
    const done = all.filter(x => x.state === 'done').sort((a, b) => key(b) - key(a));
    const pend = all.filter(x => x.state === 'pending'), sh = all.filter(x => x.state === 'short').sort((a, b) => b.r.song_duration - a.r.song_duration), sk = all.filter(x => x.state === 'skipped');
    return {done, pend, sh, sk, list: done.concat(pend, sh, sk)};
  }
  const chip = r => `tempo ${Math.round(r.tempo_pct)} · sync ${Math.round(r.sync_pct)} · energy ${Math.round(r.arc_pct)}`;
  function rowHTML(x, rank) {
    const r = x.r;
    if (x.state === 'pending') return `<div class="mr-sp"></div><div class="mr-n"><b title="${esc(x.name)}">${esc(x.name)}</b><span class="muted">analyzing…</span></div>`;
    if (x.state === 'skipped') return `<div class="mr-rk">–</div><div class="mr-n"><b title="${esc(x.name)}">${esc(x.name)}</b><span class="muted">skipped: ${esc(x.reason)}</span></div>`;
    const p = x.state === 'short' ? 0 : r.total_pct, af = after(r);
    const rg = x.state === 'short' ? '<div class="mr-rg dim">–</div>' : `<div class="mr-rg">${ring(p)}</div>`;
    const sub = x.state === 'short' ? `<span class="muted">Too short: ${fmtMS(r.song_duration)} vs video ${fmtMS(r.video_duration)}</span>` : `<span class="mr-chip">${chip(r)}</span>${af != null ? `<span class="mr-chip aft">after auto-sync ${Math.round(af)}%</span>` : ''}${window.ASA ? ASA.rowHTML(x) : ''}`;
    const act = x.state === 'short' ? '' : `<button class="mr-use" title="Use this audio (Enter)">Use this audio</button><button class="mr-as" title="Auto-sync this song">${x.syncing ? 'Syncing…' : 'Auto-sync'}</button>`;
    return `<div class="mr-rk">${x.state === 'short' ? '' : rank}</div>${rg}<div class="mr-n"><b title="${esc(x.name)}">${esc(x.name)}</b>${sub}</div><div class="mr-bpm">${r.bpm ? Math.round(r.bpm) + ' BPM' : ''}</div><div class="mr-dur">${fmtMS(r.song_duration)}</div><div class="mr-act">${act}</div>${x.state === 'done' && window.PVS ? PVS.html(x) : ''}`;
  }
  const els = new Map();
  let rrQ = 0;   // U3b: coalesce bursts of per-song updates into one render per frame
  function renderRes() { if (!rrQ) rrQ = requestAnimationFrame(() => { rrQ = 0; renderResNow(); }); }
  function renderResNow() {
    if ($$('mr').classList.contains('hidden')) return;
    const v = visible(), box = $$('mr-list'), n = M.rows.size;
    if (window.ASA) ASA.paint();   // F4: enable the Auto-sync all button as rows finish
    const rem = M.total - M.doneN, el = (Date.now() - M.t0) / 1000, eta = M.doneN ? fmtETA(el / M.doneN * rem / 2) : '…';
    $$('mr-head').textContent = `${M.videoName} · ${n} songs · ${M.doneN} analyzed${rem > 0 ? ' · ETA ' + eta : ''}`;
    const old = new Map(); if (!reduce && els.size <= 80) els.forEach((e, p) => old.set(p, e.getBoundingClientRect().top));
    let rank = 0, prevGroup = '';
    const want = [];
    v.list.forEach(x => {
      const g = x.state === 'short' ? 'short' : x.state === 'skipped' ? 'skip' : 'main';
      if (g !== 'main' && g !== prevGroup) want.push(['h' + g, g === 'short' ? 'Too short for this video' : 'Skipped']);
      prevGroup = g; if (x.state === 'done') rank++;
      want.push([x, rank]);
    });
    const seen = new Set(), frag = [];
    want.forEach(w => {
      if (typeof w[0] === 'string') { let h = els.get(w[0]); if (!h) { h = document.createElement('div'); h.className = 'mr-grp'; els.set(w[0], h); } h.textContent = w[1]; seen.add(w[0]); frag.push(h); return; }
      const x = w[0]; let e = els.get(x.path), sig = x.state + '|' + w[1] + '|' + (x.r ? x.r.total_pct : '') + '|' + after(x.r) + '|' + !!x.syncing + '|' + (window.ASA ? ASA.sig(x) : '');
      if (!e) { e = document.createElement('div'); els.set(x.path, e); e.onclick = () => { M.kb = visibleIdx(x.path); markKb(); }; e.ondblclick = () => useRow(x); }
      if (e.dataset.sig !== sig) {
        e.dataset.sig = sig; e.className = 'mr-row ' + x.state; e.innerHTML = rowHTML(x, w[1]); e.dataset.path = x.path;
        const b = s => e.querySelector(s);
        if (b('.mr-use')) { b('.mr-use').onclick = ev => { ev.stopPropagation(); useRow(x); }; b('.mr-as').onclick = ev => { ev.stopPropagation(); autoSyncRow(x); }; }
        if (window.PVS && x.state === 'done') PVS.bind(e, x);
        requestAnimationFrame(() => e.querySelectorAll('.fg').forEach(c => { c.style.strokeDashoffset = C * (1 - c.dataset.p / 100); }));
      }
      seen.add(x.path); frag.push(e);
    });
    els.forEach((e, p) => { if (!seen.has(p)) { e.remove(); els.delete(p); } });
    frag.forEach((e, i) => { if (box.children[i] !== e) box.insertBefore(e, box.children[i] || null); });
    if (!reduce && frag.length <= 80) frag.forEach(e => { const p = e.dataset.path, o = old.get(p); if (o == null) return; const d = o - e.getBoundingClientRect().top; if (Math.abs(d) > 1) { e.style.transition = 'none'; e.style.transform = `translateY(${d}px)`; requestAnimationFrame(() => { e.style.transition = 'transform 320ms cubic-bezier(.2,.7,.2,1)'; e.style.transform = ''; }); } });
    $$('mr-empty').classList.toggle('hidden', !!v.list.length);
    markKb();
  }
  const rowsVis = () => visible().list.filter(x => x.state === 'done' || x.state === 'short');
  const visibleIdx = p => rowsVis().findIndex(x => x.path === p);
  function markKb() {
    const l = rowsVis(); els.forEach(e => e.classList.remove('kb'));
    const x = l[M.kb]; if (x && els.get(x.path)) { const e = els.get(x.path); e.classList.add('kb'); e.scrollIntoView({block: 'nearest'}); }
  }

  // ---------- row actions ----------
  function stopPrev() { if (window.PVS) PVS.stop(); M.prevPath = ''; }
  function preview(x) { if (window.PVS) PVS.toggle(x); }   // F8: play/pause through the row's preview slider (static/js/preview-slider.js)
  async function useRow(x) {
    if (x.state !== 'done') return;
    stopPrev(); closeRes(); syncSide();
    const r = S.results.find(s => s.path === x.path) || x.r;
    await selectSong(r); useAudio(r);
  }
  async function autoSyncRow(x) {
    if (x.syncing || x.state !== 'done') return;
    stopPrev(); x.syncing = true; renderRes();
    const r = S.results.find(s => s.path === x.path) || x.r;
    try { await selectSong(r); useAudio(r); const b = $$('as-go'); if (b && !b.disabled) b.click();
      await sleep(300); while (window.AS && AS.running) await sleep(400); if (r.auto_sync_cached) x.r.auto_sync_cached = r.auto_sync_cached; } catch (e) { note(e.message, true); }
    x.syncing = false; renderRes();
  }

  // ---------- keyboard ----------
  window.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n' && !e.shiftKey) { e.preventDefault(); openNew(); return; }
    const resOpen = !$$('mr').classList.contains('hidden'), newOpen = !$$('nm').classList.contains('hidden');
    if (e.key === 'Escape') { if (resOpen) { e.preventDefault(); closeRes(); } else if (newOpen && !$$('nm-close').classList.contains('hidden')) { e.preventDefault(); closeNew(); } return; }
    if (newOpen && e.key === 'Enter' && !$$('nm-go').disabled && e.target.tagName !== 'BUTTON') { e.preventDefault(); startMatch(); return; }
    if (!resOpen) return;
    const typing = e.target && e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT';
    const l = rowsVis();
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); M.kb = Math.max(0, Math.min(l.length - 1, M.kb + (e.key === 'ArrowDown' ? 1 : -1))); markKb(); }
    else if (e.key === 'Enter' && !typing || (e.key === 'Enter' && typing && e.target.id === 'mr-q' && l[M.kb])) { e.preventDefault(); if (l[M.kb] || l[0]) useRow(l[M.kb] || l[0]); }
    else if (e.key === ' ' && !typing) { e.preventDefault(); const x = l[M.kb >= 0 ? M.kb : 0]; if (x) { if (M.kb < 0) { M.kb = 0; markKb(); } preview(x); } }
  });

  window.MATCH = {M, NM, openNew, openRes, closeRes, render: renderRes};
  // first launch with no project: ask for a match (desktop shell, or ?newmatch=1)
  const first = () => { if (!S.video && !M.hasMatch && $$('nm').classList.contains('hidden')) openNew(); };
  if (/[?&]newmatch=1/.test(location.search)) setTimeout(first, 0);
  else window.addEventListener('pywebviewready', () => setTimeout(first, 300));
  if (window.pywebview && window.pywebview.api) setTimeout(first, 300);
})();
