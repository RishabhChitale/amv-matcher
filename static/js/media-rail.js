/* U4: collapsible Media rail (collapsed by default, remembered) + FPS counter. Standalone; no edits to core files. */
(function () {
  const KEY_OPEN = 'amv.mediaRail.open', KEY_FPS = 'amv.fps.on';
  const ls = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };
  const layout = document.querySelector('main.layout'), media = document.getElementById('media');
  if (layout && media) {
    layout.classList.add('rail-on');
    const btn = document.createElement('button');
    btn.className = 'rail-btn'; btn.type = 'button';
    media.insertBefore(btn, media.firstChild);
    let hoverT = null, pinned = ls.get(KEY_OPEN) === '1';
    const set = (open) => {
      layout.classList.toggle('rail-open', open);
      btn.textContent = open ? '◀ Media' : '▤';
      btn.title = open ? 'Collapse media (click to pin closed)' : 'Media (click to pin open, hover to peek)';
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    set(pinned);
    btn.addEventListener('click', () => { pinned = !pinned; ls.set(KEY_OPEN, pinned ? '1' : '0'); set(pinned); });
    media.addEventListener('mouseenter', () => { if (pinned) return; clearTimeout(hoverT); hoverT = setTimeout(() => set(true), 250); });
    media.addEventListener('mouseleave', () => { if (pinned) return; clearTimeout(hoverT); hoverT = setTimeout(() => { if (!media.matches(':focus-within')) set(false); }, 350); });
    media.addEventListener('focusout', () => { if (!pinned) setTimeout(() => { if (!media.matches(':focus-within') && !media.matches(':hover')) set(false); }, 50); });
  }

  const stage = document.getElementById('stage'), v = document.getElementById('vid');
  if (!stage || !v) return;
  const hud = document.createElement('div'); hud.className = 'fps-hud'; hud.hidden = true;
  const fb = document.createElement('button'); fb.type = 'button'; fb.className = 'fps-btn'; fb.textContent = 'FPS'; fb.title = 'Toggle FPS counter (video fps / rendered fps)';
  stage.appendChild(hud); stage.appendChild(fb);
  let on = ls.get(KEY_FPS) === '1', frames = 0, last = performance.now(), shown = 0, cb = null;
  const vfps = () => { try { return (window.S && S.fps) ? S.fps : 0; } catch (e) { return 0; } };
  const render = () => { hud.textContent = 'video ' + (vfps() ? vfps().toFixed(2).replace(/\.?0+$/, '') : '--') + ' fps\nrender ' + (v.paused ? '--' : shown.toFixed(1)) + ' fps'; };
  const tick = () => {
    cb = null; frames++;
    const now = performance.now();
    if (now - last >= 500) { shown = frames * 1000 / (now - last); frames = 0; last = now; render(); }
    arm();
  };
  const arm = () => {
    if (!on || cb !== null) return;
    if (v.requestVideoFrameCallback) cb = v.requestVideoFrameCallback(tick);
    else cb = requestAnimationFrame(tick);
  };
  const apply = () => {
    hud.hidden = !on; fb.classList.toggle('on', on); ls.set(KEY_FPS, on ? '1' : '0');
    frames = 0; last = performance.now(); shown = 0; render(); arm();
  };
  fb.addEventListener('click', (e) => { e.stopPropagation(); on = !on; apply(); });
  ['play', 'seeked', 'loadeddata'].forEach((n) => v.addEventListener(n, () => { if (on) { arm(); render(); } }));
  v.addEventListener('pause', () => { shown = 0; render(); });
  apply();
})();

// Keep the timeline/overlay canvases in sync when the rail's grid transition changes their size
// (no window 'resize' event fires for that).
(() => {
  if (!window.ResizeObserver) return;
  const cv = document.getElementById('tl');
  if (!cv) return;
  let raf = 0;
  new ResizeObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
  }).observe(cv);
})();
