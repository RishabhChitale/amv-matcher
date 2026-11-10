'use strict';
// Developer 'Do not disturb' toggle: while on, tools/relaunch_app.sh waits instead of quitting/relaunching this app.
(function () {
  const top = document.querySelector('.topbar'); if (!top) return;
  const b = document.createElement('button'); b.id = 'dnd'; b.type = 'button';
  b.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="8" cy="8" r="6.2"/><path d="M4.5 8h7"/></svg><span>Dev: do not disturb</span>';
  const st = document.getElementById('status'); top.insertBefore(b, st || null);
  const tip = on => 'Developer do-not-disturb (' + (on ? 'ON' : 'OFF') + '): while ON, the helper bots will not quit, replace or relaunch this app; they wait until you turn it off.';
  let on = false;
  const show = d => { on = !!(d && d.on); b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); b.title = tip(on); };
  show(null);
  fetch('/api/dnd').then(r => r.json()).then(show).catch(() => {});
  b.onclick = () => fetch('/api/dnd', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({on: !on})}).then(r => r.json()).then(show).catch(() => {});
})();
