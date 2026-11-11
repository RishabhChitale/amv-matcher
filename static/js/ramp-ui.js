/* U11d: read-only retime-curve lane under the timeline + two-score comparison card.
   Data (shape per docs/U11C-RAMP-FORMAT.md; adapt pick() when rufflet's final JSON lands):
     plan.retime_ramps = {warp:[[src,out]...], points:[{t_src,speed,...}], curve:[[src,out,speed]...@4Hz], out_duration}
     plan.retime       = step plan (sections [{v_start,v_end,speed}], warp)
     final.ramps       = {kept, total_step, total_after, tight_*, sync_*, smooth_step, smooth_ramp}
   Speed = source s per output s (>1 = faster = amber, <1 = slower = blue). Lane x-axis = video time like V1. */
(function () {
  const COL = { slow: '#3b82f6', fast: '#f59e0b', mid: 'rgba(255,255,255,.35)' };
  const el = (id) => document.getElementById(id);
  const arr = (x) => (Array.isArray(x) ? x : []);
  const run = () => (window.AS && AS.curRun && AS.curRun()) || null;

  // -> [{v, speed}] sorted by video time, or null. Prefers the ramp plan, else the step plan's sections.
  function curve(res) {
    const p = res && res.plan; if (!p) return null;
    const rr = p.retime_ramps;
    if (rr && arr(rr.curve).length > 1) return rr.curve.map((c) => ({ v: c[0], speed: c[2] }));
    if (rr && arr(rr.warp).length > 1) {
      const w = rr.warp, out = [];
      for (let i = 1; i < w.length; i++) { const dv = w[i][0] - w[i - 1][0], dt = w[i][1] - w[i - 1][1]; if (dt > 0) out.push({ v: w[i - 1][0], speed: dv / dt }); }
      return out.length ? out : null;
    }
    const rs = arr(p.retime && p.retime.sections);
    if (!rs.length) return null;
    const out = [];
    rs.forEach((s) => { out.push({ v: s.v_start, speed: s.speed }); out.push({ v: s.v_end, speed: s.speed }); });
    return out;
  }
  // song tempo curve (temporamp.py shape: same warp/points/curve with speed = song s per output s), if present
  function tempoCurve(res) {
    const t = res && res.plan && (res.plan.tempo_ramps || res.plan.tempo_curve); if (!t) return null;
    const c = arr(t.curve); return c.length > 1 ? c.map((q) => ({ v: q[1], speed: q[2] })) : null;
  }
  const colorFor = (s) => (s < 0.995 ? COL.slow : s > 1.005 ? COL.fast : COL.mid);

  function drawLane(cv, pts, dur, label, range) {
    const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1, W = r.width, H = r.height;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    const D = dur || (pts.length ? pts[pts.length - 1].v : 1) || 1, x = (t) => t / D * W;
    const dev = range || Math.max(0.1, ...pts.map((p) => Math.abs(p.speed - 1))) * 1.15, mid = H / 2;
    const y = (s) => mid - (s - 1) / dev * (H / 2 - 4);
    ctx.strokeStyle = 'rgba(255,255,255,.18)'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(W, mid); ctx.stroke(); ctx.setLineDash([]);
    ctx.lineWidth = 2;
    for (let i = 1; i < pts.length; i++) {   // colour each piece by its speed (blue slower, amber faster)
      ctx.strokeStyle = colorFor((pts[i].speed + pts[i - 1].speed) / 2);
      ctx.beginPath(); ctx.moveTo(x(pts[i - 1].v), y(pts[i - 1].speed)); ctx.lineTo(x(pts[i].v), y(pts[i].speed)); ctx.stroke();
    }
    ctx.fillStyle = '#8b8f9c'; ctx.font = '10px Inter,sans-serif'; ctx.textBaseline = 'top';
    ctx.fillText(label, 4, 2);
    const hi = Math.max(...pts.map((p) => p.speed)), lo = Math.min(...pts.map((p) => p.speed));
    ctx.textAlign = 'right'; ctx.fillText(Math.round(lo * 100) + '% – ' + Math.round(hi * 100) + '%', W - 4, 2);
  }

  function renderLane() {
    const box = el('rl-wrap'); if (!box) return;
    const r = run(), res = r && r.result, c = curve(res), tc = tempoCurve(res);
    const on = !!(window.AS && AS.mode === 'synced' && (c || tc));
    box.classList.toggle('hidden', !on); if (!on) return;
    const dur = (window.S && S.dur) || 0;
    const v = el('rl-v'), a = el('rl-a');
    v.classList.toggle('hidden', !c); a.classList.toggle('hidden', !tc);
    if (c) drawLane(v, c, dur, 'Video speed (slower = blue, faster = amber)');
    if (tc) drawLane(a, tc, 0, 'Song tempo');
  }

  // ---- comparison card: smooth vs best (step) ----
  const pct = (x) => (typeof x === 'number' ? Math.round(x * 10) / 10 + '' : '–');
  function cardRows(f) {
    const q = f && f.ramps; if (!q) return null;
    const row = (name, a, b, hib) => ({ name, a, b, better: typeof a === 'number' && typeof b === 'number' ? (hib ? (b > a ? 'b' : b < a ? 'a' : '') : (b < a ? 'b' : b > a ? 'a' : '')) : '' });
    return [row('Total', q.total_step, q.total_after, true), row('Sync', q.sync_step, q.sync_after, true), row('Cut timing', q.tight_step, q.tight_after, true),
      row('Arc', q.arc_step, q.arc_after, true), row('Max jump', q.max_jump_step != null ? q.max_jump_step : q.smooth_step, q.max_jump_ramp != null ? q.max_jump_ramp : q.smooth_ramp, false)]
      .filter((r) => r.a != null || r.b != null);
  }
  function renderCard() {
    const host = el('as-done'); if (!host) return;
    let box = el('as-cmp');
    const r = run(), rows = r && r.result && cardRows(r.result.final);
    if (!rows || !rows.length) { if (box) box.remove(); return; }
    if (!box) { box = document.createElement('div'); box.id = 'as-cmp'; box.className = 'as-cmp'; const m = el('as-mode'); host.insertBefore(box, m ? m.nextSibling : null); }
    box.innerHTML = '<h4>Smooth vs best</h4><table><tr><th></th><th>Best (steps)</th><th>Smooth</th></tr>' +
      rows.map((q) => '<tr><td>' + q.name + '</td><td' + (q.better === 'a' ? ' class="win"' : '') + '>' + pct(q.a) + '</td><td' + (q.better === 'b' ? ' class="win"' : '') + '>' + pct(q.b) + '</td></tr>').join('') + '</table>';
  }

  function render() { try { renderLane(); renderCard(); } catch (e) { console.error(e); } }
  const uf = window.updateFit;
  window.updateFit = function () { const r = uf ? uf.apply(this, arguments) : undefined; render(); return r; };
  const mb = el('as-mode'); if (mb) mb.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => setTimeout(render, 0)));
  window.addEventListener('resize', render);
  render();
  window.ASRamp = { curve, tempoCurve, cardRows, render };
})();
