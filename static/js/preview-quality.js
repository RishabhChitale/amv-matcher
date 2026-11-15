'use strict';
// F25: 'Preview quality' select + 'upgrading preview' indicator. Preview only: export and scoring use the original file.
// Server contract (krokorok): GET /api/video?path=..&pq=<720|1080|1440|0>; GET /api/preview-status?path=..&pq=..
// -> {state:'none'|'building'|'ready', height:int, playable:bool}. pq=0 means Original (only offered if playable).
(function () {
  const $ = id => document.getElementById(id), vid = $('vid'), KEY = 'amv-preview-quality';
  const OPTS = [['720', '720p'], ['1080', '1080p (default)'], ['1440', '1440p'], ['0', 'Original']];
  const get = () => { try { return localStorage.getItem(KEY) || '1080'; } catch (e) { return '1080'; } };
  const put = v => { try { localStorage.setItem(KEY, v); } catch (e) { /* storage may be blocked */ } };
  let pq = get(), pollTimer = null, token = 0;
  const host = $('vcard'); if (!host || !vid) return;
  const box = document.createElement('div'); box.className = 'vc-pq'; box.id = 'pq-box';
  box.title = 'Preview only: export and scoring always use the original file'; box.innerHTML = '<span>Preview quality</span><div class="pq-row"><select id="pq-sel" title="Preview only: export and scoring always use the original file">' +
    OPTS.map(o => '<option value="' + o[0] + '">' + o[1] + '</option>').join('') + '</select><i id="pq-status" class="pq-status hidden" role="status"></i></div>';
  host.appendChild(box);
  const sel = $('pq-sel'), st = $('pq-status'); sel.value = OPTS.some(o => o[0] === pq) ? pq : '1080'; pq = sel.value;
  function srcFor(path) { return '/api/video?path=' + encodeURIComponent(path) + '&pq=' + pq; }
  function say(t) { st.textContent = t; st.classList.toggle('hidden', !t); }
  let rl = 0;
  // keep: the src already has the explicit pq and plays at the ready height; wait: same src, height not known yet; reload: different src or a lower height than what is ready
  function decide(cur, url, vh, h) {
    if (cur.split('&r=')[0] !== url) return 'reload';
    if (!h) return 'keep';
    if (!vh) return 'wait';
    return vh >= h - 8 ? 'keep' : 'reload';
  }
  function swap(path, h) {   // switch the <video> source (or reload it when a lower height is playing); keep playhead, play state and rate
    const url = srcFor(path), cur = vid.getAttribute('src') || '';
    const d = decide(cur, url, vid.videoHeight, h), same = cur.split('&r=')[0] === url;
    if (d === 'keep') return;
    if (d === 'wait') { const w = () => { vid.removeEventListener('loadedmetadata', w); swap(path, h); }; vid.addEventListener('loadedmetadata', w); return; }
    const t = vid.currentTime || 0, was = !vid.paused, rate = vid.playbackRate, vol = vid.muted;
    const back = () => { vid.removeEventListener('loadedmetadata', back); try { vid.currentTime = Math.min(t, vid.duration || t); } catch (e) { /* not seekable yet */ } vid.playbackRate = rate; vid.muted = vol; if (was) vid.play().catch(() => {}); };
    vid.addEventListener('loadedmetadata', back); vid.src = same ? url + '&r=' + (++rl) : url;
  }
  async function poll(path, my) {
    clearTimeout(pollTimer);
    let j = null;
    try { const r = await fetch('/api/preview-status?path=' + encodeURIComponent(path) + '&pq=' + pq); if (r.ok) j = await r.json(); } catch (e) { /* server busy */ }
    if (my !== token) return;
    if (!j) { say(''); return; }
    const orig = [...sel.options].find(o => o.value === '0'); if (orig) orig.disabled = j.playable === false;
    if (j.state === 'building') { say('Upgrading preview…'); pollTimer = setTimeout(() => poll(path, my), 1500); return; }
    say(''); if (j.state === 'ready') swap(path, j.height | 0);
  }
  function refresh() { const p = window.S && S.videoPath; if (!p) return; const my = ++token; poll(p, my); }
  sel.addEventListener('change', () => { pq = sel.value; put(pq); refresh(); });
  // new video loaded -> re-check (app.js/drop.js set S.videoPath; the poll picks it up)
  let lastPath = '';
  setInterval(() => { const p = window.S && S.videoPath; if (p && p !== lastPath) { lastPath = p; refresh(); } }, 600);
  window.MKPreviewQuality = {get: () => pq, refresh};
  window.MKPQ = () => pq;
  window.MKPreviewQuality.decide = decide;
})();
