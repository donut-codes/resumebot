/*consent:find*/
(() => {
const BANNER = new RegExp('cookie|consent|gdpr|ccpa|onetrust|didomi|usercentrics|trustarc|osano|iubenda|cybot|' +
  'termly|truste|cc[-_ ]window|qc[-_ ]cmp|sp[-_ ]message', 'i');
const TOPIC = /cookie|tracking|personal data/i;
const BANNER_CHARS = 8000;
const CONTROLS = 'button, [role=button], input[type=button], input[type=submit], a';
const DIALOG = 'dialog, [role=dialog], [role=alertdialog], [aria-modal=true]';
const shown = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0; };
const named = (el) => [el.id, typeof el.className === 'string' ? el.className : '', el.getAttribute('aria-label')]
  .filter(Boolean).join(' ').replace(/([a-z0-9])([A-Z])/g, '$1 $2');
document.querySelectorAll('[data-rb-consent]').forEach((e) => e.removeAttribute('data-rb-consent'));
// The banners come first, found by their names alone (cheap): only an element that calls itself a banner, or is a
// dialog, has its text read, once, and a control is one of the banner's when it is inside one that talks about
// cookies. (Reading the text of every control's every ancestor made a results page of hundreds of links slow.)
const inside = new Set();
for (const el of document.querySelectorAll('[id], [class], [aria-label], ' + DIALOG)) {
  if (el === document.body || el === document.documentElement) continue;
  if (!(BANNER.test(named(el)) || el.matches(DIALOG))) continue;
  const said = el.innerText || '';
  if (said.length > BANNER_CHARS || !TOPIC.test(said)) continue;
  for (const control of el.querySelectorAll(CONTROLS)) inside.add(control);
}
const labels = [];
for (const el of document.querySelectorAll(CONTROLS)) {
  if (!inside.has(el) || el.disabled || !shown(el)) continue;
  if (el.tagName === 'A' && !/^(#.*|javascript:.*)?$/i.test((el.getAttribute('href') || '').trim())) continue;
  const label = ((el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '') + '')
    .trim().slice(0, 80);
  if (!label) continue;
  el.setAttribute('data-rb-consent', String(labels.length));
  labels.push(label);
}
return JSON.stringify(labels);
})()
