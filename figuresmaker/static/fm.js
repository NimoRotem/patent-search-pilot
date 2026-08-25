/* The editor.
 *
 * The figure SVG is drawn in millimetres: one user unit is one millimetre, the same unit the
 * validator and the sheet packer speak. So a drag never has to know about scale factors. The
 * pointer position goes through the SVG's own screen matrix, the figure's data-dx/dy offset comes
 * back off it, and what is sent to the server is millimetres on the figure.
 */
'use strict';

const state = {
  figures: [],
  current: 0,
  registry: { entries: [] },
  report: { findings: [] },
  plan: { figures: [] },
  sheets: [],
  rules: {},
  coverage: { rows: [], columns: [] },
  sources: [],
  busy: false,
};

const $ = (id) => document.getElementById(id);

/* ----------------------------------------------------------------- polling while it builds */

async function poll() {
  const res = await fetch(`${PREFIX}/api/job/${JOB_ID}`);
  if (!res.ok) { setStatus('this job could not be read', true); return; }
  const job = await res.json();
  $('statusline').textContent = job.status;
  renderSteps(job.steps || []);
  renderLive(job.figures || []);
  if (job.title) $('title').textContent = job.title;

  if (job.status === 'failed') {
    const box = $('failure');
    box.classList.remove('hidden');
    box.innerHTML = `<strong>It stopped.</strong> ${escapeHtml(job.error || 'no reason recorded')}
      <a class="small" href="${PREFIX}/api/job/${JOB_ID}/traceback" target="_blank"
         rel="noopener">traceback</a>`;
    return;
  }
  if (job.status === 'awaiting_approval') { await openGate(); return; }
  if (job.status === 'done') { await load(); return; }
  setTimeout(poll, 1500);
}

/* ------------------------------------------------------------------------ the coverage gate
 *
 * The one place the compiler asks before it spends. Rows are parts and claim elements, columns
 * are figures, and a claim row is a mirror of the part it names rather than a second editable
 * copy of the same fact.
 */

const SOURCE_KINDS = ['cad', 'sketch', 'screenshot', 'existing_figure', 'schema', 'blockout'];

async function openGate() {
  const res = await fetch(`${PREFIX}/api/job/${JOB_ID}/coverage`);
  if (!res.ok) { setGate('the coverage matrix could not be read', true); return; }
  const data = await res.json();
  state.coverage = data.coverage || { rows: [], columns: [] };
  state.sources = (data.sources || {}).items || [];
  $('progress').classList.add('hidden');
  $('gate').classList.remove('hidden');
  renderGate();
}

