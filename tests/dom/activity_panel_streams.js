/*  Stages that name no document must still appear in the panel: those are the minutes the reader
    was staring at one unchanged sentence for. */
const fs = require('fs');
const { Window } = require('happy-dom');
const win = new Window({ url: 'https://nimo.iptorch.com/' });
win.document.body.innerHTML = '<div class="refining"><div id="mount"></div>' +
  '<div class="vh" data-progress-live></div></div>';
win.eval(fs.readFileSync(process.argv[2], 'utf8'));
const mk = win.eval('typeof createProgress === "function" ? createProgress : null');
const mount = win.document.getElementById('mount');
const pg = mk(mount, { wide: true });
const rows = () => Array.from(mount.querySelectorAll('.st-feed li')).map(li =>
  li.textContent.trim().replace(/\s+/g, ' '));

[['elements', 'Decomposing the invention into technical elements…'],
 ['search_progress', 'Searching all 8 channels: 12 of 49 retrieval passes…'],
 ['screen_progress', 'Screening candidates: batch 8 of 25…'],
 ['screen_progress', 'Screening candidates: batch 9 of 25…'],
 ['enriching', 'Fetching missing full text: 40 of 180…']
].forEach(([kind, msg]) => pg.apply({ kind, msg, detail: {} }));

pg.apply({ kind: 'chart_progress',
           msg: 'Reading in full: 1 of 210 · JP2015077655A · Suction pad and transfer device',
           detail: { done: 1, total: 210 },
           read_log: [{ pub: 'JP2015077655A', title: 'Suction pad and transfer device',
                        chars: 21430, n: 1, found: true, n_features: 4 }] });

console.log('panel, newest first:');
rows().forEach((r, i) => console.log('  ' + (i + 1) + '. ' + r));
console.log('empty-state text: ' +
  JSON.stringify(mount.querySelector('.st-empty').textContent));
win.happyDOM.close();
