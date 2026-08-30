/*  Does the progress panel stream the references as they are read?
    The real createProgress from the real app.js, in a real DOM, fed real event shapes. */
const fs = require('fs');
const { Window } = require('happy-dom');

const win = new Window({ url: 'https://nimo.iptorch.com/' });
win.document.body.innerHTML = '<div class="refining"><div id="mount"></div>' +
  '<div class="vh" data-progress-live></div></div>';
win.eval(fs.readFileSync(process.argv[2], 'utf8'));

const mk = win.eval('typeof createProgress === "function" ? createProgress : null');
if (!mk) { console.log('createProgress not reachable'); process.exit(1); }

const mount = win.document.getElementById('mount');
const pg = mk(mount, { wide: true });
const panel = () => mount.querySelector('.st-detail');
const feed = () => Array.from(mount.querySelectorAll('.st-feed li')).map(li => li.textContent.trim());
const line = () => mount.querySelector('.st-line').textContent;

console.log('before any read  feed rows=' + feed().length +
            ' | feed hidden=' + mount.querySelector('.st-feed').hidden);

const refs = [
  { pub: 'JP2015077655A', title: 'Suction pad and transfer device', chars: 21430, n: 130,
    reused: false, found: true, n_features: 4 },
  { pub: 'US20210231163A1', title: 'Suction cup with a compliant multi-layer seal', chars: 84213,
    n: 131, reused: false, found: true, n_features: 7 },
  { pub: 'CN211104058U', title: '', chars: 0, n: 132, reused: true, found: false, n_features: 0 },
  /*  the evidence-sweep shape: a batch of documents against one requirement */
  { pub: 'DE9105214U1', title: '', chars: 0, n: 133, note: 'claim 1[c]', found: true },
];
refs.forEach((r, i) => {
  pg.apply({ kind: 'chart_progress', msg: 'Reading in full: ' + r.n + ' of 210 · ' + r.pub +
             (r.title ? ' · ' + r.title : ''),
             detail: { done: r.n, total: 210, families: 6138 },
             read_log: refs.slice(0, i + 1) });
});

console.log('headline         ' + line().slice(0, 96));
console.log('feed rows        ' + feed().length + ' (newest first)');
feed().forEach((r, i) => console.log('  ' + (i + 1) + '. ' + r.replace(/\s+/g, ' ')));

//  A repeat of the same frame must not duplicate a row.
pg.apply({ kind: 'chart_progress', detail: { done: 132, total: 210 }, read_log: refs });
console.log('after a repeat   feed rows=' + feed().length);
console.log('stage notes kept ' + (mount.querySelectorAll('.st-all li').length > 0));
win.happyDOM.close();