function renderGate() {
  const cov = state.coverage;
  const cols = cov.columns || [];
  const rows = cov.rows || [];
  const covered = rows.filter(r => r.kind === 'numeral' && r.figures.length).length;
  const numerals = rows.filter(r => r.kind === 'numeral').length;
  const drafts = cols.filter(c => c.needs_a_source && !c.filing_ready);

  $('gatetally').innerHTML =
    `<span class="${covered === numerals ? 'ok' : 'w'}">${covered}/${numerals} parts placed</span>` +
    `<span class="${drafts.length ? 'e' : 'ok'}">${cols.length - drafts.length}/${cols.length} views filing-ready</span>` +
    (state.sources.length ? `<span>${state.sources.length} source(s)</span>` : '');

  const gaps = [];
  const missing = rows.filter(r => r.kind === 'numeral' && !r.figures.length);
  if (missing.length) {
    gaps.push(`<div class="gapnote warn">${missing.length} part(s) the description names appear in
      no view: ${missing.slice(0, 12).map(r => escapeHtml(r.key)).join(', ')}${missing.length > 12 ? '...' : ''}.
      Tick them into a figure, or leave them out deliberately.</div>`);
  }
  if (drafts.length) {
    gaps.push(`<div class="gapnote bad">${drafts.map(c => escapeHtml(c.label)).join(', ')}
      ${drafts.length === 1 ? 'is a draft' : 'are drafts'}: a mechanical view built from the
      description alone is a blockout, not a drawing of the invention. Add a CAD file or a sketch
      and point the view at it.</div>`);
  }
  const unmatched = rows.filter(r => r.kind === 'claim_element' && !r.numeral);
  if (unmatched.length) {
    gaps.push(`<div class="gapnote warn">${unmatched.length} claimed feature(s) carry no reference
      character, so 37 CFR 1.83(a) cannot be checked for them.</div>`);
  }
  $('gategaps').innerHTML = gaps.join('') + (state.sources.length ? `<div class="srcchips">` +
    state.sources.map(s => `<span>${escapeHtml(s.filename)} &middot; ${escapeHtml(s.kind.replace(/_/g,' '))}</span>`).join('') +
    `</div>` : '');

  const head = `<thead><tr><th class="rowhead">part or claimed feature</th>` +
    cols.map(c => `<th class="${c.needs_a_source ? (c.filing_ready ? 'ready' : 'draft') : ''}">
        ${escapeHtml(c.label)}<span class="kind">${escapeHtml(c.kind.replace(/_/g, ' '))}</span>
        ${c.needs_a_source ? sourceSelect(c) : '<span class="kind">from the text</span>'}
      </th>`).join('') + `</tr></thead>`;

  const body = rows.map(r => {
    const gap = r.kind === 'numeral' && !r.figures.length;
    const name = r.kind === 'numeral'
      ? `<span class="num">${escapeHtml(r.key)}</span> <span class="term">${escapeHtml(r.label)}</span>`
      : `<span class="term">${escapeHtml(r.label)}</span>${r.numeral ? ` <span class="num">${escapeHtml(r.numeral)}</span>` : ' <span class="muted">(no numeral)</span>'}`;
    return `<tr class="${gap ? 'gap' : ''} ${r.kind === 'claim_element' ? 'claim' : ''}"
                data-key="${escapeHtml(r.key)}">
      <td class="rowhead">${name}</td>` +
      cols.map(c => `<td class="cell" data-key="${escapeHtml(r.key)}" data-figure="${escapeHtml(c.label)}">
        <span class="${r.figures.includes(c.label) ? 'tick' : 'dot'}">${r.figures.includes(c.label) ? '\u2713' : '\u00b7'}</span>
      </td>`).join('') + `</tr>`;
  }).join('');

  $('matrix').innerHTML = head + `<tbody>${body}</tbody>`;
  $('matrix').querySelectorAll('td.cell').forEach(cell =>
    cell.addEventListener('click', () => toggleCell(cell)));
  $('matrix').querySelectorAll('select[data-figure]').forEach(sel =>
    sel.addEventListener('change', () => changeSource(sel)));
}

function sourceSelect(column) {
  const meshes = state.sources.filter(s => s.kind === 'cad');
  const options = SOURCE_KINDS.map(k => {
    if (k === 'cad' && !meshes.length) return '';
    const value = k === 'cad' && meshes.length ? `cad:${meshes[0].id}` : k;
    const on = column.source_kind === k;
    return `<option value="${value}" ${on ? 'selected' : ''}>${k.replace(/_/g, ' ')}</option>`;
  }).join('');
  return `<select data-figure="${escapeHtml(column.label)}">${options}</select>`;
}

async function toggleCell(cell) {
  const present = cell.querySelector('.tick') === null;
  await gatePost('coverage/cell', {
    key: cell.dataset.key, figure: cell.dataset.figure, present });
}

async function changeSource(select) {
  const [kind, id] = select.value.split(':');
  await gatePost('coverage/source', {
    figure: select.dataset.figure, source_kind: kind, source_id: id || '' });
}

