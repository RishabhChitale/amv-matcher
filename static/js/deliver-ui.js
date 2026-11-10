'use strict';
// Edit / Deliver pages. Reuses the existing render flow (autosync.js rd-* controls); this file only adds the tabs, the Deliver panel,
// the render playhead over the timeline, ETA, and the "no media changes on Deliver / while rendering" lock.
(function () {
  const $ = id => document.getElementById(id);
  const D2 = {page: 'edit', t0: 0, queue: [], lastOut: ''};
  const bar = document.createElement('div'); bar.id = 'pages';
  bar.innerHTML = '<button data-pg="edit" class="on">Edit</button><button data-pg="deliver">Deliver</button>';
  document.body.appendChild(bar);
  const panel = document.createElement('section'); panel.id = 'deliver'; panel.className = 'glass panel';
  panel.innerHTML = `<h3>Deliver</h3>
    <div class="dl-sum" id="dl-sum"></div>
    <div class="dl-row" id="dl-q"><span>Quality</span><button data-q="auto">Auto</button><button data-q="rife">RIFE</button><button data-q="blend">Blend</button><button data-q="dup">Duplicate</button></div>
    <div class="dl-sum" id="dl-fx"></div>
    <div class="dl-row"><button id="dl-go">Render</button><button id="dl-cancel" class="hidden">Cancel</button><button id="dl-folder">Output folder</button></div>
    <div class="dl-lock hidden" id="dl-lock"></div>
    <div class="dl-bar"><i id="dl-fill"></i></div>
    <div class="dl-sum" id="dl-status">Idle</div>
    <h3>Render queue</h3><ul id="dl-queue"></ul>`;
  document.querySelector('main.layout').appendChild(panel);
  const ph = document.createElement('div'); ph.id = 'dl-playhead'; const tl = $('tl-wrap'); tl.style.position = tl.style.position || 'relative'; tl.appendChild(ph);

  const busy = () => !!(window.AS && AS.rd && AS.rd.running && !AS.rd.installing);
  const locked = () => D2.page === 'deliver' || busy();
  const msg = () => busy() ? 'A render is running: media changes are disabled until it finishes or is cancelled.' : 'Media drops and linking are disabled on Deliver. Switch to Edit to change media.';
  const warn = () => { if (typeof note === 'function') note(msg(), true); };

  function setPage(p) { D2.page = p; document.body.classList.toggle('page-deliver', p === 'deliver');
    bar.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.pg === p)); sync(); }
  bar.onclick = e => { const b = e.target.closest('button'); if (b) setPage(b.dataset.pg); };

  // media lock: capture-phase drop/dragover guard + wrap the shared entry points
  ['dragover', 'drop'].forEach(t => window.addEventListener(t, e => {
    if (!locked()) return; e.preventDefault(); e.stopImmediatePropagation(); if (e.dataTransfer) e.dataTransfer.dropEffect = 'none'; if (t === 'drop') warn();
  }, true));
  const origND = window.nativeDrop;
  if (origND) window.nativeDrop = function (paths) { if (locked()) { warn(); return Promise.resolve(); } return origND.apply(this, arguments); };
  if (typeof link === 'function') { const ol = link; link = function () { if (locked()) { warn(); return Promise.resolve(); } return ol.apply(this, arguments); }; }
  document.addEventListener('click', e => { if (locked() && e.target.closest('#media, .m-add, #b-new, #nm-go')) { e.preventDefault(); e.stopImmediatePropagation(); warn(); } }, true);

  function fmt(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
  function sync() {
    const b = busy(), lk = locked();
    document.body.classList.toggle('rendering', b); document.body.classList.toggle('locked-media', lk);
    const q = window.AS && AS.quality; panel.querySelectorAll('#dl-q button').forEach(x => x.classList.toggle('on', x.dataset.q === q));
    $('dl-go').disabled = b || ($('rd-go') && $('rd-go').disabled); $('dl-cancel').classList.toggle('hidden', !b);
    const l = $('dl-lock'); l.classList.toggle('hidden', !lk); l.textContent = lk ? msg() : '';
    const fx = (typeof FX !== 'undefined' ? FX : {}); $('dl-fx').textContent = `Boom ${fx.boom != null ? fx.boom : 40} · decay ${fx.decay != null ? fx.decay : 180}${fx.drops ? ' · drops only' : ''}${fx.shake ? ' · shake' : ''}`;
    const dur = window.S && S.dur; $('dl-sum').textContent = dur ? `Range: whole retimed video (${fmt(dur)}).` : 'Run Auto-sync first.';
    const w = parseFloat(($('rd-bar') || {style: {}}).style.width) || 0, f = b ? Math.min(1, w / 100) : 0;
    $('dl-fill').style.width = (b ? w : 0) + '%'; ph.style.left = (f * 100) + '%';
    const st = ($('rd-stage') && $('rd-stage').textContent) || '';
    if (b) { if (!D2.t0) D2.t0 = Date.now(); const el = (Date.now() - D2.t0) / 1000, eta = f > 0.05 ? el * (1 - f) / f : null;
      $('dl-status').textContent = `${st} · ${Math.round(w)}%` + (eta != null ? ` · ETA ${fmt(eta)}` : ' · estimating…'); }
    else { if (D2.t0) { D2.t0 = 0; capture(st); } $('dl-status').textContent = st || 'Idle'; }
  }
  function capture(st) {
    const out = $('rd-out'), a = out && out.querySelector('a.dl, button.dl'), name = a ? a.textContent.replace(/^(Open|Save)\s+/, '').replace(/…$/, '') : '';
    if (/Done/.test(st) && name && name !== D2.lastOut) { D2.lastOut = name; D2.queue.unshift(name); }
    else if (/Cancelled|failed/i.test(st)) D2.queue.unshift(st);
    $('dl-queue').innerHTML = D2.queue.slice(0, 8).map(n => `<li><span>${String(n).replace(/[<&]/g, c => c === '<' ? '&lt;' : '&amp;')}</span></li>`).join('');
  }
  panel.querySelector('#dl-q').onclick = e => { const b = e.target.closest('button[data-q]'); if (b && !busy()) { const x = document.querySelector(`#rd-q button[data-q="${b.dataset.q}"]`); if (x) x.click(); sync(); } };
  $('dl-go').onclick = () => { if (!busy()) $('rd-go').click(); };
  $('dl-cancel').onclick = () => $('rd-cancel').click();
  $('dl-folder').onclick = () => { const a = document.querySelector('#rd-out #rd-reveal'); if (a) a.click(); else if (typeof note === 'function') note('Renders are saved to the .exports folder inside the app data folder.'); };
  setInterval(sync, 500); sync();
  window.DELIVER_UI = {setPage, locked, get page() { return D2.page; }};
})();
