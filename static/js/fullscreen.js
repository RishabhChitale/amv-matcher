/* F3: fullscreen button on the player. HTML Fullscreen API first; falls back to pywebview js_api toggle_fullscreen
   (native frozen app) when the API is missing or the page did not actually fill the screen. Keys: F, Esc (browser), dblclick. */
(function () {
  const stage = document.getElementById('stage'); if (!stage) return;
  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  let native = false;
  const native_api = () => window.pywebview && window.pywebview.api && window.pywebview.api.toggle_fullscreen;
  function sync() {
    const on = !!fsEl() || native;
    stage.classList.toggle('is-fs', on);
    btn.title = on ? 'Exit fullscreen (F / Esc)' : 'Fullscreen (F)';
    btn.setAttribute('aria-label', btn.title);
    btn.innerHTML = on ? ICON_OUT : ICON_IN;
  }
  const ICON_IN = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4"/></svg>';
  const ICON_OUT = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 2v4H2M14 6h-4V2M10 14v-4h4M2 10h4v4"/></svg>';
  const btn = document.createElement('button');
  btn.id = 'fs-btn'; btn.className = 'fs-btn'; btn.type = 'button';
  stage.appendChild(btn);
  function viaNative() { const f = native_api(); if (!f) return false; native = !native; try { f.call(window.pywebview.api); } catch (e) { native = !native; return false; } sync(); return true; }
  async function toggle() {
    if (native) return viaNative();
    if (fsEl()) { try { await (document.exitFullscreen || document.webkitExitFullscreen).call(document); } catch (e) {} return; }
    const req = stage.requestFullscreen || stage.webkitRequestFullscreen;
    if (req) { try { await req.call(stage); } catch (e) { /* fall through */ } }
    // blocked/unsupported (e.g. embedded webview): did it fill the screen?
    setTimeout(() => { if (!fsEl()) viaNative(); }, 150);
  }
  btn.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
  stage.addEventListener('dblclick', (e) => { if (e.target.closest('button')) return; toggle(); });
  document.addEventListener('fullscreenchange', sync);
  document.addEventListener('webkitfullscreenchange', sync);
  document.addEventListener('keydown', (e) => {
    const t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'f' || e.key === 'F') { e.preventDefault(); toggle(); }
    else if (e.key === 'Escape' && native) { viaNative(); }
  });
  window.ASFullscreen = { toggle };
  sync();
})();
