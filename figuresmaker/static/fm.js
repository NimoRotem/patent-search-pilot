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
  if (job.title) $('title').textContent = job.title;

  if (job.status === 'failed') {
    const box = $('failure');
    box.classList.remove('hidden');
    box.innerHTML = `<strong>It stopped.</strong> ${escapeHtml(job.error || 'no reason recorded')}
      <a class="small" href="${PREFIX}/api/job/${JOB_ID}/traceback" target="_blank"
         rel="noopener">traceback</a>`;
    return;
  }
  if (job.status === 'done') { await load(); return; }
  setTimeout(poll, 1500);
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
