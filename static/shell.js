'use strict';
// Native shell glue (menu bar, shortcuts, notifications, quit hook). Safe in a plain browser: everything no-ops.
// Pages listen for:  document.addEventListener('amv-menu', e => e.detail.action)   (actions: new open save save_as
// add_folder export undo redo delete toggle_media toggle_stems toggle_effects play_pause help)
(function () {
  const api = () => (window.pywebview && window.pywebview.api) || null;
  const fire = action => document.dispatchEvent(new CustomEvent('amv-menu', {detail: {action}}));
  window.nativeMenu = fire;

  const editing = t => t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
  document.addEventListener('keydown', e => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
    const k = e.key.toLowerCase();
    let a = null;
    if (k === 's') a = e.shiftKey ? 'save_as' : 'save';
    else if (k === 'o') a = 'open';
    else if (k === 'n') a = 'new';
    else if (k === 'z' && !editing(e.target)) a = e.shiftKey ? 'redo' : 'undo';
    else if (k === 'y' && !editing(e.target)) a = 'redo';
    if (!a) return;
    e.preventDefault();
    fire(a);
  });

  // Notify only when the window is in the background.
  window.nativeNotify = (title, body) => {
    const a = api();
    if (!a || !a.notify || document.hasFocus()) return Promise.resolve(false);
    return a.notify(String(title), String(body || ''));
  };
  // Hooks other scripts may set: nativeBeforeQuit = () => autosave()
  window.nativeBeforeQuit = window.nativeBeforeQuit || null;
  window.nativeProjectDialogs = {
    open: () => (api() && api().pick_project_open ? api().pick_project_open() : Promise.resolve('')),
    save: name => (api() && api().pick_project_save ? api().pick_project_save(name || '') : Promise.resolve('')),
  };
  window.nativeResolve = {
    installed: () => (api() && api().resolve_installed ? api().resolve_installed() : Promise.resolve(false)),
    open: p => (api() && api().open_in_resolve ? api().open_in_resolve(p) : Promise.resolve(false)),
  };
})();
