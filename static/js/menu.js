'use strict';
// Native menu actions (shell.js dispatches 'amv-menu'). open/save/save_as belong to project-ui.js.
(function () {
  const GUIDE = 'https://github.com/RishabhChitale/amv-matcher/blob/main/docs/USER-GUIDE.md';
  const panel = id => document.getElementById(id);
  function help() {
    let m = panel('help-modal');
    if (!m) {
      m = document.createElement('div'); m.id = 'help-modal'; m.className = 'mm';
      m.innerHTML = '<div class="mm-box glass"><h3>Help</h3><p>Pick a video, add a folder of songs, then press Scan. Click a song to hear it on A1; Auto-sync fits it to the video.</p><p><a id="help-link" href="' + GUIDE + '" target="_blank" rel="noopener">Open the full user guide</a></p><button id="help-close">Close</button></div>';
      document.body.appendChild(m);
      m.classList.remove('hidden');
      m.onclick = e => { if (e.target === m || e.target.id === 'help-close') m.classList.add('hidden'); };
    }
    else m.classList.toggle('hidden');
  }
  const flip = id => { const e = panel(id); if (e) e.classList.toggle('hidden'); };
  const A = {
    new: () => window.MATCH && MATCH.openNew(),
    add_folder: () => { const b = panel('m-add'); if (b) b.click(); },
    export: () => { const e = panel('export'); if (e) { e.classList.remove('hidden'); e.scrollIntoView({block: 'nearest'}); e.classList.add('flash'); setTimeout(() => e.classList.remove('flash'), 900); } },
    undo: () => undoRemoval(),
    redo: () => {},
    delete: () => deleteSel(),
    toggle_media: () => flip('media'),
    toggle_stems: () => { const b = panel('stems-toggle'); if (b) b.click(); },
    toggle_effects: () => flip('fx'),
    play_pause: () => panel('b-play').click(),
    help,
  };
  document.addEventListener('amv-menu', e => { const f = A[e.detail && e.detail.action]; if (f) f(); });
})();
