/*consent:click*/
(() => { const el = document.querySelector('[data-rb-consent="%NUMBER%"]');
  document.querySelectorAll('[data-rb-consent]').forEach((e) => e.removeAttribute('data-rb-consent'));
  if (!el) return JSON.stringify({clicked: false});
  el.click(); return JSON.stringify({clicked: true}); })()
