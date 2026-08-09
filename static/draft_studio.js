/* The drafting studio.
 *
 * One state object, one render.  The page is painted from the same JSON the poller fetches, so
 * there is no server-rendered first paint to drift out of step with the live one, and a reload
 * during a drafting turn shows exactly what a poll would have shown.
 *
 * A turn is minutes long.  Everything that happens during one is reported as it happens — the
 * stage the worker is in, then the agent's own summary, then the review — because the alternative
 * is a spinner that says nothing for four minutes and a page that changes all at once at the end.
 */
(function () {
  'use strict';
  const root = document.querySelector('.studio');
  if (!root) return;
  const BASE = window.APP_BASE || '';
  const PID = root.dataset.project;
  let S = JSON.parse(document.getElementById('studioState').textContent || '{}');
  let lastMessageId = 0;
  let polling = null;
  let pending = [];
  let reviewing = false;

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  /* Paragraphs, not innerHTML of model output: everything the agent writes is escaped first and
     only then given line breaks. */
  function para(text) {
    return String(text || '').split(/\n{2,}/).filter(Boolean)
      .map((block) => `<p>${esc(block).replace(/\n/g, '<br>')}</p>`).join('');
  }
  function list(items, cls) {
    if (!items || !items.length) return '';
    return `<ul class="${cls || ''}">` +
      items.map((i) => `<li>${esc(i)}</li>`).join('') + '</ul>';
  }
  function when(value) {
    if (!value) return '';
    const d = new Date(value);
    return isNaN(d) ? '' : d.toLocaleString(undefined,
      { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  async function api(path, options) {
    const response = await fetch(BASE + path, Object.assign({
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.CSRF_TOKEN || '' },
    }, options || {}));
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }

  // ── conversation ───────────────────────────────────────────────────────────
  const VERDICT = {
    pass: ['good', 'consistent'], warn: ['warn', 'points to settle'],
    fail: ['bad', 'not consistent yet'], unknown: ['muted', 'not reviewed'],
  };

  function agentCard(message) {
    const p = message.payload || {};
    const parts = [];
    parts.push(`<div class="msgwho">Drafting agent${p.version_no ?
      ` · wrote version ${p.version_no}` : ' · no change to the draft'}</div>`);
    parts.push(`<div class="msgbody">${para(message.body || p.summary)}</div>`);
    if (p.changes && p.changes.length) {
      parts.push(`<details class="msgmore" open><summary>What changed (${p.changes.length})</summary>
        ${list(p.changes, 'changelist')}</details>`);
    }
    if (p.reasoning && p.reasoning.length) {
      parts.push(`<details class="msgmore"><summary>Why — the agent's reasoning this iteration
        (${p.reasoning.length})</summary>${list(p.reasoning, 'reasonlist')}</details>`);
    }
    if (p.prior_art_strategy) {
      parts.push(`<details class="msgmore"><summary>How this draft steps around the prior art</summary>
        <div class="msgbody">${para(p.prior_art_strategy)}</div></details>`);
    }
    if (p.questions && p.questions.length) {
      parts.push(`<div class="msgask"><b>The agent needs from you</b>${list(p.questions)}
        <div class="askchips">${p.questions.map((q, i) =>
          `<button type="button" class="chip askchip" data-q="${esc(q)}">Answer #${i + 1}</button>`).join('')}
        </div></div>`);
    }
    if (p.steps && p.steps.length) {
      parts.push(`<details class="msgmore"><summary>The agent's working notes
        (${p.steps.length} steps)</summary><div class="steplog">${p.steps.map(stepLine).join('')}</div></details>`);
    }
    if (p.cost_usd) {
      parts.push(`<div class="msgfoot small muted">${when(message.created_at)} · $${
        Number(p.cost_usd).toFixed(2)}</div>`);
    }
    return parts.join('');
  }

  function stepLine(step) {
    if (step.kind === 'tool') {
      return `<div class="stepr"><span class="steptool">${esc(step.tool)}</span>
        <code>${esc(step.detail)}</code></div>`;
    }
    if (step.kind === 'thinking') {
      return `<div class="stepr think">${esc(step.text).slice(0, 1200)}</div>`;
    }
    if (step.kind === 'error') return `<div class="stepr bad">${esc(step.text)}</div>`;
    return `<div class="stepr say">${esc(step.text)}</div>`;
  }

  function qaCard(message) {
    const p = message.payload || {};
    const [tone, label] = VERDICT[p.verdict] || VERDICT.unknown;
    const c = p.counts || {};
    const bits = [];
    if (c.checks_passed != null) bits.push(`${c.checks_passed}/${c.checks} checks passed`);
    if (c.critical) bits.push(`${c.critical} critical`);
    if (c.major) bits.push(`${c.major} major`);
    if (c.minor) bits.push(`${c.minor} minor`);
    return `<div class="msgwho">Consistency review${p.version_no ?
        ` · version ${p.version_no}` : ''}</div>
      <div class="qaline"><span class="verdict ${tone}">${label}</span>
        <span class="small muted">${esc(bits.join(' · '))}</span></div>
      <div class="msgbody">${para(message.body)}</div>
      ${p.failed && p.failed.length ? list(p.failed, 'failedlist') : ''}
      <button type="button" class="chip openreview">Open the review</button>`;
  }

  function renderFeed() {
    const feed = $('chatFeed');
    const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
    feed.innerHTML = (S.messages || []).map((message) => {
      const cls = 'msg msg-' + message.role;
      if (message.role === 'user') {
        return `<article class="${cls}"><div class="msgbody">${para(message.body)}</div>
          <div class="msgfoot small muted">${when(message.created_at)}</div></article>`;
      }
      if (message.role === 'agent') return `<article class="${cls}">${agentCard(message)}</article>`;
      if (message.role === 'qa') return `<article class="${cls}">${qaCard(message)}</article>`;
      return `<article class="${cls}"><div class="msgbody">${para(message.body)}</div></article>`;
    }).join('');
    feed.querySelectorAll('.askchip').forEach((chip) => chip.addEventListener('click', () => {
      const input = $('chatInput');
      input.value = (input.value ? input.value + '\n' : '') + chip.dataset.q + '\n\n';
      input.focus();
    }));
    feed.querySelectorAll('.openreview').forEach((button) =>
      button.addEventListener('click', () => showPane('review')));
    if (atBottom) feed.scrollTop = feed.scrollHeight;
    const messages = S.messages || [];
    lastMessageId = messages.length ? messages[messages.length - 1].id : 0;
  }

  // ── the draft ──────────────────────────────────────────────────────────────
  function renderDraft() {
    const body = $('draftBody');
    if (!S.version || !S.version.sections) {
      body.innerHTML = `<div class="emptypane"><h3>No draft yet</h3>
        <p>The agent is writing the first version. It reads your description and every reference
        attached to this project before it writes a word, so the first one takes a few minutes.</p></div>`;
      return;
    }
    const sections = S.version.sections;
    body.innerHTML = `<div class="draftinfo small muted">Version ${S.version.version_no}
        ${S.version.change_note ? '· ' + esc(S.version.change_note) : ''}</div>` +
      (S.sections || []).map((section) => {
        const text = sections[section.key] || '';
        const isClaims = section.key === 'claims';
        return `<section class="dsec" id="sec-${section.key}">
          <h3>${esc(section.heading)}</h3>
          <div class="dsectext ${isClaims ? 'claims' : ''}">${
            isClaims ? claimsHtml(text) : citeHtml(text)}</div></section>`;
      }).join('');
  }

  /* Citation tokens are rendered as chips linking to the source, so a reader can check a
     characterisation against the reference in one click rather than scrolling to a list. */
  function citeHtml(text) {
    const byPub = {};
    (S.references || []).forEach((r) => { byPub[r.publication_number] = r; });
    return para(text).replace(/\[REF:([^\]]+)\]/g, (whole, pub) => {
      const key = pub.trim();
      const reference = byPub[key];
      const href = (reference && reference.url) ||
        ('https://patents.google.com/patent/' + key.replace(/-/g, ''));
      return `<a class="citechip" href="${esc(href)}" target="_blank" rel="noopener"
        title="${esc((reference && reference.title) || key)}">${esc(key)}</a>`;
    });
  }

  function claimsHtml(text) {
    return String(text || '').split(/\n(?=\s*\d{1,3}\s*[.)]\s)/)
      .filter((c) => c.trim())
      .map((claim) => `<div class="claim">${esc(claim.trim()).replace(/\n/g, '<br>')}</div>`)
      .join('');
  }

  // ── review ─────────────────────────────────────────────────────────────────
  function renderReview() {
    const body = $('reviewBody');
    const qa = S.qa;
    if (!qa) {
      body.innerHTML = `<div class="emptypane"><h3>Nothing reviewed yet</h3>
        <p>Every iteration is checked automatically: reference numerals against the text and the
        drawings, claim numbering and dependency, whether each citation resolves to a real
        publication, and whether the claims are supported by what was disclosed.</p></div>`;
      return;
    }
    const [tone, label] = VERDICT[qa.verdict] || VERDICT.unknown;
    const checks = qa.checks || [];
    const findings = qa.findings || [];
    const order = { fail: 0, warn: 1, pass: 2 };
    checks.sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));
    body.innerHTML = `
      <div class="rvhead">
        <span class="verdict big ${tone}">${label}</span>
        <div><b>${esc(qa.summary)}</b>
          <div class="small muted">version ${qa.version_no || '—'} · reviewed by
            ${esc(qa.model_name || 'the reviewer')}${qa.last_error ?
              ' · reviewer error: ' + esc(qa.last_error) : ''}</div></div>
        <span class="grow"></span>
        <button type="button" class="btn ghost sm" id="rvRerun">Re-run the review</button>
      </div>
      <h4 class="rvsub">Mechanical checks <span class="small muted">decided in code, the same way
        every time</span></h4>
      <div class="checklist">${checks.map(checkRow).join('')}</div>
      <h4 class="rvsub">Reviewer findings <span class="small muted">judgements, each with the text
        it is about</span></h4>
      ${findings.length ? findings.map(findingRow).join('') :
        '<p class="muted small">The reviewer raised nothing.</p>'}`;
    const rerun = $('rvRerun');
    if (rerun) rerun.addEventListener('click', rerunReview);
  }

  function checkRow(check) {
    const tone = { pass: 'good', warn: 'warn', fail: 'bad' }[check.status] || 'muted';
    const advisory = check.severity === 'advisory'
      ? '<span class="chip tiny">heuristic</span>' : '';
    return `<details class="chk ${tone}"${check.status === 'fail' ? ' open' : ''}>
      <summary><span class="dot"></span><b>${esc(check.name)}</b>${advisory}
        <span class="small">${esc(check.detail)}</span></summary>
      ${list(check.items, 'chkitems')}</details>`;
  }

  function findingRow(finding) {
    return `<article class="finding sev-${esc(finding.severity)}">
      <div class="fhead"><span class="sev">${esc(finding.severity)}</span>
        <b>${esc(finding.title)}</b>
        <span class="small muted">${esc(finding.where)} · ${esc(finding.category)}</span></div>
      <div class="msgbody">${para(finding.detail)}</div>
      <blockquote class="fevidence">${esc(finding.evidence)}</blockquote>
      ${finding.fix ? `<div class="ffix"><b>Suggested fix</b> ${esc(finding.fix)}
        <button type="button" class="chip fixchip"
          data-q="${esc(finding.title + ' — ' + finding.fix)}">Ask for this</button></div>` : ''}
    </article>`;
  }

  // ── drawings ───────────────────────────────────────────────────────────────
  /* The agent writes a SPECIFICATION for each drawing — what view it is, what it shows, which
     numerals appear on it — and that specification is what the review checks against the text.
     The image is drawn from it on request, because an image nobody asked for is five seconds and
     a model call spent on a figure the user was about to reword. */
  function renderFigures() {
    const figures = S.figures || [];
    $('figuresBody').innerHTML = `
      <p class="small muted">Drawn from this draft's own Brief Description of the Drawings and its
        reference numerals, so the numerals on the sheet are the draft's rather than invented.
        Drafting aids only — not formal drawings under 37 CFR 1.84.</p>
      ${figures.length ? figures.map(figureCard).join('') :
        `<div class="emptypane"><h3>No drawings yet</h3><p>The agent writes one specification per
         figure as it drafts. Ask it for the figures this invention needs — for example
         “add an exploded view showing how the ring comes out of the groove”.</p></div>`}`;
    document.querySelectorAll('.figdraw').forEach((button) =>
      button.addEventListener('click', () => drawFigure(button)));
  }

  function figureCard(figure) {
    const src = figure.figure_id
      ? `${BASE}/drafts/${PID}/figures/${figure.figure_id}.png?version=${figure.active_version}`
      : '';
    return `<article class="figblock${figure.orphan ? ' orphan' : ''}">
      <div class="fighead"><b>${esc(figure.label)}</b>
        <span class="small muted">${esc(figure.caption || '')}</span></div>
      ${figure.orphan ? `<div class="small warn">This drawing is no longer described in the
        specification.</div>` : ''}
      ${src ? `<img class="figimg" loading="lazy" alt="${esc(figure.label)}" src="${src}">` : ''}
      ${figure.numerals && figure.numerals.length ?
        `<div class="fignums">${figure.numerals.map((n) =>
          `<span class="chip tiny">${esc(n)}</span>`).join('')}</div>` : ''}
      <div class="figrow2">
        <input type="text" class="figinstr" maxlength="1000"
          placeholder="${figure.drawn ? 'Change something — “move the pump into the handle”'
                                      : 'Anything to add before it is drawn'}">
        <button type="button" class="btn ghost sm figdraw"
          data-label="${esc(figure.label)}" data-caption="${esc(figure.caption || '')}"
          ${figure.figure_id ? `data-figure="${figure.figure_id}"` : ''}>${
          figure.drawn ? 'Redraw' : 'Draw this figure'}</button>
        <span class="small figmsg" role="status"></span>
      </div></article>`;
  }

  async function drawFigure(button) {
    const card = button.closest('.figblock');
    const message = card.querySelector('.figmsg');
    const was = button.textContent;
    button.disabled = true;
    button.textContent = 'Drawing…';
    message.textContent = 'About five seconds.';
    try {
      await api(`/drafts/${PID}/studio/figure`, {
        method: 'POST',
        body: JSON.stringify({
          label: button.dataset.label, caption: button.dataset.caption,
          instruction: card.querySelector('.figinstr').value.trim(),
          figure_id: button.dataset.figure || null,
        }),
      });
      await refresh();
      showPane('figures');
    } catch (error) {
      message.textContent = error.message;
      message.className = 'small figmsg bad';
      button.disabled = false;
      button.textContent = was;
    }
  }

  // ── sources ────────────────────────────────────────────────────────────────
  function renderSources() {
    const references = S.references || [];
    const documents = S.documents || [];
    $('sourcesBody').innerHTML = `
      <div class="srcadd">
        <label for="srcPub">Add prior art by publication number</label>
        <div class="srcrow">
          <input type="text" id="srcPub" placeholder="US-9108319-B2 or EP 3 707 092 B1" maxlength="64">
          <button type="button" class="btn sm" id="srcAdd">Add</button>
        </div>
        <span class="small muted" id="srcMsg">Resolved against the local corpus before it is
          added, so a number that does not exist is refused rather than cited.</span>
      </div>
      <h4 class="rvsub">References <span class="small muted">${references.length}</span></h4>
      ${references.length ? references.map((reference) => `
        <div class="srcitem">
          <div><b>${esc(reference.publication_number)}</b>
            <span class="chip tiny">${esc(reference.origin)}</span>
            <div class="small muted">${esc(reference.title || '')}</div></div>
          <span class="grow"></span>
          ${reference.url ? `<a class="small" href="${esc(reference.url)}" target="_blank"
            rel="noopener">open ↗</a>` : ''}
          <button type="button" class="chip srcdel"
            data-pub="${esc(reference.publication_number)}">remove</button>
        </div>`).join('') :
        `<p class="muted small">No references. The draft is being written from your description
         alone — add art here, or run a prior-art search and start a new draft from it.</p>`}
      <h4 class="rvsub">Uploaded documents <span class="small muted">${documents.length}</span></h4>
      ${documents.length ? documents.map((document_) => `
        <div class="srcitem">
          <div><b>${esc(document_.filename)}</b>
            <span class="chip tiny">${esc(document_.kind.replace('_', ' '))}</span>
            <div class="small muted">${esc(document_.title || '')} ·
              ${Number(document_.chars || 0).toLocaleString()} characters</div></div>
          <span class="grow"></span>
          <button type="button" class="chip docdel" data-id="${document_.id}">remove</button>
        </div>`).join('') : '<p class="muted small">Nothing uploaded.</p>'}`;

    $('srcAdd').addEventListener('click', addReference);
    $('srcPub').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); addReference(); }
    });
    document.querySelectorAll('.srcdel').forEach((button) =>
      button.addEventListener('click', () => removeReference(button.dataset.pub)));
    document.querySelectorAll('.docdel').forEach((button) =>
      button.addEventListener('click', () => removeDocument(button.dataset.id)));
  }

  async function addReference() {
    const input = $('srcPub');
    const message = $('srcMsg');
    const value = input.value.trim();
    if (!value) return;
    message.textContent = 'Looking it up…';
    try {
      const data = await api(`/drafts/${PID}/studio/reference`, {
        method: 'POST', body: JSON.stringify({ publication: value }),
      });
      message.textContent = `Added ${data.reference.publication_number} — ${
        data.reference.title || 'untitled'}.`;
      input.value = '';
      await refresh();
    } catch (error) { message.textContent = error.message; }
  }

  async function removeReference(pub) {
    await api(`/drafts/${PID}/studio/reference`, {
      method: 'POST', body: JSON.stringify({ remove: pub }),
    });
    await refresh();
  }

  async function removeDocument(id) {
    await api(`/drafts/${PID}/studio/document/${id}/delete`, { method: 'POST' });
    await refresh();
  }

  // ── history ────────────────────────────────────────────────────────────────
  function renderHistory() {
    const turns = S.turns || [];
    const versions = S.versions || [];
    $('historyBody').innerHTML = `
      <h4 class="rvsub">Versions</h4>
      ${versions.length ? versions.map((version) => {
        const [tone, label] = VERDICT[version.verdict] || VERDICT.unknown;
        return `<div class="srcitem"><div><b>Version ${version.version_no}</b>
          <span class="verdict ${tone} tiny">${label}</span>
          <div class="small muted">${esc(version.change_note || '')}</div></div>
          <span class="grow"></span>
          <a class="small" href="${BASE}/drafts/${PID}?version=${version.version_no}">edit ↗</a>
          <a class="small" href="${BASE}/drafts/${PID}/download/docx?version=${version.version_no}">Word</a>
        </div>`;
      }).join('') : '<p class="muted small">No versions yet.</p>'}
      <h4 class="rvsub">Iterations</h4>
      ${turns.map((turn) => `<div class="srcitem">
        <div><b>#${turn.turn_no}</b> <span class="chip tiny">${esc(turn.status)}</span>
          <div class="small muted">${esc(turn.summary || turn.stage || '')}</div>
          ${turn.last_error ? `<div class="small bad">${esc(turn.last_error)}</div>` : ''}</div>
        <span class="grow"></span>
        <span class="small muted">${turn.version_no ? 'v' + turn.version_no + ' · ' : ''}${
          turn.duration_ms ? Math.round(turn.duration_ms / 1000) + 's' : ''}${
          turn.cost_usd ? ' · $' + Number(turn.cost_usd).toFixed(2) : ''}</span>
      </div>`).join('')}`;
  }

  // ── filing ─────────────────────────────────────────────────────────────────
  async function renderFiling() {
    const body = $('filingBody');
    body.innerHTML = '<p class="muted small">Checking the draft against the filing requirements…</p>';
    let report;
    try {
      report = (await api(`/api/drafts/${PID}/filing`)).readiness;
    } catch (error) {
      body.innerHTML = `<div class="emptypane"><h3>Not yet</h3><p>${esc(error.message)}</p></div>`;
      return;
    }
    const block = (title, items, tone) => !items || !items.length ? '' :
      `<div class="rdy ${tone}"><h4>${esc(title)}</h4><ul>${items.map((item) =>
        `<li><b>${esc(item.title)}</b> ${esc(item.detail)}${item.items ?
          `<code>${esc(item.items)}</code>` : ''}</li>`).join('')}</ul></div>`;
    const fees = report.fees || {};
    body.innerHTML = `
      <div class="rdyhead ${report.ready ? 'good' : 'bad'}">
        <b>${report.ready ? 'No blockers found in the automated checks'
                          : report.blockers.length + ' blocker(s) remain'}</b>
        <span class="small">${report.ready ?
          'That is not the same as ready to file. Everything under “Still required” has to be done by a person.' :
          'These would leave the application defective or incomplete as filed.'}</span>
      </div>
      ${block('Blockers', report.blockers, 'bad')}
      ${block('Formalities', report.formalities, 'warn')}
      <div class="rdy">
        <h4>Claim counts for the fee calculation</h4>
        <ul><li>${fees.total} claims · ${fees.independent} independent ·
          ${fees.multiple_dependent} multiple dependent
          (counted as ${fees.billable} for fees)</li>
          ${(fees.surcharges || []).map((s) => `<li>${esc(s)}</li>`).join('')}
          <li><a href="${esc(fees.fee_schedule_url)}" target="_blank" rel="noopener">Current fee
            schedule ↗</a> — no amounts are printed here because they change.</li></ul>
      </div>
      <div class="rdy"><h4>Still required — none of this is done for you</h4>
        <ul>${(report.remaining || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div>
      <div class="rdyactions">
        <a class="btn" href="${BASE}/drafts/${PID}/download/filing.docx">Download the filing package</a>
        <a class="btn ghost sm" href="${BASE}/drafts/${PID}/download/filing.txt">Plain text</a>
        <a class="btn ghost sm" href="${esc(report.patent_center_url)}" target="_blank"
           rel="noopener">USPTO Patent Center ↗</a>
      </div>`;
  }

  // ── panes ──────────────────────────────────────────────────────────────────
  function showPane(name) {
    document.querySelectorAll('.stab').forEach((tab) =>
      tab.classList.toggle('on', tab.dataset.pane === name));
    document.querySelectorAll('.spane').forEach((pane) =>
      pane.classList.toggle('on', pane.id === 'pane-' + name));
    if (name === 'filing') renderFiling();
  }
  document.querySelectorAll('.stab').forEach((tab) =>
    tab.addEventListener('click', () => showPane(tab.dataset.pane)));
  $('stFileBtn').addEventListener('click', () => showPane('filing'));

  // ── composing ──────────────────────────────────────────────────────────────
  const modeButton = $('chatModeBtn');
  modeButton.addEventListener('click', () => {
    const asking = modeButton.dataset.kind === 'revise';
    modeButton.dataset.kind = asking ? 'question' : 'revise';
    modeButton.textContent = asking ? 'Ask a question' : 'Revise the draft';
    modeButton.classList.toggle('asking', asking);
    $('chatInput').placeholder = asking
      ? 'Ask about the draft — the agent answers without changing anything. For example: “Why is the seal in claim 1 at all?”'
      : 'Ask for a change, add a fact, or ask a question.';
  });

  $('chatFiles').addEventListener('change', async (event) => {
    pending = Array.from(event.target.files || []);
    renderAttached();
    if (!pending.length) return;
    const form = new FormData();
    pending.forEach((file) => form.append('files', file));
    form.append('kind', 'prior_art');
    $('chatAttached').innerHTML = '<span class="small muted">Reading the documents…</span>';
    try {
      const response = await fetch(`${BASE}/drafts/${PID}/studio/upload`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRF-Token': window.CSRF_TOKEN || '' }, body: form,
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'upload failed');
      pending = [];
      event.target.value = '';
      await refresh();
      $('chatAttached').innerHTML =
        `<span class="small good">${data.documents.length} document(s) added to the sources.
         Say what you want done with them and send.</span>`;
    } catch (error) {
      $('chatAttached').innerHTML = `<span class="small bad">${esc(error.message)}</span>`;
    }
  });

  function renderAttached() {
    const box = $('chatAttached');
    box.hidden = !pending.length;
    box.innerHTML = pending.map((file) => `<span class="chip">${esc(file.name)}</span>`).join('');
  }

  $('chatForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    await send();
  });
  $('chatInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
  });

  async function send() {
    const input = $('chatInput');
    const text = input.value.trim();
    if (!text) return;
    const button = $('chatSend');
    button.disabled = true;
    try {
      await api(`/drafts/${PID}/studio/message`, {
        method: 'POST',
        body: JSON.stringify({ message: text, kind: modeButton.dataset.kind }),
      });
      input.value = '';
      await refresh();
      startPolling();
    } catch (error) {
      $('chatAttached').hidden = false;
      $('chatAttached').innerHTML = `<span class="small bad">${esc(error.message)}</span>`;
    } finally {
      button.disabled = false;
    }
  }

  $('chatCancel').addEventListener('click', async () => {
    const turn = S.active_turn;
    if (!turn) return;
    try {
      await api(`/drafts/${PID}/studio/cancel`, {
        method: 'POST', body: JSON.stringify({ turn_id: turn.id }),
      });
      await refresh();
    } catch (error) { /* the turn finished on its own; the next poll settles it */ }
  });

  async function rerunReview() {
    const button = $('rvRerun');
    button.disabled = true;
    button.textContent = 'Reviewing…';
    try {
      await api(`/drafts/${PID}/studio/review`, { method: 'POST' });
      reviewing = true;
      startPolling();
    } catch (error) {
      button.textContent = error.message;
      button.disabled = false;
    }
  }

  // ── live ───────────────────────────────────────────────────────────────────
  const STAGE_TEXT = {
    queued: 'queued — the drafting agent will pick this up in a moment',
    preparing: 'gathering your disclosure, the prior art and the current draft',
    drafting: 'reading the sources and writing — this is the long part',
    'checking the draft': 'checking the draft before storing it',
    reviewing: 'the reviewer is checking numerals, claims and citations',
    'waiting to retry': 'that attempt failed; trying once more',
    'resuming after a restart': 'resuming after a server restart',
  };

  function renderBusy() {
    const turn = S.active_turn;
    const box = $('chatStatus');
    box.hidden = !turn && !reviewing;
    if (turn) {
      $('chatStage').textContent = STAGE_TEXT[turn.stage] || turn.stage || 'working…';
    } else if (reviewing) {
      $('chatStage').textContent = 'the reviewer is re-checking the current draft';
    }
    $('chatCancel').hidden = !turn;
    $('chatSend').disabled = !!turn;
    $('chatInput').placeholder = turn
      ? 'The agent is working. Your next message will be sent when it finishes.'
      : 'Ask for a change, add a fact, or ask a question.';
  }

  function renderChrome() {
    $('stStatus').textContent = S.project.status;
    $('stStatus').className = 'statuspill status-' + S.project.status;
    $('stVersion').textContent = S.project.latest_version_no
      ? 'version ' + S.project.latest_version_no : 'no draft yet';
    $('stTitle').textContent = S.project.title;
    const verdictBox = $('stVerdict');
    if (S.qa) {
      const [tone, label] = VERDICT[S.qa.verdict] || VERDICT.unknown;
      verdictBox.innerHTML = `<span class="verdict ${tone} tiny">${label}</span>`;
    } else verdictBox.innerHTML = '';
    const version = S.project.latest_version_no;
    if (version) {
      $('stDownloadMd').href = `${BASE}/drafts/${PID}/download/md?version=${version}`;
      $('stDownloadDocx').href = `${BASE}/drafts/${PID}/download/docx?version=${version}`;
    }
    const counts = (S.qa && S.qa.counts) || {};
    const bad = (counts.checks_failed || 0) + (counts.critical || 0);
    $('tabReview').textContent = bad ? bad : '';
    $('tabReview').className = 'tabbadge' + (bad ? ' bad' : '');
    $('tabSources').textContent =
      (S.references || []).length + (S.documents || []).length || '';
    const undrawn = (S.figures || []).filter((f) => !f.drawn).length;
    $('tabFigures').textContent = (S.figures || []).length || '';
    $('tabFigures').title = undrawn ? `${undrawn} figure(s) described but not drawn` : '';
  }

  function renderAll() {
    renderChrome();
    renderFeed();
    renderDraft();
    renderReview();
    renderSources();
    renderFigures();
    renderHistory();
    renderBusy();
  }

  async function refresh() {
    S = await api(`/api/drafts/${PID}/studio`);
    renderAll();
  }

  /* Poll while a turn is in flight, and for a short while after the page loads so a turn started
     in another tab is noticed.  The poll endpoint is small on purpose; the full state is fetched
     only when something has actually changed. */
  function startPolling() {
    if (polling) return;
    let idle = 0;
    polling = setInterval(async () => {
      let state;
      try {
        state = await api(`/api/drafts/${PID}/studio/poll`);
      } catch (error) { return; }
      if (state.turn && S.active_turn && state.turn.stage !== (S.active_turn || {}).stage) {
        S.active_turn = Object.assign({}, S.active_turn, state.turn);
        renderBusy();
      }
      const changed = state.last_message_id !== lastMessageId ||
        state.latest_version_no !== S.project.latest_version_no ||
        (!!state.busy) !== (!!S.active_turn);
      if (changed) { await refresh(); idle = 0; }
      reviewing = !!state.reviewing;
      if (!state.busy && !reviewing) {
        idle += 1;
        if (idle > 4) { clearInterval(polling); polling = null; }
      } else idle = 0;
    }, 3000);
  }

  renderAll();
  startPolling();
})();
