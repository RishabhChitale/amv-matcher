/* U5: colour-coded changes on V1/A1 + Changes list in the Auto-sync panel.
   Colours: blue = slowed, amber = sped up, magenta = splice/arrange seam, teal = tempo change,
   ticks = nudged events, green dots = stem-target locks. Extra drawing wraps window.asDrawV1/asDrawA1. */
(function () {
  const C = { slow: '#3b82f6', fast: '#f59e0b', seam: '#e11d8f', tempo: '#22d3ee', nudge: '#e5e7eb', stem: '#22c55e' };
  const el = (id) => document.getElementById(id);
  const run = () => (window.AS && AS.curRun && AS.curRun()) || null;
  const plan = () => { const r = run(); return r && r.result && r.result.plan; };
  const synced = () => !!(window.AS && AS.mode === 'synced' && run());
  const fmt = (t) => { t = Math.max(0, t || 0); const m = Math.floor(t / 60), s = t - m * 60; return m + ':' + (s < 10 ? '0' : '') + s.toFixed(1); };

  // output time -> video time (inverse of the retime warp); identity when there is no warp or in Original mode
  function outToVid(o) {
    const p = plan(), w = synced() && p && p.retime && p.retime.warp;
    if (!w || w.length < 2) return o;
    if (o <= w[0][1]) return w[0][0] + (o - w[0][1]);
    for (let i = 1; i < w.length; i++) if (o <= w[i][1]) {
      const a = w[i - 1], b = w[i], f = b[1] > a[1] ? (o - a[1]) / (b[1] - a[1]) : 0; return a[0] + f * (b[0] - a[0]);
    }
    const l = w[w.length - 1]; return l[0] + (o - l[1]);
  }
  const arr = (x) => (Array.isArray(x) ? x : []);
  // Collect every change as {kind, v (video time), label, color, v2?}
  function items() {
    const p = plan(); if (!p) return [];
    const out = [];
    const rs = arr(p.retime && p.retime.sections);
    rs.forEach((s) => out.push({ kind: s.speed < 1 ? 'slow' : 'fast', v: s.v_start, v2: s.v_end, color: s.speed < 1 ? C.slow : C.fast,
      label: (s.speed < 1 ? 'Slowed ' : 'Sped up ') + s.speed.toFixed(2) + 'x' }));
    arr(p.segments).forEach((g, i) => { if (i === 0 && !(g.dst_start > 0.01)) return;   // first segment start is not a seam
      out.push({ kind: 'seam', v: outToVid(g.dst_start), color: C.seam, label: 'Seam ← song ' + fmt(g.src_start) }); });
    const pct = ((p.tempo_ratio || 1) - 1) * 100;
    if (Math.abs(pct) >= 0.05) out.push({ kind: 'tempo', v: 0, color: C.tempo, label: 'Tempo ' + (pct > 0 ? '+' : '') + pct.toFixed(1) + '%' });
    arr(p.nudges).forEach((n) => out.push({ kind: 'nudge', v: n.t, color: C.nudge, label: 'Nudge ' + (n.kind || 'event') + ' ' + (n.delta >= 0 ? '+' : '') + Math.round((n.delta || 0) * 1000) + ' ms' }));
    const r = run(), st = arr((r && r.result && (r.result.stem_targets || (r.result.plan && r.result.plan.stem_targets))));
    // stem_targets[].t is SOURCE-song time (before retime/splice): map it through the plan segments to output time, then back to video time
    const segs = arr(p.segments);
    const songToOut = (t) => {
      if (!segs.length) return t - (p.offset || 0);
      const g = segs.find((q) => t >= q.src_start - 1e-6 && t <= q.src_end + 1e-6);
      return g ? g.dst_start + (t - g.src_start) : null;   // outside every used part: not on the timeline
    };
    st.forEach((s) => { const t = typeof s === 'number' ? s : (s && (typeof s.t_song === 'number' ? s.t_song : s.t)); if (typeof t !== 'number') return;
      const o = songToOut(t); if (o === null || o < 0) return;
      out.push({ kind: 'stem', v: outToVid(o), color: C.stem, label: 'Stem lock (' + (s.stem || s.kind || 'stem') + ', song ' + fmt(t) + ')' }); });
    return out.sort((a, b) => a.v - b.v);
  }

  function legend(ctx, x, y) {
    const L = [['slowed', C.slow], ['sped up', C.fast], ['seam', C.seam], ['tempo', C.tempo], ['nudge', C.nudge], ['stem lock', C.stem]];
    ctx.save(); ctx.font = '10px Inter, system-ui, sans-serif'; ctx.textBaseline = 'middle';
    let w = 6; L.forEach((l) => { w += 12 + ctx.measureText(l[0]).width + 8; });
    ctx.fillStyle = 'rgba(0,0,0,.5)'; ctx.fillRect(x - w, y, w, 14);
    let cx = x - w + 5;
    L.forEach((l) => { ctx.fillStyle = l[1]; ctx.fillRect(cx, y + 3, 8, 8); ctx.fillStyle = '#ddd'; ctx.fillText(l[0], cx + 11, y + 7.5); cx += 12 + ctx.measureText(l[0]).width + 8; });
    ctx.restore();
  }

  function drawMarks(ctx, y0, h, t2x, onA1, W) {
    const live = synced(), its = items(); if (!its.length) return;
    ctx.save();
    for (const it of its) {
      if (it.kind === 'tempo') continue;   // tempo badge is drawn by autosync on A1
      const x = t2x(it.v);
      if (x < -4 || x > (W || 1e5) + 4) continue;
      if (!live) {   // Original view: faint dashed outline of what Auto-synced WOULD change
        ctx.globalAlpha = 0.55; ctx.strokeStyle = it.color; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
        if (it.kind === 'slow' || it.kind === 'fast') { if (!onA1) ctx.strokeRect(x + .5, y0 + .5, Math.max(2, t2x(it.v2) - x), h - 1); }
        else { ctx.beginPath(); ctx.moveTo(x + .5, y0); ctx.lineTo(x + .5, y0 + h); ctx.stroke(); }
        ctx.setLineDash([]); ctx.globalAlpha = 1; continue;
      }
      if (it.kind === 'seam') {
        ctx.strokeStyle = C.seam; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y0 + h); ctx.stroke();
        if (!onA1) { ctx.font = '600 10px Inter, system-ui, sans-serif'; ctx.textBaseline = 'bottom'; const tx = it.label.replace('Seam ← ', '← ');
          const tw = ctx.measureText(tx).width; ctx.fillStyle = 'rgba(0,0,0,.6)'; ctx.fillRect(x + 2, y0 + h - 14, tw + 6, 13); ctx.fillStyle = '#f9a8d4'; ctx.fillText(tx, x + 5, y0 + h - 2); }
      } else if (it.kind === 'nudge') {
        if (onA1) continue; ctx.strokeStyle = C.nudge; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x + .5, y0 + h - 8); ctx.lineTo(x + .5, y0 + h); ctx.stroke();
      } else if (it.kind === 'stem') {
        if (!onA1) continue; ctx.fillStyle = C.stem; ctx.beginPath(); ctx.arc(x, y0 + h - 7, 3.5, 0, 6.2832); ctx.fill();
        ctx.strokeStyle = 'rgba(0,0,0,.6)'; ctx.lineWidth = 1; ctx.stroke();
      }
    }
    ctx.restore();
  }

  const v1 = window.asDrawV1, a1 = window.asDrawA1;
  window.asDrawV1 = function (ctx, y0, h, t2x) {
    if (v1) v1.apply(this, arguments);
    if (!run() || !plan()) return;
    const W = ctx.canvas ? ctx.canvas.clientWidth || ctx.canvas.width : 1e5;
    drawMarks(ctx, y0, h, t2x, false, W);
    legend(ctx, W - 8, y0 + 2);
  };
  window.asDrawA1 = function (ctx, y0, h, t2x, W) {
    if (a1) a1.apply(this, arguments);
    if (!run() || !plan()) return;
    drawMarks(ctx, y0, h, t2x, true, W);
  };

  // ---- Changes list ----
  let box = null;
  function ensureBox() {
    if (box && box.isConnected) return box;
    const host = el('as-done'); if (!host) return null;
    box = document.createElement('div'); box.id = 'as-changes';
    const mode = el('as-mode'); host.insertBefore(box, mode ? mode.nextSibling : null);
    box.addEventListener('click', (e) => {
      const li = e.target.closest('li[data-v]'); if (!li) return;
      const t = parseFloat(li.dataset.v); if (isFinite(t) && typeof seek === 'function') seek(Math.max(0, t));
    });
    return box;
  }
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function render() {
    const b = ensureBox(); if (!b) return;
    const its = plan() ? items() : [];
    b.hidden = !its.length && !plan();
    if (!plan()) { b.innerHTML = ''; return; }
    const lg = [['slowed', C.slow], ['sped up', C.fast], ['seam', C.seam], ['tempo', C.tempo], ['nudge', C.nudge], ['stem lock', C.stem]]
      .map((l) => '<span><i style="background:' + l[1] + '"></i>' + l[0] + '</span>').join('');
    b.innerHTML = '<h4>Changes (' + its.length + ')</h4><div class="chg-legend">' + lg + '</div>' +
      (AS.mode === 'orig' ? '<div class="chg-mode">Original view: dashed outlines show where Auto-synced changes things.</div>' : '') +
      (its.length ? '<ul>' + its.map((it) => '<li data-v="' + it.v + '" title="Jump to ' + fmt(it.v) + '"><i style="background:' + it.color + '"></i>' + esc(it.label) + '<span class="t">' + fmt(it.v) + '</span></li>').join('') + '</ul>' : '<div class="chg-none">No changes: the original already fits.</div>');
  }
  const uf = window.updateFit;
  window.updateFit = function () { const r = uf ? uf.apply(this, arguments) : undefined; try { render(); } catch (e) { console.error(e); } return r; };
  const modeBar = el('as-mode');
  if (modeBar) modeBar.querySelectorAll('button').forEach((bt) => bt.addEventListener('click', () => setTimeout(() => { try { render(); } catch (e) { console.error(e); } }, 0)));
  try { render(); } catch (e) { /* panel not ready */ }
  window.ASChanges = { items, render };
})();
