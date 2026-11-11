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
    ['Video', 'cut', 'video cut', 'A shot change detected in the video.', 'var(--mk-cut)', 'tick'],
    ['Song', 'wave', 'song waveform', 'The audio on A1.', '#34d399', 'box'],
    ['Song', 'beat', 'beat', 'A detected beat of the song (every 4th is brighter).', 'var(--mk-beat)', 'tick'],
    ['Song', 'mdrop', 'main drop', 'The song\'s main drop: taller tick labelled MAIN.', 'var(--mk-drop)', 'bar'],
    ['Song', 'drop', 'drop', 'A detected drop.', 'var(--mk-drop)', 'tick'],
    ['Song', 'initial', 'initial boom', 'Where the groove / beat first enters.', 'var(--mk-initial)', 'tick'],
    ['Song', 'encore', 'encore', 'A drop again, may have no build-up.', 'var(--mk-encore)', 'tick'],
    ['Video', 'visual', 'visual boom', 'A visual impact: motion burst, flash, zoom punch or whip pan.', 'var(--mk-visual)', 'tick'],
    ['Video', 'camera', 'camera whip', 'A fast camera whip (span from start to end).', 'var(--mk-camera)', 'bar'],
    ['Video', 'peak', 'high energy', 'A stretch of very high motion energy.', 'var(--mk-peak)', 'box'],
    ['Song', 'cut', 'splice cut', 'Where the song is cut and crossfaded (scissors + wedge).', '#fff', 'wedge'],
    ['Song', 'pump-d', 'pump (drop)', 'Pump-lane bar at a drop; height = strength of the boom.', 'var(--mk-drop)', 'bar'],
    ['Song', 'pump-b', 'pump (beat)', 'Pump-lane bar at a beat.', '#8b5cf6', 'bar'],
    ['Overview', 'ov-in', 'used part', 'Part of the song that is used (green), the rest is grey.', '#34d399', 'box'],
    ['Overview', 'ov-seg', 'song window', 'Cyan / violet brackets: the song windows kept in the video.', '#22d3ee', 'box'],
    ['Bookmarks', 'bm', 'your bookmarks', 'Filled shapes you add yourself (Mark mode, key B); the bookmark bar lists the types.', '#e5e7eb', 'diamond'],
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
        if (!out.length && !D.booms.length && c.y >= RULER + V1H + A1H - 20) (W_.beat_times || []).forEach(b => { const t = toDst(b); if (t != null && near(t)) out.push({label: 'Beat', t}); });
        if (sp) (S_.sp.plan.cuts || []).forEach(k => { if (near(k.dst_time)) out.push({label: 'Splice cut (crossfade)', t: k.dst_time}); });
      }
      if (inP && typeof EV !== 'undefined') EV.forEach(e => { const t = e.t - (sp ? 0 : off); if (near(t)) out.push({label: e.d ? 'Pump at a drop' : 'Pump at a beat', t, conf: e.s}); });
    } catch (e) {}
    return out;
  }

  // ---- detected markers from the server timeline log (docs/MARKERS.md) ----
  const D = {key: '', busy: false, booms: [], vbooms: [], cams: [], lulls: [], peaks: [], main: null};
  const mmss = t => { t = Math.max(0, t || 0); const m = Math.floor(t / 60); return m + ':' + (t - m * 60).toFixed(3).padStart(6, '0'); };
  const span = (a, b) => { const f = x => { x = Math.max(0, x || 0); return Math.floor(x / 60) + ':' + (x % 60).toFixed(1).padStart(4, '0'); }; return f(a) + '-' + f(b); };
  const KIND = {motion: 'motion burst', flash: 'flash', zoom_punch: 'zoom punch', whip_pan: 'whip pan'};
  const kindText = k => String(k || '').split('+').map(x => KIND[x] || x.replace(/_/g, ' ')).join(' + ');
  const pct = c => 'conf ' + Math.round((c || 0) * 100) + '%';
  const mapA = () => { const S_ = window.S, sp = typeof spliceActive === 'function' && spliceActive(); const off = sp ? 0 : (S_.offset || 0); return t => t - off; };  // song_t -> timeline_t (spliced: already output time)
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  async function refresh() {
    const S_ = window.S; if (!S_ || !S_.dur || !S_.videoPath || D.busy) return;
    const key = [S_.videoPath, S_.sel && S_.sel.path, S_.sel && S_.sel.source].join('|'); if (key === D.key) return;
    D.busy = true; D.key = key;
    try {
      const data = window.PROJECT_UI && PROJECT_UI.collect ? await PROJECT_UI.collect() : null; if (!data) throw 0;
      const r = await fetch('/api/timeline-log', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({data, format: 'json'})});
      const L = await r.json(), vd = (L.video || {}).detected || {}, ad = (L.audio || {}).detected || {};
      D.booms = (ad.booms || []).filter(b => b.class !== 'boom'); D.main = ad.main_boom != null ? ad.main_boom : null;
      D.vbooms = vd.visual_booms || []; D.cams = (vd.camera_peaks || []).filter(c => c.kind === 'whip_pan' || c.kind === 'whip');
    } catch (e) { D.booms = []; D.vbooms = []; D.cams = []; D.main = null; D.key = ''; }
    D.busy = false; if (typeof draw === 'function') draw();
  }
  function lullsPeaks() { const S_ = window.S, V = S_.video, W_ = S_.wf; D.lulls = ((W_ && W_.lulls) || []); D.peaks = ((V && V.peak_segments) || []); }
  const isMain = b => b.main || (D.main != null && Math.abs(b.t - D.main) < 0.01);
  const bcol = b => b.class === 'initial_boom' ? '--mk-initial' : b.class === 'encore' ? '--mk-encore' : '--mk-drop';
  window.mkDraw = function (ctx, RULER_, V1H_, A1H_, t2x_, W) {
    const S_ = window.S; if (!S_ || !S_.dur) return; lullsPeaks();
    const toT = mapA(), a0 = RULER_ + V1H_ + 4, a1 = a0 + A1H_ - 8, v0 = RULER_;
    ctx.save();
    if (S_.wf) {
      ctx.fillStyle = css('--mk-lull') || 'rgba(139,92,246,.12)';
      D.lulls.forEach(l => { const s = Array.isArray(l) ? l[0] : l.start, e = Array.isArray(l) ? l[1] : l.end; const x0 = t2x_(toT(s)), x1 = t2x_(toT(e)); if (x1 > 0 && x0 < W) ctx.fillRect(x0, a0, x1 - x0, A1H_ - 8); });
      D.booms.forEach(b => { const x = t2x_(toT(b.t)); if (x < 0 || x > W) return; const m = isMain(b);
        ctx.strokeStyle = css(bcol(b)); ctx.lineWidth = m ? 2 : 1; ctx.beginPath(); ctx.moveTo(x, m ? a0 : a0 + 4); ctx.lineTo(x, a1); ctx.stroke();
        if (m) { ctx.fillStyle = css('--mk-drop'); ctx.font = '9px sans-serif'; ctx.fillText('MAIN', x + 3, a0 + 9); } });
    }
    ctx.fillStyle = css('--mk-peak') || 'rgba(34,211,238,.12)';
    D.peaks.forEach(p => { const s = Array.isArray(p) ? p[0] : p.start, e = Array.isArray(p) ? p[1] : p.end; const x0 = t2x_(s), x1 = t2x_(e); if (x1 > 0 && x0 < W) ctx.fillRect(x0, v0, x1 - x0, V1H_); });
    ctx.strokeStyle = css('--mk-visual'); ctx.lineWidth = 1; D.vbooms.forEach(b => { const x = t2x_(b.t); if (x >= 0 && x <= W) { ctx.beginPath(); ctx.moveTo(x, v0 + V1H_ - 14); ctx.lineTo(x, v0 + V1H_); ctx.stroke(); } });
    ctx.strokeStyle = css('--mk-camera'); ctx.lineWidth = 3; D.cams.forEach(c => { const x0 = t2x_(c.start), x1 = Math.max(x0 + 3, t2x_(c.end)); ctx.beginPath(); ctx.moveTo(x0, v0 + V1H_ - 3); ctx.lineTo(x1, v0 + V1H_ - 3); ctx.stroke(); });
    ctx.restore();
  };
  addTip(c => {
    const out = [], S_ = window.S, tol = 5, near = t => Math.abs(t2x(t) - c.x) <= tol;
    const inV1 = c.y >= RULER && c.y <= RULER + V1H, inA1 = c.y > RULER + V1H && c.y <= RULER + V1H + A1H;
    if (inA1) { const toT = mapA(); D.booms.forEach(b => { const t = toT(b.t); if (!near(t)) return; const nm = isMain(b) ? 'Main drop' : b.class === 'initial_boom' ? 'Initial boom' : b.class === 'encore' ? 'Encore' : 'Drop'; out.push({text: nm + ' · ' + mmss(t) + ' · ' + pct(b.conf)}); });
      D.lulls.forEach(l => { const s = toT(Array.isArray(l) ? l[0] : l.start), e = toT(Array.isArray(l) ? l[1] : l.end); if (c.t >= s && c.t <= e && !out.length) out.push({text: 'Quiet stretch · ' + span(s, e)}); }); }
    if (inV1) { D.vbooms.forEach(b => { if (near(b.t)) out.push({text: 'Visual boom · ' + mmss(b.t) + ' · ' + kindText(b.kind) + ' · ' + pct(b.conf)}); });
      D.cams.forEach(k => { if (c.x >= t2x(k.start) - tol && c.x <= t2x(k.end) + tol) out.push({text: 'Camera whip · ' + span(k.start, k.end) + (k.speed != null ? ' · speed ' + (+k.speed).toFixed(2) : '')}); });
      if (!out.length) D.peaks.forEach(p => { const s = Array.isArray(p) ? p[0] : p.start, e = Array.isArray(p) ? p[1] : p.end; if (c.t >= s && c.t <= e && !out.length) out.push({text: 'High energy · ' + span(s, e)}); }); }
    return out;
  });
  function show(ev) {
    const cv = $('tl'); if (!cv || !window.S || !S.dur) return hide();
    const r = cv.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top, c = {x, y, t: x2t(x), band: null};
    let hits = builtin(c); tips.forEach(f => { try { hits = hits.concat(f(c) || []); } catch (e) {} });
    if (!hits.length) return hide();
    tip.innerHTML = hits.slice(0, 4).map(h => '<div><b></b><span></span></div>').join('');
    hits.slice(0, 4).forEach((h, i) => { const d = tip.children[i]; if (h.text) { d.firstChild.textContent = h.text; return; } d.firstChild.textContent = h.label; d.lastChild.textContent = (h.t != null ? ' @ ' + fmt(h.t) : '') + (h.conf != null ? ' · ' + (h.conf <= 1 ? Math.round(h.conf * 100) + '%' : h.conf) : ''); });
    tip.classList.remove('hidden'); const tw = tip.offsetWidth || 230, th = tip.offsetHeight || 40; tip.style.left = Math.max(4, Math.min(innerWidth - tw - 4, ev.clientX + 12)) + 'px'; tip.style.top = Math.max(4, Math.min(innerHeight - th - 4, ev.clientY + 16)) + 'px';
  }
  function hide() { tip.classList.add('hidden'); }

  function init() {
    const wrap = $('tl-wrap'), cv = $('tl'); if (!wrap || !cv || $('mk-legend')) return;
    wrap.appendChild(root); document.body.appendChild(tip); build();
    $('mk-lg-btn').onclick = () => setOpen(!root.classList.contains('open'));
    cv.addEventListener('mousemove', show); cv.addEventListener('mouseleave', hide);
    let o = false; try { o = !!localStorage.getItem('mkLegendOpen'); } catch (e) {} if (o) setOpen(true);
    const place = () => { root.style.left = (cv.offsetLeft + cv.clientWidth - 6) + 'px'; root.style.top = (cv.offsetTop + 3) + 'px'; };
    place(); addEventListener('resize', () => { place(); fit(); }); setInterval(() => { place(); refresh(); }, 800);
  }
  window.MKLegend = {register, addTip, rows: () => rows.slice(), detected: D};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
