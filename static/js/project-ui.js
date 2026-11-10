'use strict';
// Project UI (.amvproj): top-bar controls, recent list, collect/restore, autosave, relink, shortcuts.
// Reads app globals (S, FX, AS, ...) read-only; restores by calling their existing UI functions.
(function () {
  const g = id => document.getElementById(id);
  const api = () => (window.pywebview && window.pywebview.api) || null;
  const baseOf = p => String(p || '').split(/[\\/]/).pop();
  const stripExt = n => n.replace(/\.amvproj$/i, '');
  const P = window.PROJECT = {path: '', name: 'Untitled', dirty: false, savedSig: '', loading: false, recent: [], created: null};
  const jp = async (u, b) => { const r = await jpost(u, b); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || j.error || r.statusText); return j; };
  const say = (m, err) => (window.note ? note(m, err) : console.log(m));

  // ---------- state <-> project ----------
  // Size/mtime are stamped by the server on save (project.save), so the UI sends just the path.
  async function entry(path) { return path ? {path, size: null, mtime: null} : null; }
  const num = (id, d) => { const v = parseFloat(g(id) && g(id).value); return isNaN(v) ? d : v; };
  function guards() { const A = window.AS; return A ? Object.assign({}, A.guard) : {}; }
  function planOf() {
    const A = window.AS; if (!A || !S.videoPath || !S.inUse) return null;
    const r = A.runs[S.videoPath + '|' + S.inUse.path]; return r ? {result: r.result, audioUrl: r.audioUrl || null, song: S.inUse.path} : null;
  }
  // Synchronous signature of everything saved (no file stats), used for the dirty dot.
  function sig() {
    const A = window.AS || {};
    return JSON.stringify([S.videoPath, (S.results || []).map(r => r.path), (S.dropDirs || []), S.inUse && S.inUse.path, +(S.offset || 0).toFixed(3), S.sp && S.sp.on, S.sp && S.sp.max,
      A.guard, FX, A.quality, planOf() ? 1 : 0, P.name]);
  }
  async function collect() {
    const songs = (S.results || []).filter(r => r.source !== 'original').map(r => r.path);
    const dirs = [...new Set((window.MATCH && MATCH.NM && MATCH.NM.dir ? [MATCH.NM.dir] : [g('music-dir').value.trim()]).filter(Boolean))];
    const A = window.AS || {};
    return {
      version: 1, name: P.name, created: P.created,
      video: await entry(S.videoPath),
      music_dirs: dirs,
      sources: await Promise.all(songs.map(entry)),
      in_use: S.inUse ? S.inUse.path : null,
      offset: S.offset || 0,
      splice: {on: !!S.sp.on, max_cuts: S.sp.max},
      autosync: {plan: planOf(), guardrails: guards(), lead_s: (A.guard ? A.guard.lead : 60) / 1000},
      effects: {boom: FX.boom, decay: FX.decay, drops_only: !!FX.drops, shake: !!FX.shake},
      export: {quality: A.quality || 'blend'},
      ui: {},
    };
  }
  function setRange(id, v) { const e = g(id); if (!e || v == null) return; e.value = v; e.dispatchEvent(new Event('input', {bubbles: true})); }
  function setChk(id, v) { const e = g(id); if (!e || e.checked === !!v) return; e.checked = !!v; e.dispatchEvent(new Event('change', {bubbles: true})); }
  const wait = async (fn, ms = 20000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return true; await new Promise(z => setTimeout(z, 120)); } return false; };

  async function restore(d) {
    const vpath = d.video && d.video.path;
    const songs = (d.sources || []).filter(s => !s.missing).map(s => s.path);
    if (d.music_dirs && d.music_dirs[0]) g('music-dir').value = d.music_dirs[0];
    if (vpath && !(d.video && d.video.missing)) {
      g('video-path').value = vpath;
      await POST('/api/register-paths', {paths: [vpath].concat(songs)}).then(r => r.json()).catch(() => {});
      await enqueue(() => doVideo({name: baseOf(vpath), path: vpath}));
      if (songs.length) await enqueue(() => doSongs(songs.map(p => ({name: baseOf(p), path: p}))));
      await wait(() => !songs.length || (S.results || []).some(r => r.source !== 'original'));
    }
    const A = window.AS;
    if (A && d.autosync) { Object.assign(A.guard, d.autosync.guardrails || {}); ['lead', 'dev', 'tempo', 'cuts'].forEach(k => A.guard[k] != null && setRange('g-' + k, A.guard[k])); setChk('g-loop', A.guard.loop); setChk('g-unlock', A.guard.unlock); }
    const e = d.effects || {};
    setRange('fx-boom', e.boom); setRange('fx-decay', e.decay); setChk('fx-drops', e.drops_only); setChk('fx-shake', e.shake);
    const q = (d.export || {}).quality; if (q && A) { const b = document.querySelector(`#rd-q button[data-q="${q}"]`); if (b && !b.classList.contains('on')) b.click(); }
    const sp = d.splice || {};
    S.sp.max = sp.max_cuts != null ? sp.max_cuts : S.sp.max;
    const mb = document.querySelector(`#sp-max button[data-n="${S.sp.max}"]`); if (mb) mb.click();
    if (sp.on) setChk('sp-on', true);
    if (d.in_use) {
      const r = (S.results || []).find(x => x.path === d.in_use);
      if (r) {
        const plan = d.autosync && d.autosync.plan;
        if (A && plan && plan.result) A.runs[S.videoPath + '|' + r.path] = {result: plan.result, audioUrl: plan.audioUrl || null};
        useAudio(r); await wait(() => S.sel === r && S.wf, 8000);
        if (d.offset != null && typeof setOffset === 'function' && S.sel && S.sel.source !== 'original') { S.manual = true; setOffset(d.offset); }
        if (typeof updateFit === 'function') updateFit();
      }
    }
  }

  // ---------- file ops ----------
  const dirOfProj = () => P.path.replace(/[\\/][^\\/]*$/, '');
  function mark(path, name) { P.path = path || P.path; if (name) P.name = name; P.savedSig = sig(); ui(); }
  async function askPath(kind, def) {
    const nd = window.nativeProjectDialogs, a = api();
    if (a && nd) { const p = await (kind === 'open' ? nd.open() : nd.save(def)); return p || ''; }
    return promptPath(kind === 'open' ? 'Open project (full path to a .amvproj)' : 'Save project as (full path ending in .amvproj)', def);
  }
  function promptPath(title, val) {
    return new Promise(res => {
      const d = dlg(`<h2>${esc(title)}</h2><input id="pj-p" spellcheck="false" style="width:100%" value="${esc(val || '')}"><div class="pj-row"><button id="pj-x">Cancel</button><button id="pj-ok" class="primary">OK</button></div>`);
      const i = g('pj-p'); i.focus(); i.select();
      const done = v => { d.remove(); res(v); };
      g('pj-ok').onclick = () => done(i.value.trim()); g('pj-x').onclick = () => done('');
      i.onkeydown = e => { if (e.key === 'Enter') done(i.value.trim()); else if (e.key === 'Escape') done(''); };
    });
  }
  function dlg(html) { const d = document.createElement('div'); d.className = 'pj-dlg'; d.innerHTML = `<div class="pj-box">${html}</div>`; document.body.appendChild(d); return d; }
  async function save(asNew, quiet) {
    let p = P.path;
    if (!p || asNew) { p = await askPath('save', (P.path || (P.name.replace(/[^\w .-]/g, '_') || 'Untitled')) ); if (!p) return false; if (!/\.amvproj$/i.test(p)) p += '.amvproj'; }
    try {
      if (!P.path || asNew) { if (!P.created || asNew) P.name = P.name === 'Untitled' ? stripExt(baseOf(p)) : P.name; }
      const data = await collect(); const j = await jp('/api/project/save', {path: p, data});
      P.created = (j.data && j.data.created) || P.created; mark(p); say('Saved ' + baseOf(p)); loadRecent(); return true;
    } catch (e) { say('Save failed: ' + e.message, true); return false; }
  }
  async function open(path) {
    if (P.dirty && !(await confirmDiscard())) return;
    const p = path || await askPath('open', ''); if (!p) return;
    try {
      const j = await jp('/api/project/load', {path: p}); const d = j.data;
      P.loading = true; P.name = d.name || stripExt(baseOf(p)); P.created = d.created; P.path = p;
      const miss = (j.missing || []).length ? j.missing : [d.video].concat(d.sources || []).filter(x => x && x.missing);
      if (miss.length) { const fixed = await relinkDialog(d, miss); if (fixed === null) { P.loading = false; return; } }
      await restore(d); P.loading = false; mark(p); say('Opened ' + P.name); loadRecent();
    } catch (e) { P.loading = false; say('Open failed: ' + e.message, true); }
  }
  function confirmDiscard() {
    return new Promise(res => {
      const d = dlg(`<h2>Unsaved changes</h2><p class="muted">Save changes to ${esc(P.name)} before continuing?</p><div class="pj-row"><button id="pj-c">Cancel</button><button id="pj-d">Discard</button><button id="pj-s" class="primary">Save</button></div>`);
      g('pj-c').onclick = () => { d.remove(); res(false); }; g('pj-d').onclick = () => { d.remove(); res(true); };
      g('pj-s').onclick = async () => { d.remove(); res(await save(false)); };
    });
  }

  // ---------- relink ----------
  function relinkDialog(d, miss) {
    return new Promise(async res => {
      const rows = [];
      let html = `<h2>Missing files</h2><p class="muted">${miss.length} file${miss.length > 1 ? 's' : ''} could not be found. Pick a replacement, or skip to open without ${miss.length > 1 ? 'them' : 'it'}.</p>`;
      for (let i = 0; i < miss.length; i++) {
        let cands = []; try { cands = (await jp('/api/project/relink', {entry: miss[i]})).candidates || []; } catch (e) {}
        rows.push({e: miss[i], cands});
        html += `<div class="pj-miss" data-i="${i}"><b>${esc(baseOf(miss[i].path))}</b><div><code>${esc(miss[i].path)}</code></div>
          <select><option value="">${cands.length ? 'Choose a match…' : 'No match found'}</option>${cands.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('')}</select>
          <div class="pj-row" style="justify-content:flex-start"><button class="pj-find">Find in folder…</button></div></div>`;
      }
      html += `<div class="pj-row"><button id="pj-cancel">Cancel</button><button id="pj-skip">Skip missing</button><button id="pj-apply" class="primary">Relink and open</button></div>`;
      const dl = dlg(html); dl.dataset.relink = '1';
      dl.querySelectorAll('.pj-find').forEach(b => b.onclick = async () => {
        const box = b.closest('.pj-miss'), i = +box.dataset.i;
        let root = ''; const a = api();
        root = a && a.pick_folder ? [].concat(await a.pick_folder() || [])[0] || '' : await promptPath('Folder to search', dirOfProj());
        if (!root) return;
        try {
          const r = await jp('/api/project/relink', {entry: rows[i].e, roots: [root]}), sel = box.querySelector('select');
          sel.innerHTML = `<option value="">${r.candidates.length ? 'Choose a match…' : 'No match found'}</option>` + r.candidates.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
          if (r.candidates[0]) sel.value = r.candidates[0];
        } catch (e) { say(e.message, true); }
      });
      dl.querySelectorAll('.pj-miss').forEach(box => { const s = box.querySelector('select'); if (rows[+box.dataset.i].cands[0]) s.value = rows[+box.dataset.i].cands[0]; });
      g('pj-cancel').onclick = () => { dl.remove(); res(null); };
      const finish = async apply => {
        const reg = [];
        dl.querySelectorAll('.pj-miss').forEach(box => {
          const v = apply ? box.querySelector('select').value : '', e = rows[+box.dataset.i].e;
          if (v) { e.path = v; delete e.missing; reg.push(v); }
        });
        if (reg.length) await POST('/api/register-paths', {paths: reg}).catch(() => {});
        dl.remove(); res(reg);
      };
      g('pj-skip').onclick = () => finish(false); g('pj-apply').onclick = () => finish(true);
    });
  }

  // ---------- top bar ----------
  function build() {
    const bar = document.createElement('span'); bar.className = 'pj-bar'; bar.id = 'pj-bar';
    bar.innerHTML = `<span id="pj-name" class="pj-name" title="Click to rename"></span><i id="pj-dot" class="pj-dot" title="Unsaved changes"></i>
      <button id="pj-open" class="mini" title="Open project (Cmd/Ctrl+O)">Open</button><button id="pj-save" class="mini" title="Save (Cmd/Ctrl+S)">Save</button><button id="pj-saveas" class="mini" title="Save As (Shift+Cmd/Ctrl+S)">Save As</button><button id="pj-rec" class="mini" title="Recent projects">Recent ▾</button>`;
    const brand = document.querySelector('.topbar .brand'); brand.after(bar);
    const menu = document.createElement('div'); menu.id = 'pj-menu'; menu.className = 'pj-menu hidden'; document.body.appendChild(menu);
    g('pj-name').onclick = rename;
    g('pj-open').onclick = () => open(); g('pj-save').onclick = () => save(false); g('pj-saveas').onclick = () => save(true);
    g('pj-rec').onclick = e => { e.stopPropagation(); const r = g('pj-rec').getBoundingClientRect(); menu.style.left = r.left + 'px'; menu.style.top = (r.bottom + 4) + 'px'; menu.classList.toggle('hidden'); drawRecent(menu); };
    document.addEventListener('click', e => { if (!e.target.closest('#pj-menu')) menu.classList.add('hidden'); });
    ui();
  }
  function rename() {
    const n = g('pj-name'), i = document.createElement('input'); i.className = 'pj-name-in'; i.value = P.name; n.replaceWith(i); i.focus(); i.select();
    const done = ok => { if (ok && i.value.trim()) P.name = i.value.trim(); const s = document.createElement('span'); s.id = 'pj-name'; s.className = 'pj-name'; s.title = 'Click to rename'; s.onclick = rename; i.replaceWith(s); ui(); };
    i.onkeydown = e => { if (e.key === 'Enter') done(true); else if (e.key === 'Escape') done(false); }; i.onblur = () => done(true);
  }
  function ui() {
    const n = g('pj-name'); if (n) { n.textContent = P.name; n.title = P.path || 'Unsaved project (click to rename)'; }
    const d = g('pj-dot'); if (d) d.classList.toggle('on', P.dirty);
    document.title = (P.dirty ? '• ' : '') + P.name + ' — AMV Matcher';
  }
  async function loadRecent() { try { P.recent = (await (await fetch('/api/project/recent')).json()).recent || []; } catch (e) { P.recent = []; } drawRecent(g('pj-menu')); drawRecent(g('pj-recent-nm')); }
  function drawRecent(box) {
    if (!box) return;
    const rows = P.recent.map(r => `<div class="pj-item${r.missing ? ' missing' : ''}" data-p="${esc(r.path)}" data-m="${r.missing ? 1 : ''}"><b>${esc(r.name || baseOf(r.path))}${r.missing ? '<span class="pj-flag">missing</span>' : ''}</b><small>${esc(r.path)}</small></div>`).join('') || '<div class="pj-item muted">No recent projects</div>';
    box.querySelectorAll('.pj-list').forEach(x => x.remove());
    const l = document.createElement('div'); l.className = 'pj-list'; l.innerHTML = rows; box.appendChild(l);
    l.querySelectorAll('.pj-item[data-p]').forEach(it => it.onclick = () => { if (it.dataset.m) return say('That project file is missing', true); g('pj-menu').classList.add('hidden'); const nm = g('nm'); if (nm) nm.classList.add('hidden'); open(it.dataset.p); });
  }
  function hookNewMatch() {
    const nm = document.querySelector('#nm .nm-box'); if (!nm || g('pj-recent-nm')) return;
    const d = document.createElement('div'); d.id = 'pj-recent-nm'; d.className = 'pj-recent-nm'; d.innerHTML = '<h4>Recent projects</h4>'; nm.appendChild(d); drawRecent(d);
  }

  // ---------- autosave / dirty ----------
  let busy = false;
  async function autosave() {
    if (busy || !P.dirty || P.loading || !S.videoPath) return; busy = true;
    try { const data = await collect(); await jp('/api/project/autosave', {data}); P.lastAuto = Date.now(); toastSaved(); } catch (e) {} busy = false;
  }
  function toastSaved() { const t = document.createElement('div'); t.className = 'toast pj-saved'; t.textContent = 'Saved'; (g('toasts') || document.body).appendChild(t); setTimeout(() => t.remove(), 1400); }
  function tick() {
    if (P.loading) return;
    const s = sig(); if (!P.savedSig) P.savedSig = S.videoPath ? '' : s;
    const d = !!S.videoPath && s !== P.savedSig; if (d !== P.dirty) { P.dirty = d; ui(); }
  }
  setInterval(tick, 700);
  setInterval(() => { if (P.dirty) { if (P.path) save(false, true).then(() => {}); else autosave(); } }, 30000);
  // Named projects autosave to autosave/ as well so a crash never loses work.
  const closeSave = () => { if (P.dirty) autosave(); };
  const prev = window.nativeBeforeQuit; window.nativeBeforeQuit = () => { closeSave(); return prev ? prev() : undefined; };
  window.addEventListener('beforeunload', () => { if (P.dirty) { try { navigator.sendBeacon('/api/project/autosave', new Blob([JSON.stringify({data: {version: 1, name: P.name, video: S.videoPath ? {path: S.videoPath} : null, in_use: S.inUse && S.inUse.path, offset: S.offset}})], {type: 'application/json'})); } catch (e) {} } });

  // ---------- menu / keyboard (native.js dispatches amv-menu for Cmd/Ctrl+S/O) ----------
  document.addEventListener('amv-menu', e => {
    const a = e.detail && e.detail.action;
    if (a === 'save') save(false); else if (a === 'save_as') save(true); else if (a === 'open') open();
  });
  if (!window.nativeMenu) document.addEventListener('keydown', e => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return; const k = e.key.toLowerCase();
    if (k === 's') { e.preventDefault(); save(e.shiftKey); } else if (k === 'o') { e.preventDefault(); open(); }
  });

  window.PROJECT_UI = {collect, restore, save, open, loadRecent, sig, autosave, relinkDialog};
  build(); hookNewMatch(); loadRecent();
})();
