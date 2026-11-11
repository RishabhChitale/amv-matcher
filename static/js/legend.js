'use strict';
// F7: global marker legend (collapsible, always visible) + hover tooltips on timeline markers.
// Extension point: MKLegend.register({group, id, label, desc, color, shape}) adds a legend row;
// MKLegend.addTip(fn) adds a tooltip provider fn(ctx) -> [{label, t?, conf?}] (ctx = {x, y, t, band}).
// boom / build-up / lull markers are intentionally NOT defined here: they are registered by their owner via this API.
(function () {
  const $ = id => document.getElementById(id);
  const rows = [], tips = [];
  const SHAPES = {box: 'width:10px;height:10px', bar: 'width:3px;height:12px', tick: 'width:1px;height:12px', diamond: 'width:9px;height:9px;transform:rotate(45deg)', wedge: 'width:10px;height:6px'};
  function register(r) { if (!r || !r.id) return; const i = rows.findIndex(x => x.id === r.id); if (i >= 0) rows[i] = r; else rows.push(r); build(); }
  function addTip(fn) { if (typeof fn === 'function') tips.push(fn); }
  const fmt = t => { t = Math.max(0, t || 0); return Math.floor(t / 60) + ':' + (t % 60).toFixed(1).padStart(4, '0'); };

  // built-in rows: only things that exist today
  const BASE = [
    ['Video', 'seg-peak', 'peak section', 'High-energy section of the video (red tint).', 'rgba(255,59,78,.55)', 'box'],
    ['Video', 'seg-calm', 'calm section', 'Low-energy section of the video (cyan tint).', 'rgba(34,211,238,.55)', 'box'],
    ['Video', 'seg-mid', 'normal section', 'Everything between calm and peak (violet tint).', 'rgba(139,92,246,.55)', 'box'],
    ['Video', 'energy', 'energy curve', 'Motion energy of the video over time.', '#8b5cf6', 'box'],
    ['Video', 'cut', 'video cut', 'A shot change detected in the video.', 'rgba(255,255,255,.7)', 'tick'],
    ['Song', 'wave', 'song waveform', 'The audio on A1.', '#34d399', 'box'],
    ['Song', 'beat', 'beat', 'A detected beat of the song.', 'rgba(255,255,255,.5)', 'tick'],
    ['Song', 'drop', 'drop', 'A detected drop in the song.', '#22d3ee', 'diamond'],
    ['Song', 'cut', 'splice cut', 'Where the song is cut and crossfaded (scissors + wedge).', '#fff', 'wedge'],
    ['Song', 'pump-d', 'pump (drop)', 'Pump-lane bar at a drop; height = strength of the boom.', '#22d3ee', 'bar'],
    ['Song', 'pump-b', 'pump (beat)', 'Pump-lane bar at a beat.', '#8b5cf6', 'bar'],
    ['Overview', 'ov-in', 'used part', 'Part of the song that is used (green), the rest is grey.', '#34d399', 'box'],
    ['Overview', 'ov-seg', 'song window', 'Cyan / violet brackets: the song windows kept in the video.', '#22d3ee', 'box'],
    ['Score', 'score', 'match score', 'Ring and % show how well a song matches (violet to cyan). Grey = no score.', '#8b5cf6', 'box'],
    ['Score', 'score-nov', 'no score', 'Grey ring: not scored yet.', '#8b8f9c', 'box']];

  const root = document.createElement('div'); root.id = 'mk-legend'; root.className = 'mk-legend';
  root.innerHTML = '<button id="mk-lg-btn" type="button" aria-expanded="false" title="Legend: what every colour and marker on the timeline means">Legend</button><div id="mk-lg-body" class="hidden"></div>';
  const tip = document.createElement('div'); tip.id = 'mk-tip'; tip.className = 'mk-tip hidden';

  function changeRows() { // single source of truth: mirror yanma's #v1-legend entries (colour + tooltip) instead of redefining them
    const out = []; document.querySelectorAll('#v1-legend .lg').forEach(c => { const i = c.querySelector('i'), b = c.querySelector('b'); if (i && b) out.push({label: b.textContent, desc: c.title, color: i.style.background}); }); return out;
  }
  function row(color, shape, label, desc) {
    return '<div class="mk-row" title="' + String(desc || '').replace(/"/g, '&quot;') + '"><i style="background:' + color + ';' + (SHAPES[shape] || SHAPES.box) + '"></i><b>' + label + '</b></div>';
  }
  function build() {
    const groups = {}; const add = (g, h) => { (groups[g] = groups[g] || []).push(h); };
    BASE.forEach(r => add(r[0], row(r[4], r[5], r[2], r[3])));
    changeRows().forEach(c => add('Changes', row(c.color, 'bar', c.label, c.desc)));
    rows.forEach(r => add(r.group || 'More', row(r.color, r.shape, r.label, r.desc)));
    $('mk-lg-body').innerHTML = Object.keys(groups).map(g => '<div class="mk-g"><h4>' + g + '</h4>' + groups[g].join('') + '</div>').join('');
  }
  function fit() { const b = $('mk-lg-btn').getBoundingClientRect(); $('mk-lg-body').style.maxHeight = Math.max(80, Math.min(300, innerHeight - b.bottom - 30)) + 'px'; }
  function setOpen(o) { $('mk-lg-body').classList.toggle('hidden', !o); $('mk-lg-btn').setAttribute('aria-expanded', o ? 'true' : 'false'); root.classList.toggle('open', o); if (o) { build(); fit(); } try { localStorage.setItem('mkLegendOpen', o ? '1' : ''); } catch (e) {} }

  // tooltip providers for the markers that exist today
  function builtin(c) {
    const out = [], S_ = window.S; if (!S_ || !S_.dur) return out;
    const tol = 5, xs = t => t2x(t);
    const near = t => Math.abs(xs(t) - c.x) <= tol;
    const V = S_.video, W_ = S_.wf;
    const inV1 = c.y >= RULER && c.y <= RULER + V1H, inA1 = c.y > RULER + V1H && c.y <= RULER + V1H + A1H, inP = c.y > RULER + V1H + A1H;
    try {
      if (window.ASChanges) ASChanges.items().forEach(it => { if (it.kind === 'tempo') return; const hit = it.v2 != null ? (c.x >= xs(it.v) - tol && c.x <= xs(it.v2) + tol) : near(it.v); if (hit) out.push({label: it.label, t: it.v, conf: it.conf}); });
      if (V && inV1 && c.y <= RULER + 12) (V.cut_times || []).forEach(t => { if (near(t)) out.push({label: 'Video cut (shot change)', t}); });
      if (V && inV1 && !out.length) { const sg = (V.segments || []).find(s => c.t >= s.start && c.t < s.end); if (sg) out.push({label: (sg.label === 'peak' ? 'Peak' : sg.label === 'calm' ? 'Calm' : 'Normal') + ' section ' + fmt(sg.start) + '-' + fmt(sg.end), t: sg.start}); }
      const sp = spliceActive(), off = S_.offset || 0;
      const segs = sp ? S_.sp.plan.segments : [{src_start: off, src_end: off + S_.dur, dst_start: 0}];
      const toDst = st => { const g = segs.find(q => st >= q.src_start && st < q.src_end); return g ? g.dst_start + st - g.src_start : null; };
      if (W_ && inA1) {
        (W_.drops || []).forEach(d => { const t = toDst(d); if (t != null && near(t)) out.push({label: 'Drop', t}); });
        if (!out.length && c.y >= RULER + V1H + A1H - 20) (W_.beat_times || []).forEach(b => { const t = toDst(b); if (t != null && near(t)) out.push({label: 'Beat', t}); });
        if (sp) (S_.sp.plan.cuts || []).forEach(k => { if (near(k.dst_time)) out.push({label: 'Splice cut (crossfade)', t: k.dst_time}); });
      }
      if (inP && typeof EV !== 'undefined') EV.forEach(e => { const t = e.t - (sp ? 0 : off); if (near(t)) out.push({label: e.d ? 'Pump at a drop' : 'Pump at a beat', t, conf: e.s}); });
    } catch (e) {}
    return out;
  }
  function show(ev) {
    const cv = $('tl'); if (!cv || !window.S || !S.dur) return hide();
    const r = cv.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top, c = {x, y, t: x2t(x), band: null};
    let hits = builtin(c); tips.forEach(f => { try { hits = hits.concat(f(c) || []); } catch (e) {} });
    if (!hits.length) return hide();
    tip.innerHTML = hits.slice(0, 4).map(h => '<div><b></b><span></span></div>').join('');
    hits.slice(0, 4).forEach((h, i) => { const d = tip.children[i]; d.firstChild.textContent = h.label; d.lastChild.textContent = (h.t != null ? ' @ ' + fmt(h.t) : '') + (h.conf != null ? ' · ' + (h.conf <= 1 ? Math.round(h.conf * 100) + '%' : h.conf) : ''); });
    tip.classList.remove('hidden'); tip.style.left = Math.min(innerWidth - 230, ev.clientX + 12) + 'px'; tip.style.top = (ev.clientY + 16) + 'px';
  }
  function hide() { tip.classList.add('hidden'); }

  function init() {
    const wrap = $('tl-wrap'), cv = $('tl'); if (!wrap || !cv || $('mk-legend')) return;
    wrap.appendChild(root); document.body.appendChild(tip); build();
    $('mk-lg-btn').onclick = () => setOpen(!root.classList.contains('open'));
    cv.addEventListener('mousemove', show); cv.addEventListener('mouseleave', hide);
    let o = false; try { o = !!localStorage.getItem('mkLegendOpen'); } catch (e) {} if (o) setOpen(true);
    const place = () => { root.style.left = (cv.offsetLeft + cv.clientWidth - 6) + 'px'; root.style.top = (cv.offsetTop + 3) + 'px'; };
    place(); addEventListener('resize', () => { place(); fit(); }); setInterval(place, 800);
  }
  window.MKLegend = {register, addTip, rows: () => rows.slice()};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
