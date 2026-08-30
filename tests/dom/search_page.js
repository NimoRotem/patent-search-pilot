/*  The redesigned search page, driven in a real DOM: the mode select, the estimate and the gate. */
const fs = require('fs');
const { Window } = require('happy-dom');
const win = new Window({ url: 'https://nimo.iptorch.com/' });
const d = win.document;
d.documentElement.innerHTML = fs.readFileSync(process.argv[2], 'utf8')
  .replace(/^[\s\S]*?<html[^>]*>/i, '').replace(/<\/html>[\s\S]*$/i, '');
let ran = 0;
for (const s of Array.from(d.querySelectorAll('script'))) {
  if (s.getAttribute('src')) continue;
  try { win.eval(s.textContent); ran++; } catch (e) { console.log('SCRIPT ERROR:', e.message); }
}
const sel = d.getElementById('searchModeSel');
const adv = d.getElementById('attackAdv');
const eta = () => (d.getElementById('etaLine') || {}).textContent || '';
const gate = () => !(d.getElementById('attackGate') || {}).hidden;
const go = () => (d.getElementById('gobtn') || {}).textContent || '';

console.log('scripts run                ' + ran);
console.log('order: h1 < textarea < mode select < advanced');
const H = d.documentElement.innerHTML;
console.log('   h1 before textarea      ' + (H.indexOf('<h1') < H.indexOf('id="qbox"')));
console.log('   textarea before select  ' + (H.indexOf('id="qbox"') < H.indexOf('id="searchModeSel"')));
console.log('mode options               ' +
  Array.from(sel.options).map(o => o.value + (o.disabled ? '(disabled)' : '')).join(', '));
console.log('advanced blocks on page    ' + d.querySelectorAll('details.advopts').length);
console.log('advanced controls          ' +
  Array.from(d.querySelectorAll('#attackAdv select, #attackAdv input')).map(e => e.name || e.id).join(', '));
console.log('opt-ins in the button row  ' +
  Array.from(d.querySelectorAll('.gostick input[type=checkbox]')).map(e => e.name).join(', ') +
  '  (checked: ' + Array.from(d.querySelectorAll('.gostick input[type=checkbox]')).filter(e => e.checked).length + ')');
console.log('');
console.log('no claims yet:');
console.log('   attack option disabled  ' + sel.querySelector('option[value=attack]').disabled);
console.log('   gate shown              ' + gate());
console.log('   eta                     ' + eta());
console.log('   button                  ' + go());

win.__hasClaims = true;
win.eval('window.__hasClaims = true; window.__modeRepaint && window.__modeRepaint();');
sel.value = 'attack';
sel.dispatchEvent(new win.Event('change', { bubbles: true }));
console.log('');
console.log('claims present, build selected:');
console.log('   advanced visible        ' + !adv.hidden);
console.log('   gate shown              ' + gate());
console.log('   button                  ' + go());
console.log('   eta (45, unbatched)     ' + eta());
for (const [id, val] of [['advReadTop', '400'], ['advBatched', true], ['advConcept', true]]) {
  const el = d.getElementById(id);
  if (el.type === 'checkbox') el.checked = val; else el.value = val;
  el.dispatchEvent(new win.Event('change', { bubbles: true }));
  console.log('   eta after ' + id.padEnd(12) + ' ' + eta());
}
sel.value = 'fast';
sel.dispatchEvent(new win.Event('change', { bubbles: true }));
console.log('   eta back on fast        ' + eta());
console.log('   advanced hidden again   ' + adv.hidden);
win.happyDOM.close();
