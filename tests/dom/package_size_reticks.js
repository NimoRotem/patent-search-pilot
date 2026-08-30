/*  The real page, the real inline script, a real DOM, a real change event.
    happy-dom's document.write does not run inline scripts here, so they are evaluated
    explicitly in the window: same code, same document, just started by hand. */
const fs = require('fs');
const { Window } = require('happy-dom');

const html = fs.readFileSync(process.argv[2], 'utf8');
const win = new Window({ url: 'https://nimo.iptorch.com/' });
const d = win.document;
d.documentElement.innerHTML = html
  .replace(/^[\s\S]*?<html[^>]*>/i, '')
  .replace(/<\/html>[\s\S]*$/i, '');

let ran = 0;
for (const s of Array.from(d.querySelectorAll('script'))) {
  if (s.getAttribute('src')) continue;
  try { win.eval(s.textContent); ran++; } catch (e) { console.log('SCRIPT ERROR:', e.message); }
}
console.log('inline scripts run  ' + ran);

const sel = d.getElementById('pkgSize');
if (!sel) { console.log('NO pkgSize'); process.exit(1); }
const boxes = () => Array.from(d.querySelectorAll('.pickbox'));
const ticked = () => boxes().filter(b => b.checked).length;
const tally = () => ((d.getElementById('feeTally') || {}).textContent || '').trim();

console.log('controls            selects=' + d.querySelectorAll('select').length +
            '  submit=' + d.querySelectorAll('button[type=submit]').length +
            '  candidates=' + boxes().length);
console.log('on load             ' + ticked() + ' ticked | ' + tally());
for (const units of ['2', '3', '1']) {
  sel.value = units;
  sel.dispatchEvent(new win.Event('change', { bubbles: true }));
  console.log('size -> ' + units + ' unit' + (units === '1' ? ' ' : 's') + ' (' +
    sel.options[sel.selectedIndex].getAttribute('data-docs') + ' docs)  ' +
    ticked() + ' ticked | ' + tally().slice(0, 78));
}
const before = ticked();
const extra = boxes().find(b => !b.checked);
extra.checked = true;
extra.dispatchEvent(new win.Event('change', { bubbles: true }));
console.log('hand tick           ' + before + ' -> ' + ticked() + ' | ' + tally().slice(0, 95));
win.happyDOM.close();
