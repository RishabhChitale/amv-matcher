'use strict';
// Desktop shell only (window.pywebview present): real file paths instead of uploads. Browser mode is untouched.
(function () {
  const NATIVE = () => !!(window.pywebview && window.pywebview.api);
  const base = p => String(p).split(/[\\/]/).pop();
  const fake = p => ({name: base(p), nativePath: p});
  let target = null;
  window.addEventListener('drop', e => {   // remember which zone got the drop; the shell then calls nativeDrop(paths)
    const t = e.target && e.target.closest ? e.target : document.body;
    target = t.closest('#stage') ? 'video' : t.closest('#tl-wrap,#results-panel') ? 'song' : null;
  }, true);

  const origUpload = upload, origRoute = route, origExLink = exLink;
  upload = function (file, kind, onp) {   // a native file is used in place: no copy into .uploads
    if (file && file.nativePath) { onp(1); return Promise.resolve({path: file.nativePath, name: file.name}); }
    return origUpload(file, kind, onp);
  };
  route = function (files, tgt) { if (!NATIVE()) return origRoute(files, tgt); };   // browser File objects have no real path

  async function register(paths) {
    const r = await POST('/api/register-paths', {paths});
    const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  }
  async function handle(paths, tgt) {
    if (!paths || !paths.length) return;
    const j = await register(paths);
    let vids = j.videos, songs = j.songs;
    if (tgt === 'video') songs = []; else if (tgt === 'song') vids = [];
    if (!vids.length && !songs.length) return note('Nothing usable in that drop (video: mp4 mov mkv webm avi m4v; audio: mp3 wav m4a flac ogg aac)', true);
    if (window.pywebview.api.log) window.pywebview.api.log(`native ${vids.length} video(s), ${songs.length} song(s) registered in place`);
    if (vids.length) enqueue(() => doVideo(fake(vids[0])));
    if (songs.length) enqueue(() => doSongs(songs.map(fake)));
  }
  window.nativeDrop = paths => { const t = target; target = null; return handle(paths, t).catch(e => note(e.message, true)); };

  function wire() {
    if (!NATIVE()) return;
    document.body.classList.add('native');
    $('b-video').onclick = async () => handle(await pywebview.api.pick_video(), 'video').catch(e => note(e.message, true));
    $('b-songs').onclick = async () => handle(await pywebview.api.pick_songs(), 'song').catch(e => note(e.message, true));
    exLink = function (j, what) {   // exports go through the native Save dialog
      $('ex-out').innerHTML = `<button class="dl" id="ex-link">Save ${esc(base(j.path))} as…</button><div class="muted" id="ex-saved">Choose where to save it</div>`;
      $('ex-link').onclick = async () => {
        const dest = await pywebview.api.save_as(j.path);
        $('ex-saved').textContent = dest ? 'Saved to ' + dest : 'Not saved';
      };
      $('ex-out').dataset.kind = what; $('ex-out').dataset.sig = EXSIG;
    };
  }
  window.addEventListener('pywebviewready', wire);
  if (NATIVE()) wire();
})();
