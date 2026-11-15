'use strict';
// F25: 'Preview quality' select + 'upgrading preview' indicator. Preview only: export and scoring use the original file.
// Server contract (krokorok): GET /api/video?path=..&pq=<720|1080|1440|0>; GET /api/preview-status?path=..&pq=..
// -> {state:'none'|'building'|'ready', height:int, playable:bool}. pq=0 means Original (only offered if playable).
(function () {
  const $ = id => document.getElementById(id), vid = $('vid'), KEY = 'amv-preview-quality';
  const OPTS = [['720', '720p'], ['1080', '1080p (default)'], ['1440', '1440p'], ['0', 'Original']];
  const get = () => { try { return localStorage.getItem(KEY) || '1080'; } catch (e) { return '1080'; } };
  const put = v => { try { localStorage.setItem(KEY, v); } catch (e) { /* storage may be blocked */ } };
  let init = false, pq = get(), pollTimer = null, token = 0, shown = '';
  const host = $('vcard'); if (!host || !vid) return;
  const box = document.createElement('div'); box.className = 'vc-pq'; box.id = 'pq-box';
  box.title = 'Preview only: export and scoring always use the original file'; box.innerHTML = '<span>Preview quality</span><div class="pq-row"><select id="pq-sel" title="Preview only: export and scoring always use the original file">' +
    OPTS.map(o => '<option value="' + o[0] + '">' + o[1] + '</option>').join('') + '</select><i id="pq-status" class="pq-status hidden" role="status"></i></div>';
  host.appendChild(box);
  const sel = $('pq-sel'), st = $('pq-status'); sel.value = OPTS.some(o => o[0] === pq) ? pq : '1080'; pq = sel.value;
  function srcFor(path) { return '/api/video?path=' + encodeURIComponent(path) + '&pq=' + pq; }
  function say(t) { st.textContent = t; st.classList.toggle('hidden', !t); }
  function swap(path) {   // switch the <video> source, keep playhead, play state and rate
    const t = vid.currentTime || 0, was = !vid.paused, rate = vid.playbackRate, vol = vid.muted;
    const url = srcFor(path); if (shown === url) return; shown = url;
    const cur = vid.getAttribute('src') || ''; if (cur === url || (pq === '1080' && cur.indexOf('&pq=') < 0 && !init)) { init = true; return; }
    const back = () => { vid.removeEventListener('loadedmetadata', back); try { vid.currentTime = Math.min(t, vid.duration || t); } catch (e) { /* not seekable yet */ } vid.playbackRate = rate; vid.muted = vol; if (was) vid.play().catch(() => {}); };
    vid.addEventListener('loadedmetadata', back); vid.src = url;
  }
  async function poll(path, my) {
    clearTimeout(pollTimer);
    let j = null;
    try { const r = await fetch('/api/preview-status?path=' + encodeURIComponent(path) + '&pq=' + pq); if (r.ok) j = await r.json(); } catch (e) { /* server busy */ }
    if (my !== token) return;
    if (!j) { say(''); return; }
    const orig = [...sel.options].find(o => o.value === '0'); if (orig) orig.disabled = j.playable === false;
    if (j.state === 'building') { say('Upgrading preview…'); pollTimer = setTimeout(() => poll(path, my), 1500); return; }
    say(''); if (j.state === 'ready') swap(path);
  }
  function refresh() { const p = window.S && S.videoPath; if (!p) return; const my = ++token; poll(p, my); }
  sel.addEventListener('change', () => { init = true; pq = sel.value; put(pq); shown = ''; refresh(); });
  // new video loaded -> re-check (app.js/drop.js set S.videoPath; the poll picks it up)
  let lastPath = '';
  setInterval(() => { const p = window.S && S.videoPath; if (p && p !== lastPath) { lastPath = p; shown = ''; refresh(); } }, 600);
  window.MKPreviewQuality = {get: () => pq, refresh};
})();
