'use strict';
// v0.9.7: auto-sync alternative plans. When the API returns result.alternatives, the result card lists the chosen plan and
// the alternative side by side and lets the user switch. A switch swaps the run's result (plan/final/path) in place, so the
// player preview, the Original/Auto-synced toggle, the timeline and render/export all follow the chosen plan unchanged.
(function () {
  const $ = id => document.getElementById(id);
  const CM = "Match the video's own edit", AUTO = 'Best-score plan';
  const box = $('as-alts'); if (!box || typeof AS === 'undefined' || typeof window.asUi !== 'function') return;

  const mainId = res => (res.path === 'content_match' || (res.plan && res.plan.content_matched)) ? 'content_match' : 'best_score';
  function state(run) {   // lazily snapshot the API's chosen plan + alternatives into entries[]
    const res = run && run.result; if (!res || res.eligible === false || !(res.alternatives && res.alternatives.length)) return null;
    if (!run.alt) {
      const id = mainId(res);
      run.alt = {id, why: res.why_not_100 || '', entries: [{id, label: id === 'content_match' ? CM : AUTO, plan: res.plan, final: res.final, path: res.path, audioUrl: run.audioUrl || '', head: res}]
        .concat(res.alternatives.map(a => ({id: a.id, label: a.label || (a.id === 'content_match' ? CM : AUTO), plan: a.plan, final: a.final, path: a.path, audioUrl: '', head: null})))};
    }
    return run.alt;
  }
  const pct = v => v == null ? '-' : Math.round(v) + '%';
  function row(e, on) {
    const f = e.final || {};
    return `<div class="alt${on ? ' on' : ''}" data-id="${e.id}"><div class="alt-top"><b class="alt-name">${e.label}</b><span class="alt-total">Total ${pct(f.total_pct)}</span></div>`
      + `<div class="alt-sub">Sync ${pct(f.sync_pct)} · Tight ${pct(f.tight_pct)} · Energy ${pct(f.arc_pct)} · Tempo ${pct(f.tempo_pct)}${f.median_err_ms != null ? ' · median error ' + Math.round(f.median_err_ms) + ' ms' : ''}${f.starts_on ? ' · starts on ' + f.starts_on : ''}${f.drops_total > 0 ? ' · drops matched ' + (f.drops_matched || 0) + '/' + f.drops_total : ''}</div>`
      + (on && e.err ? '<div class="alt-err">Preview audio unavailable for this plan; export is unaffected.</div>' : '')
      + (on ? '<span class="alt-badge">In use</span>' : '<button class="alt-use" data-id="' + e.id + '">Use this plan</button>') + '</div>';
  }
  const dropsTxt = f => f && f.drops_total != null ? 'Drops matched ' + (f.drops_matched || 0) + '/' + f.drops_total : '';
  const f2 = (v, d) => typeof v === 'number' && isFinite(v) ? v.toFixed(d == null ? 2 : d) : null;
  function trajTxt(f) {   // F22: information only, never a score
    const t = f && f.trajectory; if (!t) return '';
    const p = [f2(t.follow) != null ? 'follow r ' + f2(t.follow) : null, f2(t.dyn_video) != null ? 'video range ' + f2(t.dyn_video) : null, f2(t.dyn_song) != null ? 'song range ' + f2(t.dyn_song) : null,
      f2(t.lag_s, 1) != null ? 'lag ' + f2(t.lag_s, 1) + ' s' : null, (t.ramps || []).length ? 'builds into booms ' + t.ramps.filter(r => r.match).length + '/' + t.ramps.length : null].filter(Boolean);
    return p.length ? 'Intensity: ' + p.join(' · ') : '';
  }
  const startsTxt = f => f && f.starts_on ? 'Starts on: ' + f.starts_on : '';
  function render() {
    const run = AS.curRun && AS.curRun(), st = state(run);
    const so = $('as-starts'), sf = run && run.result && run.result.eligible !== false && !AS.running ? run.result.final : null;
    const dr = $('as-drops'), dt = dropsTxt(sf); if (dr) { dr.textContent = dt; dr.classList.toggle('hidden', !dt || !(sf.drops_total > 0)); }
    const tj = $('as-traj'), tt = trajTxt(sf); if (tj) { $('as-traj-txt').textContent = tt; tj.classList.toggle('hidden', !tt); const cb = $('as-traj-on'); if (cb && !cb._f22) { cb._f22 = 1; cb.checked = !(window.MKLegend && !window.MKLegend.trajOn()); cb.onchange = () => { try { localStorage.setItem('amv-traj-off', cb.checked ? '' : '1'); } catch (e) {} if (typeof draw === 'function') draw(); }; } }
    if (so) { so.textContent = startsTxt(sf); so.classList.toggle('hidden', !startsTxt(sf)); }
    box.classList.toggle('hidden', !st || !!AS.running);
    if (!st) { box.innerHTML = ''; return; }
    box.innerHTML = st.entries.map(e => row(e, e.id === st.id)).join('');
    if (st.id !== st.entries[0].id) { $('as-why').textContent = ''; }
  }
  async function choose(id) {
    const run = AS.curRun(), st = state(run); if (!st || st.id === id || AS.running) return;
    const e = st.entries.find(x => x.id === id); if (!e) return;
    const was = !aud.paused; pause();
    const cur = st.entries.find(x => x.id === st.id); cur.audioUrl = run.audioUrl;   // keep the rendered audio per plan
    run.result = Object.assign({}, run.result, {plan: e.plan, final: e.final, path: e.path, why_not_100: e === st.entries[0] ? st.why : ''});
    e.err = false;
    if (!e.audioUrl) {
      const segs = (AS.previewSegs(e.plan) || []).map(g => g.src_start < 0 ? Object.assign({}, g, {src_start: 0, dst_start: (g.dst_start || 0) - g.src_start}) : g), tempo = (e.plan && e.plan.tempo_ratio) || 1;
      try { e.audioUrl = segs && segs.length ? (await renderAudio(S.sel.path, segs, tempo)).url : ''; } catch (x) { e.audioUrl = ''; e.err = true; console.warn('alt-plans: preview audio render failed', x); if (typeof note === 'function') note('Preview audio for this plan could not be rendered; the export is unaffected.', true); }
    }
    run.audioUrl = e.audioUrl; st.id = id;
    try { if (S.inUse && S.inUse.path === S.sel.path) S.inUse.auto_sync_cached = e.final; } catch (x) {}
    AS.mode = 'synced'; applyAfterSplice(); if (typeof draw === 'function') draw(); window.asUi(); if (window.asAfterResults) window.asAfterResults(); if (was) play();
  }
  box.addEventListener('click', ev => { const b = ev.target.closest('button.alt-use'); if (b) choose(b.dataset.id); });
  window.asAfterUi = render; AS.chooseAlt = choose; render();
})();
