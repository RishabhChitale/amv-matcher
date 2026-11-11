// Desktop-shell diagnostics: forward page errors and failed /api calls to the app log (window.pywebview.api.log).
// Inert in a plain browser. Never throws.
(function () {
  const send = m => { try { const a = window.pywebview && window.pywebview.api; if (a && a.log) a.log('js: ' + String(m).slice(0, 400)); } catch (e) {} };
  window.addEventListener('error', e => send(`error ${e.message} @${(e.filename || '').split('/').pop()}:${e.lineno}`));
  window.addEventListener('unhandledrejection', e => send(`unhandledrejection ${e.reason && (e.reason.stack || e.reason.message || e.reason)}`));
  const ce = console.error; console.error = function () { send('console.error ' + Array.from(arguments).map(String).join(' ')); return ce.apply(this, arguments); };
  const f = window.fetch;
  window.fetch = function (u, o) {
    const m = (o && o.method) || 'GET';
    return f.apply(this, arguments).then(r => { if (!r.ok && String(u).indexOf('/api/') >= 0) send(`fetch ${m} ${String(u).slice(0, 120)} -> ${r.status}`); return r; },
      err => { send(`fetch ${m} ${String(u).slice(0, 120)} FAILED ${err}`); throw err; });
  };
})();
