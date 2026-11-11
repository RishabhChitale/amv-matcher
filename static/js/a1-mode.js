// F10: Original / Auto-synced playback toggle at the bottom-right corner of the A1 track.
// Drives the existing #as-mode buttons (same handler: keeps the playhead, resumes playback).
(function () {
  const el = (id) => document.getElementById(id);
  const wrap = el('tl-wrap'), cvs = el('tl'), panel = el('as-mode');
  if (!wrap || !cvs || !panel) return;
  const LS = 'amv.a1mode.';
  const RULER = 26, V1H = 84, A1H = 84;
  const box = document.createElement('div');
  box.id = 'a1-mode'; box.className = 'a1-mode seg';
  box.innerHTML = '<button data-m="orig">Original</button><button data-m="synced">Auto-synced</button>';
  wrap.appendChild(box);
  const key = () => LS + ((window.S && S.videoPath) || '');
  const hasRun = () => !!(window.AS && AS.curRun && AS.curRun());
  let restored = '';
  function sync() {
    const ok = hasRun();
    box.querySelectorAll('button').forEach((b) => {
      b.disabled = !ok;
      b.title = ok ? '' : 'Run Auto-sync first: there is nothing to switch to yet';
      b.classList.toggle('on', ok && b.dataset.m === AS.mode);
    });
    box.dataset.mode = ok ? AS.mode : '';
    const lb = el('a1-lbl'); if (lb) lb.dataset.mode = ok && AS.mode === 'synced' ? 'synced' : 'orig';
    if (box.dataset.drawn !== box.dataset.mode) { box.dataset.drawn = box.dataset.mode; if (typeof window.draw === 'function') window.draw(); }
    box.style.left = (cvs.offsetLeft + cvs.offsetWidth - box.offsetWidth - 8) + 'px';
    box.style.top = (cvs.offsetTop + RULER + V1H + A1H - box.offsetHeight - 4) + 'px';
    box.classList.toggle('hidden', !(window.S && S.sel));
    if (ok && restored !== key()) {
      restored = key();
      let m = null; try { m = localStorage.getItem(key()); } catch (e) {}
      if (m && m !== AS.mode) { const b = panel.querySelector('[data-m=' + m + ']'); if (b) b.click(); }
    }
  }
  box.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    const pb = panel.querySelector('[data-m=' + b.dataset.m + ']'); if (pb) pb.click();
    try { localStorage.setItem(key(), b.dataset.m); } catch (err) {}
    setTimeout(sync, 0);
  });
  panel.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    try { localStorage.setItem(key(), b.dataset.m); } catch (err) {}
    setTimeout(sync, 0);
  }));
  setInterval(sync, 400);
  window.addEventListener('resize', sync);
  sync();
})();
