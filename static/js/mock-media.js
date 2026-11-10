'use strict';
// Stand-in for the media endpoints, used ONLY when the real ones 404 (backend not landed). Never active against a server that has them.
window.MOCK_MEDIA = (function () {
  const day = 86400, now = Math.floor(Date.now() / 1000);
  const F = (n, k, size, dur, d, src) => ({name: n, path: '/mock/' + n, kind: k, size, mtime: now - d * day, duration: dur,
    source: src ? {source_url: src, referrer: 'https://' + new URL(src).host + '/', downloaded_by: 'Safari', downloaded_at: now - d * day} : null});
  const roots = [{path: '/mock/Movies', name: 'Movies'}, {path: '/mock/Music', name: 'Music'}, {path: '/mock/Downloads', name: 'Downloads'}];
  const files = [F('clip_a.mp4', 'video', 180e6, 212.4, 2, 'https://example.com/v/abc'), F('clip_b.mov', 'video', 90e6, 95, 9, null), F('song_one.mp3', 'audio', 6e6, 201, 1, null), F('song_two.wav', 'audio', 41e6, 238, 30, 'https://cdn.example.org/a.wav')];
  const svg = (t, w, h) => 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><rect width="100%" height="100%" fill="hsl(${(t * 37) % 360},40%,25%)"/></svg>`);
  return {
    roots: () => ({roots}),
    list: p => ({path: p, parent: null, folders: p === '/mock/Movies' ? [{name: 'Sub', path: '/mock/Movies/Sub'}] : [], files}),
    thumb: (p, t) => svg(Math.round(t || 0), 160, 90),
    link: p => ({path: p, name: p.split('/').pop(), kind: 'video'}),
    jobs: () => ({events: [], stats: {}}),
  };
})();
