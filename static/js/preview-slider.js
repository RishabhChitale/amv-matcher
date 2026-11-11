'use strict';
// F8: per-song preview slider for the scan list (match-results popup). One song plays at a time; the <audio> element is
// created lazily on the first play (no fetch/decode before that). Seeking a stopped row only moves its start position.
// F6 hook: marker ticks (boom / drop times, seconds in the song) come from PVS.markersOf(r) -> {boom:[s], drop:[s]}; edit
// markersOf once the result field names are final, or call PVS.setMarkers(path, {boom, drop}) from anywhere.
(function () {
  const fmt = s => { s = Math.max(0, Math.round(s || 0)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
  const A = {a: null, path: '', x: null, raf: 0};
  const pos = new Map();      // path -> remembered position (s)
  const extra = new Map();    // path -> markers set through setMarkers
  const dur = x => (x.r && x.r.song_duration) || 0;
  const markersOf = r => extra.get(r && r.path) || {boom: (r && r.boom_times) || [], drop: (r && r.drop_times) || []};
  const rowEl = p => document.querySelector('#mr-list .mr-row[data-path="' + (window.CSS && CSS.escape ? CSS.escape(p) : p) + '"]');
  const ui = e => e && ({btn: e.querySelector('.pvs-btn'), rng: e.querySelector('.pvs-rng'), cur: e.querySelector('.pvs-cur')});

  const ticksHTML = (r, d) => { const mk = markersOf(r), t = (arr, c) => (arr || []).filter(v => d > 0 && v >= 0 && v <= d).map(v => `<i class="pvs-tick ${c}" style="left:${(v / d * 100).toFixed(2)}%" title="${c} ${fmt(v)}"></i>`).join(''); return t(mk.boom, 'boom') + t(mk.drop, 'drop'); };
  function html(x) {
    const d = dur(x);
    return `<div class="pvs"><button class="pvs-btn" type="button" aria-label="Play preview">▶</button><span class="pvs-cur">0:00</span>` +
      `<span class="pvs-trk"><span class="pvs-ticks">${ticksHTML(x.r, d)}</span><input class="pvs-rng" type="range" min="0" max="${d.toFixed(2)}" step="0.1" value="0" aria-label="Seek preview"></span>` +
      `<span class="pvs-dur">${fmt(d)}</span></div>`;
  }
  function paint(e, x) {
    const u = ui(e); if (!u || !u.rng) return;
    if (!(+u.rng.max > 0) && A.path === x.path && A.a && isFinite(A.a.duration) && A.a.duration > 0) {   // result carried no song_duration: take it from the audio once known
      u.rng.max = A.a.duration.toFixed(2); const dd = e.querySelector('.pvs-dur'); if (dd) dd.textContent = fmt(A.a.duration);
      const tk = e.querySelector('.pvs-ticks'); if (tk) tk.innerHTML = ticksHTML(x.r, A.a.duration);
    }
    const playing = A.path === x.path && A.a && !A.a.paused, t = A.path === x.path && A.a ? A.a.currentTime : (pos.get(x.path) || x.r.trim_start_s || 0);
    u.btn.textContent = playing ? '❚❚' : '▶'; u.btn.setAttribute('aria-label', playing ? 'Pause preview' : 'Play preview');
    if (document.activeElement !== u.rng || playing) u.rng.value = t;
    u.cur.textContent = fmt(t); e.classList.toggle('playing', !!playing);
  }
  function loop() {
    cancelAnimationFrame(A.raf); A.raf = 0;
    if (!A.a || A.a.paused || !A.x) return;
    const e = rowEl(A.path); if (e) paint(e, A.x);
    A.raf = requestAnimationFrame(loop);
  }
  function stop() {
    if (A.a) { try { A.a.pause(); } catch (e) {} pos.set(A.path, A.a.currentTime); }
    const e = A.x && rowEl(A.path), x = A.x; A.a = null; A.path = ''; A.x = null; cancelAnimationFrame(A.raf);
    if (e) paint(e, x);
    document.querySelectorAll('#mr-list .mr-row.playing').forEach(r => r.classList.remove('playing'));
  }
  function play(x) {
    if (A.path !== x.path) { const old = A.x; stop(); if (old) { const o = rowEl(old.path); if (o) paint(o, old); } }
    if (!A.a) {
      const a = new Audio(); a.preload = 'none'; a.src = '/api/audio?path=' + encodeURIComponent(x.path);   // first fetch happens here, on play
      A.a = a; A.path = x.path; A.x = x;
      a.onended = () => { pos.delete(x.path); stop(); };
      a.onerror = () => { stop(); };
    }
    const a = A.a, t = pos.get(x.path) != null ? pos.get(x.path) : (x.r.trim_start_s || 0);
    const started = () => { const e = rowEl(x.path); if (e) paint(e, x); loop(); };
    const seek = () => { try { a.currentTime = t; } catch (e) {} };
    if (a.readyState >= 1) { seek(); a.play().then(started).catch(() => {}); }
    else { a.addEventListener('loadedmetadata', seek, {once: true}); a.play().then(started).catch(() => {}); }   // preload=none: play() itself triggers the load
    if (window.pause) window.pause();   // the main player must not run over the preview
    const e = rowEl(x.path); if (e) paint(e, x);
  }
  function toggle(x) { if (!x || !x.r) return; (A.path === x.path && A.a && !A.a.paused) ? stop() : play(x); }
  function bind(e, x) {
    const u = ui(e); if (!u || !u.btn) return;
    u.btn.onclick = ev => { ev.stopPropagation(); toggle(x); };
    u.rng.onclick = u.rng.ondblclick = ev => ev.stopPropagation();
    u.rng.oninput = () => {
      const t = +u.rng.value; pos.set(x.path, t); u.cur.textContent = fmt(t);
      if (A.path === x.path && A.a) { try { A.a.currentTime = t; } catch (er) {} }
    };
    paint(e, x);
  }
  function setMarkers(path, m) { extra.set(path, m || {}); const e = rowEl(path), t = e && e.querySelector('.pvs-ticks'), r = e && e.querySelector('.pvs-rng'); if (t) t.innerHTML = ticksHTML({path}, (+r.max || 0) || (A.path === path && A.a && A.a.duration) || 0); }
  window.PVS = {html, bind, toggle, stop, setMarkers, markersOf, active: () => A.path};
})();