async function gatePost(path, body) {
  setGate('saving...', false, true);
  try {
    const res = await fetch(`${PREFIX}/api/job/${JOB_ID}/${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    state.coverage = data.coverage;
    renderGate();
    setGate('');
  } catch (e) { setGate(e.message, true); }
}

function setGate(message, bad, busy) {
  const node = $('gatestatus');
  node.textContent = message || '';
  node.className = 'status' + (bad ? ' bad' : '') + (busy ? ' busy' : '');
}

/* The figure set arriving.
 *
 * Scene calls go out together and come back out of order, so this is not a progress bar: it is
 * one card per planned figure that fills in when its own call lands. The SVG is fetched the
 * moment the server says it is on disk, which is well before the run has finished.
 */
const WAITING = {
  pending: 'queued',
  generating: 'working out the scene',
  drawing: 'scene ready',
  failed: 'could not be drawn',
};

function renderLive(figures) {
  const box = $('live');
  if (!figures.length) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');

  const wanted = figures.map(f => f.label).join('|');
  if (box.dataset.shape !== wanted) {
    box.dataset.shape = wanted;
    box.innerHTML = figures.map(f => `
      <div class="livecard" data-label="${escapeHtml(f.label)}" data-state="${escapeHtml(f.state)}">
        <div class="thumb"><span class="waiting"></span></div>
        <div class="bar"><i></i></div>
        <div class="foot"><b>${escapeHtml(f.label)}</b>
          <span class="muted">${escapeHtml((f.kind || '').replace(/_/g, ' '))}</span>
          <span class="k"></span></div>
      </div>`).join('');
  }

  for (const f of figures) {
    const card = box.querySelector(`.livecard[data-label="${cssEscape(f.label)}"]`);
    if (!card) continue;
    card.dataset.state = f.state;
    card.querySelector('.bar').style.visibility =
      (f.state === 'done' || f.state === 'failed') ? 'hidden' : 'visible';
    card.querySelector('.k').textContent = f.seconds ? f.seconds.toFixed(0) + 's' : '';
    const thumb = card.querySelector('.thumb');
    if (f.ready) {
      const src = `${PREFIX}/api/job/${JOB_ID}/fig/${slugOf(f.label)}.svg`;
      if (!thumb.querySelector('img')) {
        thumb.innerHTML = `<img alt="${escapeHtml(f.label)}" src="${src}">`;
      }
    } else {
      const note = thumb.querySelector('.waiting');
      if (note) note.textContent = f.detail || WAITING[f.state] || f.state;
    }
  }
}

function slugOf(label) {
  return String(label).replace(/\./g, '').replace(/\s+/g, '').toLowerCase();
}

function renderSteps(steps) {
  $('steps').innerHTML = steps.map(s => `
    <li data-state="${s.state}">
      <span class="dot"></span>
      <span class="name">${escapeHtml(s.name)}</span>
      <span class="detail">${escapeHtml(s.detail || '')}</span>
      <span class="secs">${s.seconds ? s.seconds.toFixed(0) + 's' : ''}</span>
    </li>`).join('');
}

/* -------------------------------------------------------------------------------- loading */

async function load() {
  const res = await fetch(`${PREFIX}/api/job/${JOB_ID}/data`);
  if (!res.ok) { setStatus('the results could not be read', true); return; }
  const data = await res.json();
  state.figures = data.figures || [];
  state.registry = data.registry || { entries: [] };
  state.report = data.report || { findings: [] };
  state.plan = data.plan || { figures: [] };
  state.sheets = data.sheets || [];
  state.rules = data.rules || {};

  $('progress').classList.add('hidden');
  $('result').classList.remove('hidden');
  $('pdf').href = `${PREFIX}/api/job/${JOB_ID}/drawings.pdf`;
  $('zip').href = `${PREFIX}/api/job/${JOB_ID}/bundle.zip`;
  $('redline').href = `${PREFIX}/api/job/${JOB_ID}/redline.html`;

  renderFigureBar();
  showFigure(0);
  renderReport();
  renderRegistry();
  renderPlan();
  renderSheets();
}

/* ------------------------------------------------------------------------------- rendering */

function renderFigureBar() {
  $('figbar').innerHTML = state.figures.map((f, i) => `
    <button data-index="${i}" aria-selected="${i === state.current}">
      ${escapeHtml(f.label)}<span class="kind">${escapeHtml(f.kind.replace(/_/g, ' '))}</span>
    </button>`).join('');
  $('figbar').querySelectorAll('button').forEach(btn =>
    btn.addEventListener('click', () => showFigure(parseInt(btn.dataset.index, 10))));
}

function showFigure(index) {
  state.current = index;
  $('figbar').querySelectorAll('button').forEach(b =>
    b.setAttribute('aria-selected', String(parseInt(b.dataset.index, 10) === index)));
  const figure = state.figures[index];
  if (!figure) return;
  $('canvas').innerHTML = figure.svg;
  wireCanvas();
  renderRegistry();
}

function renderSheets() {
  $('sheets').innerHTML = state.sheets.map(s => `
    <a href="${PREFIX}/api/job/${JOB_ID}/sheet/${s.number}.svg" target="_blank" rel="noopener">
      <img src="${PREFIX}/api/job/${JOB_ID}/sheet/${s.number}.svg" alt="sheet ${s.number}">
      <span class="cap">Sheet ${s.number} of ${s.total} &middot;
        ${s.placed.map(p => escapeHtml(p.label)).join(', ')}</span>
    </a>`).join('') || '<p class="muted small">No sheets.</p>';
}

function renderReport() {
  const findings = state.report.findings || [];
  const errors = findings.filter(f => f.severity === 'error');
  const warnings = findings.filter(f => f.severity === 'warning');
  const info = findings.filter(f => f.severity === 'info');
  $('tally').innerHTML =
    (errors.length ? `<span class="e">${errors.length} error${errors.length > 1 ? 's' : ''}</span>`
                   : '<span class="ok">every check passes</span>') +
    (warnings.length ? `<span class="w">${warnings.length} warning${warnings.length > 1 ? 's' : ''}</span>` : '') +
    (info.length ? `<span>${info.length} note${info.length > 1 ? 's' : ''}</span>` : '');

  $('findings').innerHTML = findings.map(f => {
    const rule = state.rules[f.code] || {};
    const basis = f.basis === 'practice' ? ' &middot; <span class="basis">drafting practice</span>' : '';
    const where = [f.figure, f.numeral].filter(Boolean).join(' &middot; ');
    return `<div class="finding ${f.severity}">
      ${where ? `<strong>${escapeHtml(where)}</strong> ` : ''}${escapeHtml(f.message)}
      <span class="cite">${escapeHtml(f.cite || rule.cite || '')}${basis}
        ${f.stage ? ' &middot; ' + escapeHtml(f.stage) : ''}</span>
    </div>`;
  }).join('') || '<p class="muted small">Nothing to report.</p>';

  const checked = state.report.checked || [];
  $('checked').textContent = checked.length
    ? `${checked.length} checks run: ${checked.join('; ')}.` : '';
}

function renderRegistry() {
  const here = new Set((state.figures[state.current] || {}).numerals || []);
  $('registry').innerHTML = (state.registry.entries || []).map(e => `
    <tr class="${here.has(e.numeral) ? 'here' : ''}" data-numeral="${escapeHtml(e.numeral)}">
      <td class="num">${escapeHtml(e.numeral)}</td>
      <td class="term" contenteditable="true"
          data-numeral="${escapeHtml(e.numeral)}">${escapeHtml(e.term)}</td>
      <td class="figs">${escapeHtml((e.figures || []).join(' '))}</td>
    </tr>`).join('') || '<tr><td class="muted">Empty.</td></tr>';

  $('registry').querySelectorAll('.term[contenteditable]').forEach(cell => {
    cell.addEventListener('blur', () => saveTerm(cell));
    cell.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') { ev.preventDefault(); cell.blur(); }
    });
  });
  $('registry').querySelectorAll('tr[data-numeral]').forEach(row => {
    row.addEventListener('mouseenter', () => highlight(row.dataset.numeral, true));
    row.addEventListener('mouseleave', () => highlight(row.dataset.numeral, false));
  });
}

function renderPlan() {
  const rows = (state.plan.figures || []).map(f => `
    <div style="margin-bottom:.7rem">
      <strong>${escapeHtml(f.label)}</strong>
      <span class="muted small">${escapeHtml(f.kind.replace(/_/g, ' '))}</span><br>
      <span class="small">${escapeHtml(f.title || '')}</span><br>
      <span class="small mono muted">${(f.elements || []).map(e => escapeHtml(e.numeral)).join(' ')}</span>
    </div>`).join('');
  const brief = (state.plan.proposed_brief_description || [])
    .map(line => `<li class="small">${escapeHtml(line)}</li>`).join('');
  const notes = (state.plan.notes || [])
    .map(line => `<li class="small">${escapeHtml(line)}</li>`).join('');
  $('plan').innerHTML = rows +
    (brief ? `<h3 style="margin-top:1rem">Proposed brief description</h3><ul>${brief}</ul>` : '') +
    (notes ? `<h3 style="margin-top:1rem">For you to decide</h3><ul>${notes}</ul>` : '');
}

function highlight(numeral, on) {
  $('canvas').querySelectorAll(`[data-owner="${cssEscape(numeral)}"]`)
    .forEach(node => node.classList.toggle('hl', on));
}

/* ---------------------------------------------------------------------------------- drag */

function svgPoint(svg, ev) {
  const matrix = svg.getScreenCTM();
  if (!matrix) return null;
  const point = svg.createSVGPoint();
  point.x = ev.clientX;
  point.y = ev.clientY;
  return point.matrixTransform(matrix.inverse());
}

function wireCanvas() {
  const svg = $('canvas').querySelector('svg');
  if (!svg) return;
  const dx = parseFloat(svg.dataset.dx || '0');
  const dy = parseFloat(svg.dataset.dy || '0');
  const label = svg.dataset.figure;

  let drag = null;

  const start = (node, kind) => (ev) => {
    if (state.busy) return;
    ev.preventDefault();
    drag = { node, kind, numeral: node.dataset.owner };
    node.classList.add('dragging');
    svg.setPointerCapture?.(ev.pointerId);
  };

  svg.querySelectorAll('.fm-numeral').forEach(node =>
    node.addEventListener('pointerdown', start(node, 'move')));
  svg.querySelectorAll('.fm-tip').forEach(node =>
    node.addEventListener('pointerdown', start(node, 'retarget')));

  svg.addEventListener('pointermove', ev => {
    if (!drag) return;
    const at = svgPoint(svg, ev);
    if (!at) return;
    if (drag.kind === 'move') {
      drag.node.setAttribute('x', at.x.toFixed(3));
      drag.node.setAttribute('y', at.y.toFixed(3));
    } else {
      drag.node.setAttribute('cx', at.x.toFixed(3));
      drag.node.setAttribute('cy', at.y.toFixed(3));
    }
    drag.at = at;
  });

  const finish = async (ev) => {
    if (!drag) return;
    const current = drag;
    drag = null;
    current.node.classList.remove('dragging');
    if (!current.at) return;
    const endpoint = current.kind === 'move' ? 'move' : 'retarget';
    await post(endpoint, {
      figure: label,
      numeral: current.numeral,
      x: current.at.x - dx,
      y: current.at.y - dy,
    });
  };
  svg.addEventListener('pointerup', finish);
  svg.addEventListener('pointercancel', finish);
  svg.addEventListener('pointerleave', finish);
}

/* -------------------------------------------------------------------------------- server */

async function post(path, body) {
  setStatus('checking...', false, true);
  state.busy = true;
  try {
    const res = await fetch(`${PREFIX}/api/job/${JOB_ID}/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (data.figure) {
      const index = state.figures.findIndex(f => f.label === data.figure.label);
      if (index >= 0) {
        state.figures[index] = { ...state.figures[index], ...data.figure };
        if (index === state.current) { $('canvas').innerHTML = data.figure.svg; wireCanvas(); }
      }
    }
    if (data.registry) state.registry = data.registry;
    if (data.report) { state.report = data.report; renderReport(); }
    renderRegistry();
    refreshSheets();
    const errors = (state.report.findings || []).filter(f => f.severity === 'error').length;
    setStatus(errors ? `${errors} error${errors > 1 ? 's' : ''}` : 'every check passes',
              errors > 0);
  } catch (e) {
    setStatus(e.message, true);
  } finally {
    state.busy = false;
  }
}

function refreshSheets() {
  const stamp = Date.now();
  $('sheets').querySelectorAll('img').forEach(img => {
    img.src = img.src.split('?')[0] + '?t=' + stamp;
  });
}

async function saveTerm(cell) {
  const term = cell.textContent.trim();
  const numeral = cell.dataset.numeral;
  const entry = (state.registry.entries || []).find(e => e.numeral === numeral);
  if (!entry || entry.term === term || !term) { if (entry) cell.textContent = entry.term; return; }
  await post('term', { numeral, term });
}

function setStatus(message, bad, busy) {
  const node = $('editstatus');
  node.textContent = message;
  node.className = 'status' + (bad ? ' bad' : '') + (busy ? ' busy' : '');
}

/* --------------------------------------------------------------------------------- setup */

document.querySelectorAll('.side .tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.side .tabs button').forEach(b =>
      b.setAttribute('aria-selected', String(b === btn)));
    document.querySelectorAll('.side [data-pane]').forEach(p =>
      p.classList.toggle('hidden', p.dataset.pane !== btn.dataset.pane));
  });
});

