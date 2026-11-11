/* F5: auto-sync result breakdown. Tempo / Sync / Energy(arc) / Tightness, before -> after.
   before = the scan row's natural-trim scores (S.sel.tempo_pct/sync_pct/arc_pct; tightness: final.tightness_start.tight_pct)
   after  = res.final.tempo_pct/sync_pct/arc_pct/tight_pct. Bars use the same .bar/.bar i gradient as the scan rows. */
(function () {
  const el = (id) => document.getElementById(id);
  const num = (x) => (typeof x === 'number' && isFinite(x) ? x : null);
  const run = () => (window.AS && AS.curRun && AS.curRun()) || null;

  function rows(res, sel) {
    const f = res && res.final; if (!f || res.eligible === false) return null;
    const ts = f.tightness_start || {};
    const defs = [
      ['Tempo', sel && sel.tempo_pct, f.tempo_pct, 'How well the song tempo matches the video motion tempo'],
      ['Sync', sel && sel.sync_pct, f.sync_pct, 'How closely cuts and motion peaks land on the beat'],
      ['Energy', sel && sel.arc_pct, f.arc_pct, "How well the song's energy arc follows the video's"],
      ['Tightness', ts.tight_pct, f.tight_pct, 'How tight the cut timing is to the beat (median error ' + (num(f.median_err_ms) != null ? Math.round(f.median_err_ms) + ' ms' : 'n/a') + ')'],
    ];
    const out = defs.map(([name, b, a, tip]) => ({ name, before: num(b), after: num(a), tip })).filter((r) => r.after != null);
    // Boom: shown for information only; boom_pct is NOT part of the total % (BOOM_IN_TOTAL=False).
    const bm = num(f.boom_pct);
    if (out.length && bm != null && (f.n_booms == null || f.n_booms > 0 || bm > 0))
      out.push({ name: 'Boom', before: num(sel && sel.boom_pct), after: bm, extra: 'bd-boom', note: 'not in %',
        tip: 'Boom (drop) alignment. Shown for information only: it is NOT part of the total match %' });
    return out.length ? out : null;
  }
  const fmt = (v) => (v == null ? '–' : Math.round(v) + '%');
  const clamp = (v) => Math.max(0, Math.min(100, v || 0));

  function render() {
    const r = run(), res = r && r.result, host = el('as-done'); if (!host) return;
    let box = el('as-bd');
    const data = rows(res, window.S && S.sel);
    if (!data || host.classList.contains('hidden')) { if (box) box.classList.add('hidden'); return; }
    if (!box) {
      box = document.createElement('div'); box.id = 'as-bd'; box.className = 'as-bd';
      const why = el('as-why'); why.parentNode.insertBefore(box, why.nextSibling);
    }
    box.classList.remove('hidden');
    box.innerHTML = data.map((d) => {
      const dl = d.before != null ? Math.round(d.after) - Math.round(d.before) : null;
      const cls = dl == null || dl === 0 ? '' : dl > 0 ? ' up' : ' down';
      return `<div class="bd-row${d.extra ? ' ' + d.extra : ''}" title="${d.tip}"><span class="bd-n">${d.name}</span>` +
        `<div class="bar bd-bar">${d.before != null ? `<u style="width:${clamp(d.before)}%"></u>` : ''}<i style="width:${clamp(d.after)}%"></i></div>` +
        `<span class="bd-v">${d.before != null ? fmt(d.before) + ' → ' : ''}<b>${fmt(d.after)}</b></span>` +
        `<span class="bd-d${cls}">${dl == null ? '' : (dl > 0 ? '+' : '') + dl}</span></div>` +
        (d.note ? `<div class="bd-note">Boom is not part of the % above</div>` : '');
    }).join('');
  }
  window.ASBreakdown = { rows, render };
  const _uf = window.updateFit;
  window.updateFit = function () { const r = _uf && _uf.apply(this, arguments); try { render(); } catch (e) { /* cosmetic only */ } return r; };
})();
