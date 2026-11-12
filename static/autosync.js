// Auto-sync panel, retimed timeline visuals, ranking by auto-sync, video render + Resolve export controls.
(() => {
const el = id => document.getElementById(id);
const pk = (v, s) => v + '|' + s;
const LS = 'amv.guard.v1';
const AS = window.AS = {
  runs: {},            // pairKey -> {result, audioUrl}
  mode: 'synced', running: null, cancelled: false, guard: {lead: 60, dev: 15, tempo: 4, cuts: 2, loop: false, unlock: false, energyFirst: true, ramps: false},
  sort: 'now', all: null, lastReq: null, bands: 0, quality: 'blend', rife: null, rd: null, estSig: '', estTok: 0,
};
AS.finalSteps = res => finalSteps(res);
try { Object.assign(AS.guard, JSON.parse(localStorage.getItem(LS) || '{}')); } catch (e) {}
const saveGuard = () => { try { localStorage.setItem(LS, JSON.stringify(AS.guard)); } catch (e) {} };
const sleep = ms => new Promise(z => setTimeout(z, ms));
const post = (u, b) => jpost(u, b || {});
const STEPS = ['offset', 'tempo', 'boom', 'splice', 'arrange', 'retime', 'micro'];
const STEPNAME = {offset: 'Start point', tempo: 'Tempo match', boom: 'Boom match', splice: 'Splice', arrange: 'Arrange sections', retime: 'Retime video', micro: 'Micro-nudges'};

// ---------- current pair / plan ----------
const curRun = () => (S.videoPath && S.sel) ? AS.runs[pk(S.videoPath, S.sel.path)] : null;
AS.curRun = () => curRun();
const synced = () => AS.mode === 'synced' && !!curRun();
// The warp the export renders (render._clip_warp): a valid smooth-ramp plan (retime_ramps) wins, else the step plan.
function planWarp(p) {
  if (!p) return null;
  for (const k of ['retime_ramps', 'retime']) {
    const w = p[k] && p[k].warp;
    if (!Array.isArray(w) || w.length < 2 || !w.every(r => Array.isArray(r) && r.length === 2 && isFinite(r[0]) && isFinite(r[1]))) continue;
    if (Math.abs(w[0][0]) > 1e-6 || Math.abs(w[0][1]) > 1e-6) continue;
    let ok = true; for (let i = 1; i < w.length; i++) if (w[i][0] - w[i - 1][0] < -1e-9 || w[i][1] - w[i - 1][1] < -1e-9) { ok = false; break; }
    if (ok && w[w.length - 1][1] > 0) return w;
  }
  return null;
}
AS.planWarp = planWarp;
function warp(v) {   // video time -> output time
  const r = curRun(), w = r && planWarp(r.result.plan);
  if (!w || w.length < 2) return v;
  if (v <= w[0][0]) return w[0][1] + (v - w[0][0]);
  for (let i = 1; i < w.length; i++) if (v <= w[i][0]) {
    const a = w[i - 1], b = w[i], f = b[0] > a[0] ? (v - a[0]) / (b[0] - a[0]) : 0; return a[1] + f * (b[1] - a[1]);
  }
  const l = w[w.length - 1]; return l[1] + (v - l[0]);
}
const sections = () => { const r = curRun(), t = r && r.result.plan && r.result.plan.retime; return (t && t.sections) || []; };
window.asSongT = () => {
  if (!synced()) return null;
  const r = curRun(), out = warp(vid.currentTime);
  return r.audioUrl ? out : out + (r.result.plan.offset || 0);
};
window.asAudioUrl = () => {
  if (!synced()) return null;
  const r = curRun(); return r.audioUrl || '/api/audio?path=' + encodeURIComponent(S.sel.path);
};
// Video speed = the exact local warp slope (video s per output s), never the merged/rounded retime sections: section
// speeds are display bands (merged within 0.02, short ones absorbed, |sp-1|<=0.01 dropped) and following them made the
// output clock drift up to ~0.4 s from the audio, which then got rate-nudged and seeked (off-beat, warbly preview, F18).
function warpSpeed(v) {
  const r = curRun(), w = r && planWarp(r.result.plan);
  if (!w || w.length < 2 || v <= w[0][0] || v >= w[w.length - 1][0]) return 1;
  let lo = 0, hi = w.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (w[m][0] <= v) lo = m; else hi = m; }
  const dv = w[hi][0] - w[lo][0], dout = w[hi][1] - w[lo][1];
  return dv > 0 && dout > 1e-6 ? dv / dout : 1;
}
AS.warpSpeed = warpSpeed;
// The audio element is the master clock (rate stays at S.rate, so it never warbles). The muted video follows it: small
// drift is trimmed on the VIDEO rate (inaudible), proportional and capped at +-5%; big drift is still a rare audio seek.
const VTRIM = {gain: 0.5, cap: 0.05, dead: 0.01};
AS.vtrim = 1;
function videoTrim() {
  if (!S.sel || aud.paused || aud.seeking || aud.readyState < 2 || !window.songTime) return 1;
  const st = window.songTime();
  if (!(st >= 0) || st > (aud.duration || 1e9)) return 1;
  const d = aud.currentTime - st;   // > 0: audio ahead, so the video speeds up
  if (Math.abs(d) < VTRIM.dead || Math.abs(d) > 0.3) return 1;
  return 1 + Math.max(-VTRIM.cap, Math.min(VTRIM.cap, d * VTRIM.gain));
}
// Preview audio segments: the plan's splice, or (tempo plan without one) the single trim build_audio renders, so a tempo
// plan always previews from a rendered, tempo'd file instead of the raw song at 1.0x (F18).
function previewSegs(p) {
  if (!p) return null;
  if (p.segments && p.segments.length) return p.segments;
  if (!p.tempo_ratio || Math.abs(p.tempo_ratio - 1) < 1e-6) return null;
  const off = p.offset || 0, lead = Math.max(0, -off), vd = S.dur || (S.video && S.video.duration) || 0;
  return vd > lead ? [{src_start: Math.max(0, off), src_end: Math.max(0, off) + vd - lead, dst_start: lead}] : null;
}
AS.previewSegs = previewSegs;
window.asFrame = () => {
  if (!S.playing) return;
  const sp = synced() ? warpSpeed(vid.currentTime) : 1;
  AS.vtrim = videoTrim();
  const want = Math.max(0.0625, Math.min(16, sp * (S.rate || 1) * AS.vtrim));
  if (Math.abs(vid.playbackRate - want) > 2e-3) vid.playbackRate = want;
};
window.asDrawV1 = (ctx, y0, h, t2x) => {
  AS.bands = 0; if (!synced()) return;
  const secs = sections(); AS.bands = secs.length;
  ctx.save(); ctx.font = '600 11px Inter, system-ui, sans-serif'; ctx.textBaseline = 'top';
  for (const s of secs) {
    const x1 = t2x(s.v_start), x2 = t2x(s.v_end), slow = s.speed < 1;
    ctx.fillStyle = slow ? 'rgba(59,130,246,.30)' : 'rgba(245,158,11,.32)'; ctx.fillRect(x1, y0, Math.max(2, x2 - x1), h);
    ctx.fillStyle = slow ? '#93c5fd' : '#fcd34d'; ctx.fillRect(x1, y0, Math.max(2, x2 - x1), 2);
    if (x2 - x1 > 34) { ctx.fillStyle = '#fff'; ctx.fillText(s.speed.toFixed(2) + 'x', x1 + 4, y0 + 5); }
  }
  const w = (curRun().result.plan.retime || {}).warp || [];   // thin warp curve: how far output runs ahead/behind the source
  if (w.length > 1) {
    const dev = w.map(p => p[1] - p[0]), m = Math.max(0.2, ...dev.map(Math.abs)), mid = y0 + h - 18;
    ctx.beginPath(); w.forEach((p, i) => { const x = t2x(p[0]), y = mid - dev[i] / m * 12; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = 'rgba(255,255,255,.75)'; ctx.lineWidth = 1; ctx.stroke();
  }
  ctx.restore();
};
window.asDrawA1 = (ctx, y0, h, t2x, W) => {
  if (!synced()) return;
  const p = curRun().result.plan, pct = ((p.tempo_ratio || 1) - 1) * 100;
  if (Math.abs(pct) < 0.05) return;
  ctx.save(); ctx.font = '600 11px Inter, system-ui, sans-serif'; ctx.textAlign = 'right'; ctx.textBaseline = 'top';
  const t = (pct > 0 ? '+' : '') + pct.toFixed(1) + '% tempo', tw = ctx.measureText(t).width;
  ctx.fillStyle = 'rgba(0,0,0,.55)'; ctx.fillRect(W - tw - 18, y0 + 4, tw + 12, 16);
  ctx.fillStyle = '#22d3ee'; ctx.fillText(t, W - 12, y0 + 6); ctx.restore();
};

// ---------- guardrails ----------
function guardUI() {
  const g = AS.guard;
  el('g-lead').value = g.lead; el('g-dev').value = g.dev; el('g-tempo').value = g.tempo; el('g-cuts').value = g.cuts; el('g-loop').checked = !!g.loop; el('g-unlock').checked = !!g.unlock; el('g-energy').checked = !!g.energyFirst; el('g-ramps').checked = !!g.ramps;
  el('g-lead-v').textContent = g.lead + ' ms'; el('g-dev-v').innerHTML = '&plusmn;' + g.dev + '%'; el('g-tempo-v').textContent = g.tempo + '%'; el('g-cuts-v').textContent = g.cuts;
}
for (const [id, k] of [['g-lead', 'lead'], ['g-dev', 'dev'], ['g-tempo', 'tempo'], ['g-cuts', 'cuts']])
  el(id).oninput = () => { AS.guard[k] = +el(id).value; guardUI(); saveGuard(); };
el('g-loop').onchange = () => { AS.guard.loop = el('g-loop').checked; saveGuard(); };
el('g-energy').onchange = () => { AS.guard.energyFirst = el('g-energy').checked; saveGuard(); };
el('g-ramps').onchange = () => { AS.guard.ramps = el('g-ramps').checked; saveGuard(); };
el('g-unlock').onchange = () => { AS.guard.unlock = el('g-unlock').checked; saveGuard(); ui(); };

// ---------- running ----------
function setRing(p) { el('as-ring').style.background = `conic-gradient(var(--c) ${p * 3.6}deg, rgba(255,255,255,.12) 0)`; el('as-ring').textContent = Math.round(p) + '%'; }
function stepIdx(stage) { const s = String(stage || '').toLowerCase(); for (let i = STEPS.length - 1; i >= 0; i--) if (s.includes(STEPS[i])) return i; return -1; }
// A tool can run in several passes: its row is 'kept' if ANY of its steps was kept (showing the KEPT
// step's before -> after); a not-kept re-try flagged params.retained is 'kept earlier', never 'skipped'.
function stepRow(n, list, extra) {
  const ks = list.filter(e => e.kept), last = list[list.length - 1];
  if (!ks.length && last.params && last.params.not_in_path) {   // tried only in another order: dim, never 'skipped'
    const t = last.params.total_in_that_path;
    return `<li class="tried"><b>${STEPNAME[n]}</b><span class="muted">tried, not in final path${t != null ? ' (total ' + Math.round(t) + '% in that path)' : ''}</span></li>`;
  }
  const s = ks.length ? ks[ks.length - 1] : last;
  const a = Math.round(s.score_before), b = Math.round(s.score_after);
  const retained = ks.length && last !== s && last.params && last.params.retained;
  const lbl = ks.length ? (retained ? 'kept earlier' : 'kept') + (extra ? ' &middot; ' + extra : '') : 'skipped';
  return `<li class="${ks.length ? 'ok' : 'skip'}"><b>${STEPNAME[n]}</b><span>${a}% &rarr; ${b}%</span><span class="muted">${lbl}</span></li>`;
}
function groupSteps(arr) { const g = {}; (arr || []).forEach(e => { const k = e.step || e.tool; (g[k] = g[k] || []).push(e); }); return g; }
function liveSteps(j) {
  const raw = {}; (j.steps_so_far || []).forEach(e => { raw[e.step] = e; });
  if (Object.keys(raw).length) {
    let last = -1; STEPS.forEach((n, i) => { if (raw[n]) last = i; });
    el('as-steps').innerHTML = STEPS.map((n, i) => {
      if (n === 'boom' && !raw[n]) return '';   // no boom step when either side has no booms
      const ends = (j.steps_so_far || []).filter(e => e.step === n && e.status === 'end');
      const open = raw[n] && raw[n].status !== 'end' ? raw[n] : null;
      if (open) return `<li class="act"><b>${STEPNAME[n]}</b><span class="muted">working... ${Math.round(open.score_before)}%</span></li>`;
      if (ends.length) return stepRow(n, ends);
      return `<li class="${i < last ? 'skip' : ''}"><b>${STEPNAME[n]}</b><span class="muted">${i < last ? 'skipped' : 'waiting'}</span></li>`;
    }).join('');
    el('as-steps').classList.remove('hidden');
    return;
  }
  const cur = stepIdx(j.stage || j.message);
  el('as-steps').innerHTML = STEPS.map((n, i) => `<li class="${i < cur ? 'ok' : i === cur ? 'act' : ''}"><b>${STEPNAME[n]}</b><span class="muted">${i === cur ? 'working...' : i < cur ? 'done' : 'waiting'}</span></li>`).join('');
  el('as-steps').classList.remove('hidden');
}
function finalSteps(res) {
  const by = groupSteps(res.steps);
  const off = res.plan && res.plan.offset != null ? 'starts at ' + fmtMS(res.plan.offset) : '';
  const rt = res.plan && res.plan.retime && res.plan.retime.sections ? res.plan.retime.sections.length : 0;
  el('as-steps').innerHTML = STEPS.map(n => {
    if (n === 'offset') return `<li class="ok"><b>${STEPNAME[n]}</b><span class="muted">${off}</span></li>`;
    if (n === 'boom' && !by[n]) return '';
    const list = by[n]; if (!list) return `<li class="skip"><b>${STEPNAME[n]}</b><span class="muted">skipped</span></li>`;
    return stepRow(n, list, n === 'retime' && rt ? rt + ' section' + (rt > 1 ? 's' : '') : '');
  }).join('');
  el('as-steps').classList.remove('hidden');
  const lim = (res.limits_hit || []).filter(x => /^budget/i.test(x));
  const ln = el('as-limits'); ln.textContent = lim.join(' · '); ln.classList.toggle('hidden', !lim.length);
}
async function runOne(r) {   // returns result or null (cancelled / failed)
  const v = S.videoPath; if (!v || !r || false) return null;
  const g = AS.guard, key = pk(v, r.path);
  const body = {video_path: v, song_path: r.path, lead_s: g.lead / 1000, allow_loop: !!g.loop,
    guardrails: {max_speed_dev: g.dev / 100, max_tempo_pct: g.tempo, max_cuts: g.cuts}};
  body.prefer_energy = !!g.energyFirst;
  body.smooth = !!g.ramps;  // U11-SMOOTH: top-level 'smooth' bool (guard key stays 'ramps'); the old 'ramps' flag is left at its default
  if (r.source === 'original' && g.unlock) body.unlock_original = true;   // default: offset locked at 0, retime + micro only
  AS.lastReq = body; AS.cancelled = false;
  AS.running = {key, name: r.name}; ui();
  el('as-stage').textContent = 'Starting...'; setRing(0); liveSteps({stage: ''});
  try {
    const rs = await post('/api/auto-sync', body); const j0 = await rs.json();
    const id = j0.job_id; if (!id) throw new Error(j0.detail || 'Auto-sync failed to start');
    if (AS.running) AS.running.id = id;
    let j = j0, k = 0;
    while (!AS.cancelled) {
      j = await (await fetch('/api/auto-sync/' + id)).json();
      if (j.status !== 'running') break;
      el('as-stage').textContent = j.message || (j.current_step && STEPNAME[j.current_step]) || j.stage || 'Working...';
      const pct = j.total ? 100 * (j.done || 0) / j.total : Math.min(90, ++k * 4); setRing(pct); liveSteps(j);
      await sleep(600);
    }
    if (AS.cancelled || j.status === 'cancelled') return null;
    if (j.status === 'error') throw new Error(j.error || 'Auto-sync failed');
    const res = j.result; AS.runs[key] = {result: res, audioUrl: ''};
    if (window.nativeNotify) nativeNotify('Auto-sync finished', (r.name || 'Song') + ' is ready');
    r.auto_sync_cached = res.final || null;
    const segs = previewSegs(res.plan), tempo = (res.plan && res.plan.tempo_ratio) || 1;
    if (res.eligible !== false && segs && segs.length && S.videoPath === v) {
      try { AS.runs[key].audioUrl = (await renderAudio(r.path, segs, tempo)).url; } catch (e) { /* preview falls back to single trim */ }
    }
    return res;
  } catch (e) { note(e.message, true); return null; }
  finally { AS.running = null; }
}
el('as-go').onclick = async () => {
  const r = S.inUse; if (!r) return;
  const was = !aud.paused; pause();
  const res = await runOne(r);
  if (res && res.eligible !== false) { AS.mode = 'synced'; applyAfterSplice(); }
  ui(); window.asAfterResults(); if (was) play();
};
el('as-cancel').onclick = () => {
  const id = AS.running && AS.running.id; if (id) post('/api/auto-sync/' + id + '/cancel').catch(() => {});
  el('as-stage').textContent = 'Cancelling...'; AS.cancelled = true; AS.running = null; if (AS.all) AS.all.cancel = true; ui(); };
el('as-mode').querySelectorAll('button').forEach(b => b.onclick = () => {
  AS.mode = b.dataset.m; ui(); const was = !aud.paused; pause(); applyAfterSplice(); if (was) play();
  if (AS.mode === 'orig') vid.playbackRate = S.rate || 1;
});

// ---------- panel state ----------
function ui() {
  const u = S.inUse, orig = !!(u && u.source === 'original'), locked = false, ok = !!(u && S.video && !AS.running);
  const go = el('as-go'); go.disabled = !ok; go.classList.toggle('run', !!AS.running);
  go.title = !u ? 'Press "Use this audio" on a source first' : !S.video ? 'Needs a video' : orig ? 'Original audio: start locked at 0:00, auto-sync retimes the video to fit' : '';
  el('as-lock').textContent = orig ? 'Original audio: start locked at 0:00. Auto-sync retimes the video to it (tick "Unlock original" in Guardrails to also allow tempo, splice and rearranging).' : '';
  el('as-run').classList.toggle('hidden', !AS.running);
  const run = curRun(), res = run && run.result;
  if (!AS.running) el('as-steps').classList.toggle('hidden', !res);
  el('as-done').classList.toggle('hidden', !res || !!AS.running);
  if (res && !AS.running) {
    const f = res.final || {};
    finalSteps(res);
    el('as-head').textContent = res.eligible === false ? 'Not eligible' : `Sync ${f.sync_headline}% · Tight ${Math.round(f.tight_pct)}% · Energy ${Math.round(f.arc_pct)}%`;
    el('as-total').textContent = res.eligible === false ? '' : `Total ${Math.round(f.total_pct)}% · effort ${Math.round(f.effort_pct != null ? f.effort_pct : 0)}%`;
    el('as-why').textContent = res.why_not_100 || '';
    el('as-mode').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.m === AS.mode));
    el('as-pvnote').textContent = AS.mode === 'synced' && sections().length ? 'Preview: video follows the exact retime curve, export renders exact frames' : '';
  }
  rdUI();
  if (window.asAfterUi) window.asAfterUi();   // hook for alt-plans.js (ui() is IIFE-private)
}
window.asUi = ui;
const _uf = window.updateFit; window.updateFit = function () { _uf.apply(this, arguments); ui(); };

// ---------- results ranking ----------
const sortKey = r => { const f = r.auto_sync_cached; return f ? f.total_pct : null; };
window.asAfterResults = () => {
  const box = el('results'); if (!box) return;
  el('as-sort').classList.toggle('hidden', !S.results.length);
  const rows = [...box.querySelectorAll('.res')];
  rows.forEach(d => {
    const r = S.results[d.dataset.i], f = r.auto_sync_cached; let n = d.querySelector('.rafter');
    if (!f) { if (n) n.remove(); return; }
    if (!n) { n = document.createElement('div'); n.className = 'rafter'; d.querySelector('.rtxt').appendChild(n); }
    n.textContent = `after auto-sync ${Math.round(f.total_pct)}% · effort ${Math.round(f.effort_pct || 0)}%` + (f.fit_after_effort != null ? ` · fit after effort ${Math.round(f.fit_after_effort)}%` : '');
    n.title = 'Sorted by total after auto-sync';
  });
  const orig = rows.filter(d => d.classList.contains('orig')), rest = rows.filter(d => !d.classList.contains('orig'));
  const val = d => S.results[d.dataset.i];
  if (AS.sort === 'after') rest.sort((a, b) => { const x = sortKey(val(a)), y = sortKey(val(b)); return (y != null) - (x != null) || (y - x) || (val(b).total_pct - val(a).total_pct); });
  else rest.sort((a, b) => val(b).total_pct - val(a).total_pct);
  let n = 0;
  [...orig, ...rest].forEach(d => {
    box.appendChild(d); if (!d.classList.contains('orig')) { n++; const t = d.querySelector('.rname'); if (t && t.firstChild && t.firstChild.nodeType === 3) t.firstChild.nodeValue = t.firstChild.nodeValue.replace(/^\d+\. /, n + '. '); }
  });
  el('as-sort').querySelectorAll('button[data-s]').forEach(b => b.classList.toggle('on', b.dataset.s === AS.sort));
};
el('as-sort').querySelectorAll('button[data-s]').forEach(b => b.onclick = () => { AS.sort = b.dataset.s; window.asAfterResults(); });
el('as-all').onclick = async () => {
  if (AS.all) { AS.all.cancel = true; AS.cancelled = true; return; }
  const list = [...el('results').querySelectorAll('.res:not(.orig)')].map(d => S.results[d.dataset.i]).filter(r => r && !r.dropped).slice(0, 5);
  if (!list.length || !S.videoPath || AS.running) return;
  AS.all = {cancel: false}; el('as-all').textContent = 'Cancel'; el('as-allmsg').classList.remove('hidden');
  let i = 0;
  for (const r of list) {
    if (AS.all.cancel) break; i++;
    el('as-allmsg').textContent = `Auto-sync ${i}/${list.length} · ${r.name}`;
    if (!r.auto_sync_cached) await runOne(r);
    window.asAfterResults();
  }
  el('as-allmsg').textContent = AS.all.cancel ? 'Cancelled.' : `Auto-synced ${list.length} sources.`;
  AS.all = null; AS.cancelled = false; el('as-all').textContent = 'Auto-sync all'; ui();
  if (curRun()) applyAfterSplice();
};

// ---------- render video ----------
const fmtEst = s => s < 90 ? 'about ' + Math.max(1, Math.round(s)) + ' s' : 'about ' + Math.round(s / 60) + ' min';
const planNow = () => { const r = curRun(); return r && r.result.eligible !== false ? r.result.plan : null; };
async function loadTools() {
  try { AS.rife = (await (await fetch('/api/render-tools')).json()).rife || null; } catch (e) { AS.rife = null; }
  rdUI();
}
async function estimate() {
  const p = planNow(); if (!p || !S.videoPath) return;
  const sig = [S.videoPath, AS.quality, JSON.stringify(p.retime && p.retime.sections)].join('|');
  if (sig === AS.estSig) return; AS.estSig = sig; const tok = ++AS.estTok;
  el('rd-est').textContent = 'measuring...';
  try {
    const r = await post('/api/render-estimate', {video_path: S.videoPath, plan: p, interp: AS.quality, measure: true});
    const j = await r.json(); if (tok !== AS.estTok) return;
    if (!r.ok) throw new Error(j.detail || r.statusText);
    el('rd-est').textContent = `Estimated ${fmtEst(j.est_seconds)} (${j.frames} frames, ${j.interpolated} interpolated${j.estimated ? ', rough guess' : ''})`;
  } catch (e) { if (tok === AS.estTok) { AS.estSig = ''; el('rd-est').textContent = 'Estimate unavailable: ' + e.message; } }
}
function rdUI() {
  const p = planNow(), busy = !!(AS.rd && AS.rd.running), rife = AS.quality === 'rife', miss = rife && !(AS.rife && AS.rife.installed);
  el('rd-q').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.q === AS.quality));
  el('rd-go').disabled = !p || busy || miss; el('rs-go').disabled = !p || busy;
  el('rd-go').title = el('rs-go').title = p ? '' : 'Run Auto-sync first';
  el('rd-cancel').classList.toggle('hidden', !busy);
  const inst = el('rd-rife'); inst.classList.toggle('hidden', !miss || busy);
  inst.textContent = 'Install RIFE (' + ((AS.rife && AS.rife.size_mb) || 34) + ' MB after download of 436 MB)';
  inst.disabled = !(AS.rife && AS.rife.available !== false);
  if (!p) { el('rd-est').textContent = 'Run Auto-sync first, then render the retimed video.'; AS.estSig = ''; }
  else if (!busy && !(AS.rd && AS.rd.installing)) estimate();
}
el('rd-q').querySelectorAll('button').forEach(b => b.onclick = () => { AS.quality = b.dataset.q; rdUI(); });
async function pollRender(id, stageEl) {
  let j; do {
    j = await (await fetch('/api/render-mp4/' + id)).json();
    el('rd-bar').style.width = Math.max(4, j.done || 0) + '%'; el('rd-stage').textContent = j.stage || '';
    if (j.status === 'running') await sleep(700);
  } while (j.status === 'running' && AS.rd && !AS.rd.gone);
  return j;
}
el('rd-rife').onclick = async () => {
  AS.rd = {running: true, installing: true}; el('rd-prog').classList.remove('hidden'); el('rd-stage').textContent = 'Starting download...'; rdUI();
  try {
    const r = await post('/api/render-tools/rife'); const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
    const f = await pollRender(j.job_id); if (f.status === 'error') throw new Error(f.error);
    el('rd-stage').textContent = 'RIFE installed.';
  } catch (e) { el('rd-stage').textContent = 'Install failed: ' + e.message; }
  AS.rd = null; el('rd-prog').classList.add('hidden'); await loadTools();
};
function openLinks(j, out) {
  const name = baseName(j.path), nat = window.pywebview && window.pywebview.api;
  const reveal = nat && (nat.reveal_path || nat.reveal || nat.show_in_folder), open = nat && nat.open_path;
  out.innerHTML = nat && !(reveal || open)
    ? `<button class="dl" id="rd-save">Save ${esc(name)} as…</button>`
    : (open ? `<button class="dl" id="rd-open">Open ${esc(name)}</button>` : `<a class="dl" id="rd-link" href="${esc(j.url)}" download>Open ${esc(name)}</a>`) + (reveal ? ' <button id="rd-reveal">Show in folder</button>' : '');
  out.insertAdjacentHTML('beforeend', `<div class="muted">Saved to .exports/${esc(name)}${j.seconds ? ' · took ' + Math.round(j.seconds) + ' s' : ''}</div>`);
  const sv = out.querySelector('#rd-save'); if (sv) sv.onclick = async () => { const d = await nat.save_as(j.path); sv.nextSibling.textContent = d ? 'Saved to ' + d : 'Not saved'; };
  const rv = out.querySelector('#rd-reveal'); if (rv) rv.onclick = () => (nat.reveal_path || nat.reveal || nat.show_in_folder)(j.path);
  const op = out.querySelector('#rd-open'); if (op) op.onclick = () => nat.open_path(j.path);
}
el('rd-go').onclick = async () => {
  const p = planNow(), r = S.sel; if (!p || AS.rd) return;
  el('rd-out').textContent = ''; el('rd-prog').classList.remove('hidden'); el('rd-bar').style.width = '4%'; el('rd-stage').textContent = 'Starting...';
  AS.rd = {running: true}; rdUI();
  try {
    const rs = await post('/api/render-mp4', {video_path: S.videoPath, song_path: r.path, plan: p, interp: AS.quality, ...fxBody()});
    const j0 = await rs.json(); if (!rs.ok && rs.status !== 409) throw new Error(j0.detail || rs.statusText);
    AS.rd.id = j0.job_id; const j = await pollRender(j0.job_id);
    if (j.status === 'error') throw new Error(j.error || 'Render failed');
    if (j.status === 'cancelled' || j.stage === 'cancelled') el('rd-stage').textContent = 'Cancelled.';
    else { el('rd-bar').style.width = '100%'; el('rd-stage').textContent = 'Done'; openLinks(j, el('rd-out')); if (window.nativeNotify) nativeNotify('Render finished', 'Your retimed video is ready'); }
  } catch (e) { el('rd-stage').textContent = /cancel/i.test(e.message) ? 'Cancelled.' : 'Render failed: ' + e.message; }
  AS.rd = null; if (!/Done/.test(el('rd-stage').textContent)) el('rd-prog').classList.add('hidden'); rdUI();
};
el('rd-cancel').onclick = async () => {
  if (!AS.rd || !AS.rd.id) return; el('rd-stage').textContent = 'Cancelling...';
  await post('/api/render-mp4/' + AS.rd.id + '/cancel');
};
el('rs-go').onclick = async () => {
  const p = planNow(), r = S.sel; if (!p || AS.rd) return;
  const fx = window.FX || {};
  el('rd-out').textContent = ''; el('rd-prog').classList.remove('hidden'); el('rd-bar').style.width = '6%'; el('rd-stage').textContent = 'Building DaVinci Resolve bundle...';
  AS.rd = {running: true}; rdUI();
  try {
    const rs = await post('/api/export-resolve', {video_path: S.videoPath, song_path: r.path, plan: p, boom: fx.boom != null ? fx.boom : 40, decay: fx.decay != null ? fx.decay : 180, drops_only: !!fx.drops, shake: !!fx.shake});
    let j = await rs.json(); if (!rs.ok) throw new Error(j.detail || rs.statusText);
    const id = j.job_id;
    while (j.status === 'running') { await sleep(500); j = await (await fetch('/api/scan/' + id)).json(); el('rd-bar').style.width = Math.max(8, j.done || 0) + '%'; }
    if (j.status === 'error') throw new Error(j.error || 'Export failed');
    el('rd-bar').style.width = '100%'; el('rd-stage').textContent = `Done: ${j.clips} clips, ${j.markers} markers`;
    const name = baseName(j.zip || j.path);
    el('rd-out').innerHTML = `<a class="dl" id="rs-link" href="${esc(j.url)}" download>Download ${esc(name)}</a><div class="muted">Import: see HOW-TO-IMPORT.txt (inside the bundle). Folder: .exports/${esc(baseName(j.path))}</div>`;
  } catch (e) { el('rd-stage').textContent = 'Resolve export failed: ' + e.message; el('rd-prog').classList.add('hidden'); }
  AS.rd = null; rdUI();
};

guardUI(); loadTools(); ui();
})();
