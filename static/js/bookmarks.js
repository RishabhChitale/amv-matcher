'use strict';
/* F6d: timeline bookmarks. Mark moments on the V1 (video) and A1 (audio) tracks, tag each with a type and a note,
   keep them in the project, and export/import them as JSON + CSV (reviews and "perfect sync" examples).
   Add: Mark mode + click on a track, double-click a track, or press B (adds on the selected track at the playhead).
   Edit: click a marker (type, note, time, delete); drag it to move; Delete removes the selected marker.
   Store: window.BM. Project top-level fields: `bookmarks`[], `perfect_sync_example`. Format: docs/TIMELINE-LOG-FORMAT.md. */
(function () {
  const TYPES = [
    { id: 'drop', label: 'Drop', color: '#22d3ee', shape: 'diamond' },
    { id: 'initial_boom', label: 'Initial boom', color: '#ff6b81', shape: 'burst' },
    { id: 'encore', label: 'Encore', color: '#a78bfa', shape: 'tri' },
    { id: 'visual_boom', label: 'Visual boom', color: '#fbbf24', shape: 'square' },
    { id: 'perfect_sync_moment', label: 'Perfect-sync moment', color: '#4ade80', shape: 'star' },
    { id: 'other', label: 'Other', color: '#e5e7eb', shape: 'ring' },
  ];
  const T = {}; TYPES.forEach(t => { T[t.id] = t; });
  const el = id => document.getElementById(id);
  const cv = el('tl'); if (!cv) return;
  const R = 7;                                   // marker half-size in px
  const BM = window.BM = { items: [], perfect: false, sel: null, markMode: false, TYPES };
  const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(6)), x => x.toString(16).padStart(2, '0')).join('');   // 12 hex, as the backend
  const nowIso = () => new Date().toISOString();
  const dur = () => (window.S && S.dur) || 0;
  const tx = t => (typeof t2x === 'function' ? t2x(t) : 0);
  const xt = x => (typeof x2t === 'function' ? x2t(x) : 0);
  const redraw = () => { if (typeof draw === 'function') draw(); };
  const r3 = v => Math.round(v * 1000) / 1000;
  const clampT = t => Math.max(0, Math.min(dur() || 0, t));
  const rowY = tr => (tr === 'video' ? (typeof RULER === 'number' ? RULER : 26) + 12 : (typeof RULER === 'number' ? RULER + V1H + 4 : 114) + 12);
  const songT = t => (window.S && S.sel && S.sel.source !== 'original' ? t + (S.offset || 0) : t);

  // ---------- store ----------
  function changed() { renderBar(); redraw(); if (window.PROJECT && PROJECT_UI && PROJECT_UI.sig) { /* dirty dot follows sig() */ } }
  BM.add = function (track, t, type, note) {
    if (!dur()) return null;
    const b = { id: newId(), track: track === 'audio' ? 'audio' : 'video', t: r3(clampT(t)), type: T[type] ? type : 'other', note: note || '', created: nowIso(), modified: nowIso() };
    BM.items.push(b); BM.sel = b.id; changed(); sync('add', b); return b;
  };
  BM.get = id => BM.items.find(b => b.id === id) || null;
  BM.remove = function (id) { const i = BM.items.findIndex(b => b.id === id); if (i < 0) return; BM.items.splice(i, 1); if (BM.sel === id) BM.sel = null; closePop(); changed(); sync('del', { id }); };
  BM.update = function (id, patch) { const b = BM.get(id); if (!b) return; if (patch.type && T[patch.type]) b.type = patch.type; if (patch.note != null) b.note = String(patch.note); if (patch.t != null) b.t = r3(clampT(patch.t)); b.modified = nowIso(); changed(); sync('upd', b); };
  BM.snapshot = () => ({ bookmarks: BM.items.map(b => Object.assign({}, b)), perfect_sync_example: !!BM.perfect });   // top-level project fields
  BM.load = function (d) {
    BM.loading = true; d = d || {}; BM.perfect = !!d.perfect_sync_example; BM.sel = null; closePop();
    BM.items = (Array.isArray(d.bookmarks) ? d.bookmarks : (Array.isArray(d.items) ? d.items : [])).filter(b => b && isFinite(b.t)).map(b => ({ id: b.id || newId(), track: b.track === 'audio' ? 'audio' : 'video', t: +b.t, type: T[b.type] ? b.type : (b.type === 'perfect_sync' ? 'perfect_sync_moment' : 'other'), note: String(b.note || ''), created: b.created || nowIso(), modified: b.modified || nowIso() }));
    BM.loading = false; changed();
  };
  BM.sig = () => JSON.stringify([BM.perfect, BM.items.map(b => [b.track, b.t, b.type, b.note])]);

  // ---------- export / import ----------
  const csvCell = v => { v = String(v == null ? '' : v); return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  BM.toJSON = function () {
    const base = n => String(n || '').split(/[\\/]/).pop();
    return JSON.stringify({
      format: 'amv-timelines', version: 1, exported_at: new Date().toISOString(),
      project: (window.PROJECT && PROJECT.name) || '', video: base(window.S && S.videoPath), song: base(window.S && S.inUse && S.inUse.path),
      song_offset_s: r3((window.S && S.offset) || 0), duration_s: r3(dur()), perfect_sync_example: !!BM.perfect,
      bookmarks: BM.items.slice().sort((a, b) => a.t - b.t).map(b => ({ id: b.id, track: b.track, t: b.t, timeline_t: b.track === 'audio' ? r3(b.t - (S.offset || 0)) : b.t, type: b.type, note: b.note, created: b.created, modified: b.modified })),
    }, null, 2);
  };
  BM.toCSV = function () {
    const rows = [['id', 'track', 't', 'timeline_t', 'type', 'note', 'perfect_sync_example']];
    BM.items.slice().sort((a, b) => a.t - b.t).forEach(b => rows.push([b.id, b.track, b.t, b.track === 'audio' ? r3(b.t - (S.offset || 0)) : b.t, b.type, b.note, BM.perfect ? 'yes' : 'no']));
    return rows.map(r => r.map(csvCell).join(',')).join('\r\n') + '\r\n';
  };
  function parseCSV(s) {
    const out = []; let row = [], cur = '', q = false;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (q) { if (c === '"') { if (s[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
      else if (c === '"') q = true; else if (c === ',') { row.push(cur); cur = ''; }
      else if (c === '\n' || c === '\r') { if (c === '\r' && s[i + 1] === '\n') i++; row.push(cur); cur = ''; if (row.some(x => x !== '')) out.push(row); row = []; }
      else cur += c;
    }
    row.push(cur); if (row.some(x => x !== '')) out.push(row); return out;
  }
  BM.parseImport = function (text, name) {
    if (/\.csv$/i.test(name || '') || !/^\s*[{[]/.test(text)) {
      const rows = parseCSV(text), h = (rows.shift() || []).map(x => x.trim().toLowerCase()), ix = k => h.indexOf(k);
      return { perfect_sync_example: rows.some(r => (r[ix('perfect_sync_example')] || '').toLowerCase() === 'yes'),
        items: rows.map(r => ({ id: r[ix('id')], track: r[ix('track')], t: parseFloat(r[ix(h.includes('t') ? 't' : 'time_s')]), type: r[ix('type')], note: r[ix('note')] })) };
    }
    const j = JSON.parse(text), list = Array.isArray(j) ? j : (j.bookmarks || j.items || []);
    return { perfect_sync_example: !!j.perfect_sync_example, items: list.map(b => ({ id: b.id, track: b.track, t: b.t != null ? b.t : b.time_s, type: b.type, note: b.note })) };
  };
  async function saveText(name, text, mime) {
    const nat = window.pywebview && pywebview.api && pywebview.api.save_text;
    if (nat) { const d = await pywebview.api.save_text(name, text); return d ? 'Saved to ' + d : 'Not saved'; }
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], { type: mime })); a.download = name;
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); return 'Downloaded ' + name;
  }
  const say = (m, err) => (window.note ? note(m, err) : console.log(m));
  // ---------- server sync: bookmark CRUD in the saved project file (docs/TIMELINE-LOG-FORMAT.md) ----------
  // Only when the project has a path on disk; otherwise the bookmarks live in the app state and are saved with the project.
  // Operations run one after another; a new bookmark adopts the id the server gives it.
  const alias = {};                                   // local id -> server id (until the item itself is renamed)
  let chain = Promise.resolve();
  const projPath = () => (window.PROJECT && PROJECT.path) || '';
  const J = (url, method, body) => fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined })
    .then(async r => { if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) { } const er = new Error(m); er.status = r.status; throw er; } return r; });
  function sync(op, b) {
    if (BM.loading || !projPath()) return;
    const path = projPath(), id0 = b.id;
    chain = chain.then(async () => {
      try {
        if (op === 'add') {
          const cur = BM.get(id0); if (!cur) return;       // already deleted again
          const r = await (await J('/api/project/bookmarks', 'POST', { project: path, track: cur.track, t: cur.t, type: cur.type, note: cur.note })).json();
          const sid = r.result && r.result.id, again = BM.get(id0);
          if (sid && again) { alias[id0] = sid; again.id = sid; if (BM.sel === id0) BM.sel = sid; if (pendingPop === id0) pendingPop = sid; }
          else if (sid) await J('/api/project/bookmarks/' + sid + '?project=' + encodeURIComponent(path), 'DELETE');
        } else if (op === 'upd') {
          const cur = BM.get(b.id) || BM.get(alias[id0] || ''); if (!cur) return;
          await J('/api/project/bookmarks/' + cur.id, 'PATCH', { project: path, track: cur.track, t: cur.t, type: cur.type, note: cur.note });
        } else if (op === 'del') {
          await J('/api/project/bookmarks/' + (alias[id0] || id0) + '?project=' + encodeURIComponent(path), 'DELETE');
        } else if (op === 'perfect') {
          await J('/api/project/perfect-sync', 'POST', { project: path, value: !!BM.perfect });
        }
      } catch (e) {
        if (e.status === 404 || e.status === 405) return;   // endpoint absent, or an item the file never had: the project save will carry it
        say('Bookmark not saved to the project file: ' + e.message, true);
      }
    });
    return chain;
  }
  BM.sync = sync; BM.idle = () => chain;
  let pendingPop = null;

  // ---------- export / import: the server's timeline-log; client-side JSON/CSV only when the endpoints are absent ----------
  const absent = e => !e.status ? true : (e.status === 404 || e.status === 405);
  const projectData = async () => (window.PROJECT_UI && PROJECT_UI.collect ? await PROJECT_UI.collect() : { bookmarks: BM.items, perfect_sync_example: BM.perfect });
  BM.exportAll = async function () {
    const stem = ((window.PROJECT && PROJECT.name) || 'timelines').replace(/[^\w.-]+/g, '_');
    try {
      await chain;
      let jt, ct, via = 'server';
      try {
        const data = await projectData();
        jt = await (await J('/api/timeline-log', 'POST', { data, format: 'json' })).text();
        ct = await (await J('/api/timeline-log', 'POST', { data, format: 'csv' })).text();
      } catch (e) {
        if (!absent(e)) throw e;
        jt = BM.toJSON(); ct = BM.toCSV(); via = 'local';
      }
      const a = await saveText(stem + '-timelines.json', jt, 'application/json');
      const b = await saveText(stem + '-timelines.csv', ct, 'text/csv');
      say('Timelines: ' + a + ' / ' + b + (via === 'local' ? ' (basic format: the server export is unavailable)' : ''));
      BM.lastExport = { via, json: jt, csv: ct };
    } catch (e) { say('Export failed: ' + e.message, true); }
  };
  BM.importText = async function (text, name) {
    const csv = /\.csv$/i.test(name || '') || !/^\s*[{[]/.test(text);
    if (/"format"\s*:\s*"amv-timelines"/.test(text)) return importLocal(text, name);     // older client-only export
    try {
      await chain;
      const body = { log: text, format: csv ? 'csv' : 'json', mode: BM.items.length ? 'merge' : 'replace' };
      if (projPath()) body.project = projPath(); else body.data = await projectData();
      const r = await (await J('/api/timeline-log/import', 'POST', body)).json();
      BM.load({ bookmarks: r.bookmarks, perfect_sync_example: r.perfect_sync_example });
      BM.lastImport = r;
      const mm = r.media_match || {}, bad = [mm.video === false ? 'video' : '', mm.audio === false ? 'audio' : ''].filter(Boolean);
      say('Imported ' + BM.items.length + ' bookmarks' + (body.mode === 'merge' ? ' (merged with the current ones)' : '') +
        (bad.length ? '. Heads-up: this review was made on a different ' + bad.join(' and ') + ' file' : ''), !!bad.length);
    } catch (e) {
      if (absent(e)) return importLocal(text, name);
      say('Import failed: ' + e.message, true);
    }
  };
  function importLocal(text, name) {
    try {
      const d = BM.parseImport(text, name); if (!d.items.length) throw new Error('no bookmarks found');
      BM.load(d); say('Imported ' + BM.items.length + ' bookmarks');
    } catch (e) { say('Import failed: ' + e.message, true); }
  }

  // ---------- drawing (wraps the track hooks) ----------
  function shape(ctx, s, x, y, r) {
    ctx.beginPath();
    if (s === 'diamond') { ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath(); }
    else if (s === 'tri') { ctx.moveTo(x, y - r); ctx.lineTo(x + r, y + r - 1); ctx.lineTo(x - r, y + r - 1); ctx.closePath(); }
    else if (s === 'square') ctx.rect(x - r + 1, y - r + 1, 2 * r - 2, 2 * r - 2);
    else if (s === 'star' || s === 'burst') {
      const n = s === 'star' ? 5 : 8, ri = s === 'star' ? r * 0.45 : r * 0.55;
      for (let i = 0; i < n * 2; i++) { const a = -Math.PI / 2 + i * Math.PI / n, rr = i % 2 ? ri : r; i ? ctx.lineTo(x + rr * Math.cos(a), y + rr * Math.sin(a)) : ctx.moveTo(x + rr * Math.cos(a), y + rr * Math.sin(a)); }
      ctx.closePath();
    } else ctx.arc(x, y, r - 1, 0, Math.PI * 2);
  }
  function drawTrack(ctx, track, y0, t2x) {
    BM.items.forEach(b => {
      if (b.track !== track) return;
      const x = t2x(b.t), y = y0 + 12, t = T[b.type], sel = BM.sel === b.id;
      ctx.save();
      ctx.strokeStyle = 'rgba(255,255,255,.35)'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x + .5, y + R); ctx.lineTo(x + .5, y0 + (track === 'video' ? 84 : 76)); ctx.stroke();
      shape(ctx, t.shape, x, y, sel ? R + 2 : R);
      ctx.fillStyle = t.type === 'ring' || t.shape === 'ring' ? 'rgba(15,17,24,.9)' : t.color; ctx.fill();
      ctx.lineWidth = t.shape === 'ring' ? 2 : 1.5; ctx.strokeStyle = sel ? '#fff' : (t.shape === 'ring' ? t.color : 'rgba(10,12,18,.9)'); ctx.stroke();
      ctx.restore();
    });
  }
  const v1 = window.asDrawV1, a1 = window.asDrawA1;
  window.asDrawV1 = function (ctx, y0, h, t2x) { if (v1) v1.apply(this, arguments); drawTrack(ctx, 'video', y0, t2x); };
  window.asDrawA1 = function (ctx, y0, h, t2x, W) { if (a1) a1.apply(this, arguments); drawTrack(ctx, 'audio', y0, t2x); };
  // the V1 hook only runs when a video is loaded; A1's runs always, so drawing here is safe.

  // ---------- interaction on the canvas ----------
  const trackAt = y => {
    const r0 = typeof RULER === 'number' ? RULER : 26, vh = typeof V1H === 'number' ? V1H : 84, ah = typeof A1H === 'number' ? A1H : 84;
    return y >= r0 && y < r0 + vh ? 'video' : y >= r0 + vh && y < r0 + vh + ah ? 'audio' : null;
  };
  function hit(x, y) {
    let best = null, bd = 1e9;
    BM.items.forEach(b => { const dx = Math.abs(tx(b.t) - x), dy = Math.abs(rowY(b.track) - y); if (dx <= R + 3 && dy <= R + 6 && dx < bd) { best = b; bd = dx; } });
    return best;
  }
  let mdrag = null;
  const pt = e => { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  cv.addEventListener('pointerdown', e => {
    if (!dur() || e.button) return;
    const p = pt(e), b = hit(p.x, p.y);
    if (b) {
      e.stopImmediatePropagation(); e.preventDefault(); BM.sel = b.id; mdrag = { id: b.id, x0: p.x, moved: false };
      cv.setPointerCapture(e.pointerId); closePop(); redraw(); return;
    }
    const tr = trackAt(p.y);
    if (BM.markMode && tr) { e.stopImmediatePropagation(); e.preventDefault(); const nb = BM.add(tr, xt(p.x), 'other'); if (nb) openPop(nb); return; }
    if (BM.sel) { BM.sel = null; closePop(); redraw(); }
  }, true);
  cv.addEventListener('pointermove', e => {
    if (!mdrag) { const p = pt(e); cv.style.cursor = hit(p.x, p.y) ? 'grab' : ''; return; }
    e.stopImmediatePropagation(); const p = pt(e);
    if (!mdrag.moved && Math.abs(p.x - mdrag.x0) < 3) return;
    mdrag.moved = true; cv.style.cursor = 'grabbing'; const b = BM.get(mdrag.id); if (b) { b.t = r3(clampT(xt(p.x))); redraw(); }
  }, true);
  cv.addEventListener('pointerup', e => {
    if (!mdrag) return; e.stopImmediatePropagation(); const d = mdrag; mdrag = null; cv.style.cursor = '';
    const b = BM.get(d.id); if (b) { if (d.moved) { changed(); sync('upd', b); } else openPop(b); }
  }, true);
  cv.addEventListener('dblclick', e => {
    if (!dur()) return; const p = pt(e); if (hit(p.x, p.y)) return;
    const tr = trackAt(p.y); if (tr) { const nb = BM.add(tr, xt(p.x), 'other'); if (nb) openPop(nb); }
  });
  document.addEventListener('keydown', e => {
    const t = e.target, tag = (t && t.tagName) || '';
    if ((t && t.isContentEditable) || /INPUT|TEXTAREA|SELECT/.test(tag)) { if (e.key === 'Escape' && pop && pop.contains(t)) { e.stopPropagation(); closePop(); } return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'b' || e.key === 'B') {
      if (!dur()) return; e.preventDefault(); e.stopImmediatePropagation();
      const tr = window.S && S.selClip === 'a' ? 'audio' : 'video', nb = BM.add(tr, vid.currentTime, 'other'); if (nb) openPop(nb);
    } else if ((e.key === 'Delete' || e.key === 'Backspace') && BM.sel) { e.preventDefault(); e.stopImmediatePropagation(); BM.remove(BM.sel); }
    else if (e.key === 'Escape' && pop && !pop.classList.contains('hidden')) { closePop(); }
  }, true);

  // ---------- editor popover ----------
  let pop = null;
  function ensurePop() {
    if (pop) return pop;
    pop = document.createElement('div'); pop.id = 'bm-pop'; pop.className = 'bm-pop glass hidden'; pop.setAttribute('role', 'dialog'); pop.setAttribute('aria-label', 'Edit bookmark');
    pop.innerHTML = '<div class="bm-pop-h"><b id="bm-p-title"></b><span class="muted" id="bm-p-time"></span></div>' +
      '<select id="bm-p-type" aria-label="Bookmark type">' + TYPES.map(t => `<option value="${t.id}">${t.label}</option>`).join('') + '</select>' +
      '<input id="bm-p-note" type="text" placeholder="Note (optional)" maxlength="200" aria-label="Bookmark note">' +
      '<div class="bm-pop-f"><button id="bm-p-del" class="linkbtn">Delete</button><button id="bm-p-ok">Done</button></div>';
    el('tl-wrap').appendChild(pop);
    pop.querySelector('#bm-p-type').onchange = e => BM.sel && BM.update(BM.sel, { type: e.target.value });
    pop.querySelector('#bm-p-note').oninput = e => BM.sel && (BM.get(BM.sel).note = e.target.value);
    pop.querySelector('#bm-p-note').onkeydown = e => { if (e.key === 'Enter') closePop(); e.stopPropagation(); if (e.key === 'Escape') closePop(); };
    pop.querySelector('#bm-p-note').onchange = () => { changed(); const b = BM.get(BM.sel); if (b) { b.modified = nowIso(); sync('upd', b); } };
    pop.querySelector('#bm-p-del').onclick = () => BM.sel && BM.remove(BM.sel);
    pop.querySelector('#bm-p-ok').onclick = () => closePop();
    return pop;
  }
  function openPop(b) {
    ensurePop(); BM.sel = b.id;
    pop.querySelector('#bm-p-title').textContent = (b.track === 'audio' ? 'Audio' : 'Video') + ' bookmark';
    pop.querySelector('#bm-p-time').textContent = fmtT(b.t); pop.querySelector('#bm-p-type').value = b.type; pop.querySelector('#bm-p-note').value = b.note;
    pop.classList.remove('hidden');
    const wrap = el('tl-wrap'), w = pop.offsetWidth || 230, x = cv.offsetLeft + tx(b.t), maxL = wrap.clientWidth - w - 6;
    pop.style.left = Math.max(cv.offsetLeft, Math.min(maxL, x - w / 2)) + 'px';
    pop.style.top = (cv.offsetTop + (b.track === 'video' ? (typeof RULER === 'number' ? RULER : 26) + 26 : (typeof RULER === 'number' ? RULER + V1H : 110) + 26)) + 'px';
    redraw(); pop.querySelector('#bm-p-note').focus();
  }
  function closePop() { if (pop) pop.classList.add('hidden'); }
  const fmtT = t => { const m = Math.floor(t / 60), s = t - m * 60; return m + ':' + (s < 10 ? '0' : '') + s.toFixed(2); };

  // ---------- bar: mark mode, legend, perfect-sync, export/import ----------
  function renderBar() {
    const bar = el('bm-bar'); if (!bar) return;
    const n = BM.items.length, mb = el('bm-mark'), pf = el('bm-perfect'), cnt = el('bm-count');
    if (mb) { mb.classList.toggle('on', BM.markMode); mb.setAttribute('aria-pressed', BM.markMode ? 'true' : 'false'); }
    if (pf) pf.checked = BM.perfect;
    if (cnt) cnt.textContent = n ? n + (n === 1 ? ' bookmark' : ' bookmarks') : 'No bookmarks';
    const ex = el('bm-export'); if (ex) ex.disabled = !n;
  }
  function buildBar() {
    const wrap = el('tl-wrap'); if (!wrap || el('bm-bar')) return;
    const bar = document.createElement('div'); bar.id = 'bm-bar'; bar.className = 'bm-bar glass';
    const sw = t => { const c = document.createElement('canvas'); c.width = c.height = 18; c.className = 'bm-sw'; const x = c.getContext('2d'); shape(x, t.shape, 9, 9, 7); x.fillStyle = t.shape === 'ring' ? 'rgba(15,17,24,.9)' : t.color; x.fill(); x.lineWidth = 1.5; x.strokeStyle = t.shape === 'ring' ? t.color : 'rgba(10,12,18,.9)'; x.stroke(); return c; };
    bar.innerHTML = '<button id="bm-mark" class="toggle bm-mark" aria-pressed="false" title="Mark mode: click V1 or A1 to add a bookmark (or press B at the playhead, or double-click a track)">Mark</button>' +
      '<span class="bm-leg" id="bm-legend" aria-label="Bookmark types"></span><span class="muted" id="bm-count">No bookmarks</span><span class="bm-sp"></span>' +
      '<label class="chk" title="Flag this whole project as an example of perfect sync"><input type="checkbox" id="bm-perfect"> Perfect sync example</label>' +
      '<button id="bm-export" title="Download bookmarks as JSON and CSV" disabled>Export timelines</button><button id="bm-import" title="Load bookmarks from a JSON or CSV file">Import timelines</button>' +
      '<input type="file" id="bm-file" accept=".json,.csv,application/json,text/csv" class="hidden">';
    wrap.parentNode.insertBefore(bar, wrap);
    const lg = bar.querySelector('#bm-legend');
    TYPES.forEach(t => { const s = document.createElement('span'); s.className = 'bm-lg'; s.dataset.type = t.id; s.appendChild(sw(t)); const lt = document.createElement('span'); lt.className = 'bm-lt'; lt.textContent = t.label; s.title = t.label; s.appendChild(lt); lg.appendChild(s); });
    el('bm-mark').onclick = () => { BM.markMode = !BM.markMode; cv.classList.toggle('bm-marking', BM.markMode); renderBar(); };
    el('bm-perfect').onchange = e => { BM.perfect = e.target.checked; renderBar(); sync('perfect', {}); if (window.PROJECT_UI) { /* dirty via sig() */ } };
    el('bm-export').onclick = () => BM.exportAll();
    el('bm-import').onclick = () => el('bm-file').click();
    el('bm-file').onchange = async e => { const f = e.target.files[0]; e.target.value = ''; if (f) BM.importText(await f.text(), f.name); };
    renderBar();
  }
  buildBar();
})();