$('approve').addEventListener('click', async () => {
  $('approve').disabled = true;
  setGate('starting...', false, true);
  try {
    const res = await fetch(`${PREFIX}/api/job/${JOB_ID}/approve`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    $('gate').classList.add('hidden');
    $('progress').classList.remove('hidden');
    if (data.changed_figures && data.changed_figures.length) {
      setStatus(`regenerating ${data.changed_figures.join(', ')}`);
    }
    poll();
  } catch (e) {
    setGate(e.message, true);
    $('approve').disabled = false;
  }
});

$('addsrc').addEventListener('change', async (ev) => {
  if (!ev.target.files.length) return;
  const form = new FormData();
  for (const file of ev.target.files) form.append('sources', file);
  setGate('reading...', false, true);
  try {
    const res = await fetch(`${PREFIX}/api/job/${JOB_ID}/sources`, { method: 'POST', body: form });
    const data = await res.json();
    state.sources = (data.sources || {}).items || [];
    renderGate();
    setGate(data.problems && data.problems.length ? data.problems.join('; ') : '',
            Boolean(data.problems && data.problems.length));
  } catch (e) { setGate(e.message, true); }
  ev.target.value = '';
});

$('resolve').addEventListener('click', () => {
  const figure = state.figures[state.current];
  if (figure) post('resolve', { figure: figure.label });
});

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function cssEscape(value) {
  return String(value).replace(/["\\]/g, '\\$&');
}

poll();
