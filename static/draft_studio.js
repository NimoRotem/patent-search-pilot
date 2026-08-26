/* The drafting studio.
 *
 * One state object, one render.  The page is painted from the same JSON the poller fetches, so
 * there is no server-rendered first paint to drift out of step with the live one, and a reload
 * during a drafting turn shows exactly what a poll would have shown.
 *
 * A turn is minutes long.  Everything that happens during one is reported as it happens - the
 * stage the worker is in, then the agent's own summary, then the review - because the alternative
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
  let searchPolling = null;
  let pending = [];
  let reviewing = false;
  let drawing = false;
  let drawingEditor = null;
  let refreshSerial = Promise.resolve();
  let C = null;
  let compilerLoading = false;
  const COMPILER_ROUTE = '#/compiler';

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
      cache: 'no-store',
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
    const scope = p.section_heading ? ` · ${esc(p.section_heading)}` : '';
    parts.push(`<div class="msgwho">Drafting agent${scope}${p.version_no ?
      ' · updated the draft' : ' · no change to the draft'}</div>`);
    parts.push(`<div class="msgbody">${para(message.body || p.summary)}</div>`);
    if (p.consequences && p.consequences.length) {
      parts.push(`<div class="msgask"><b>Now inconsistent elsewhere</b>${list(p.consequences)}
        <div class="askchips">${p.consequences.map((item, i) =>
          `<button type="button" class="chip askchip" data-q="${esc(item)}">Ask for #${i + 1}</button>`)
          .join('')}</div></div>`);
    }
    if (p.changes && p.changes.length) {
      parts.push(`<details class="msgmore" open><summary>What changed (${p.changes.length})</summary>
        ${list(p.changes, 'changelist')}</details>`);
    }
    if (p.reasoning && p.reasoning.length) {
      parts.push(`<details class="msgmore"><summary>Why - the agent's reasoning this iteration
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
  /* THE DRAFT IS THE ONLY DRAFT.  Nothing here says which version it is, what changed since the
     last one, or what the agent thought about it.  A person reading this pane is reading the
     application they are about to file, and a version banner over the title is the studio talking
     about itself in the middle of a legal document.  Version history has a tab.

     The two boilerplate sections are folded away for the same reason from the other direction:
     "Not applicable." twice is four lines of nothing above the Field of the Disclosure, and it
     still has to be there because a filing without those headings is defective. */
  const BOILERPLATE = ['cross_reference', 'government_support'];

  //  Survives a re-render.  A poll landing mid-sentence must not take the textarea away, and the
  //  unsaved value is the user's, not the server's, so it is restored rather than refetched.
  const sectionUI = { editing: '', draft: {}, asking: '', ask: {}, saving: {}, saved: {} };
  let autosaveTimer = null;

  const ICON_EDIT = `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M13.6 2.9a1.7 1.7 0
    0 1 2.4 2.4l-.9.9-2.4-2.4.9-.9zM11.6 4.9l2.4 2.4-7.2 7.2-3 .6.6-3 7.2-7.2z"/></svg>`;
  const ICON_ASK = `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 3c-3.9 0-7 2.4-7
    5.4 0 1.7 1 3.2 2.6 4.2L5 17l3.7-2a9 9 0 0 0 1.3.1c3.9 0 7-2.4 7-5.4S13.9 3 10 3z"/></svg>`;
  const ICON_MIC = `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 2a2 2 0 0 1 2 2v5a2 2
    0 1 1-4 0V4a2 2 0 0 1 2-2zM5 9a5 5 0 0 0 4 4.9V16H7v1.5h6V16h-2v-2.1A5 5 0 0 0 15 9h-1.5a3.5
    3.5 0 1 1-7 0H5z"/></svg>`;

  const SPEECH = window.SpeechRecognition || window.webkitSpeechRecognition || null;

  function sectionText(key) {
    return ((S.version || {}).sections || {})[key] || '';
  }

  /* Editing is refused, not merely discouraged, while a turn is in flight: the version the agent
     is about to publish would silently replace whatever was typed here. */
  function editingBlocked() {
    return !!S.active_turn;
  }

  function sectionBlock(section) {
    const key = section.key;
    const text = sectionText(key);
    const isClaims = key === 'claims';
    const editing = sectionUI.editing === key;
    const value = editing && sectionUI.draft[key] != null ? sectionUI.draft[key] : text;
    return `<section class="dsec${editing ? ' editing' : ''}" id="sec-${key}" data-key="${key}">
      <div class="dsechead">
        <h3>${esc(section.heading)}</h3>
        <div class="dsectools">
          <button type="button" class="dsecbtn dsecedit" data-key="${key}"
            title="Edit ${esc(section.heading)} by hand"
            aria-label="Edit ${esc(section.heading)} by hand">${ICON_EDIT}</button>
          <button type="button" class="dsecbtn dsecask" data-key="${key}"
            title="Ask the drafting agent to change ${esc(section.heading)}"
            aria-label="Ask the drafting agent to change ${esc(section.heading)}">${ICON_ASK}</button>
        </div>
      </div>
      ${sectionUI.asking === key ? askBox(key, section.heading) : ''}
      ${editing ? `<div class="dsecedit-box">
        <textarea class="dsecinput" data-key="${key}" spellcheck="true"
          aria-label="${esc(section.heading)}">${esc(value)}</textarea>
        <div class="dsecbar">
          <button type="button" class="btn sm dsecsave" data-key="${key}">Save</button>
          <button type="button" class="btn ghost sm dsecdone" data-key="${key}">Done</button>
          <span class="small dsecmsg" data-key="${key}" role="status">${
            esc(sectionUI.saved[key] || 'Saves as you type.')}</span>
        </div></div>`
      : `<div class="dsectext ${isClaims ? 'claims' : ''}">${
          text.trim() ? (isClaims ? claimsHtml(text) : citeHtml(text))
                      : '<p class="muted">This section is empty.</p>'}</div>`}
    </section>`;
  }

  function askBox(key, heading) {
    return `<div class="dsecaskbox">
      <textarea class="dsecaskinput" data-key="${key}" rows="2" maxlength="4000"
        placeholder="What should change in ${esc(heading)}? For example: also cover the tool moving under its own power, not only pushed by hand."
        >${esc(sectionUI.ask[key] || '')}</textarea>
      <div class="dsecaskbar">
        ${SPEECH ? `<button type="button" class="dsecmic" data-key="${key}"
          title="Dictate" aria-label="Dictate">${ICON_MIC}</button>` : ''}
        <span class="small muted">The agent reads the whole application and changes only this
          section.</span>
        <span class="grow"></span>
        <span class="small dsecaskmsg" data-key="${key}" role="status"></span>
        <button type="button" class="btn ghost sm dsecaskclose" data-key="${key}">Close</button>
        <button type="button" class="btn sm dsecasksend" data-key="${key}">Send</button>
      </div></div>`;
  }

  function renderDraft() {
    const body = $('draftBody');
    //  A poll landing mid-sentence must not rebuild the pane and take the caret with it. An open
    //  editor with unsaved text owns this pane until it is closed, which is also the only state
    //  where the server's copy is not the newer one.
    if (sectionUI.editing && sectionUI.draft[sectionUI.editing] != null) return;
    if (!S.version || !S.version.sections) {
      body.innerHTML = `<div class="emptypane"><h3>No draft yet</h3>
        <p>The agent is writing the first version. It reads your description and every reference
        attached to this project before it writes a word, so the first one takes a few minutes.</p></div>`;
      return;
    }
    //  Document order is kept: the fold sits where those headings belong in the application,
    //  between the title and the field, not hoisted to the top of the pane.
    const parts = [];
    const run = [];
    const flush = () => {
      if (!run.length) return;
      const open = sectionUI.boilerOpen ||
        run.some((item) => sectionUI.editing === item.key || sectionUI.asking === item.key);
      parts.push(`<details class="dboiler"${open ? ' open' : ''}>
        <summary>${run.map((item) => esc(item.heading)).join(' · ')}</summary>
        ${run.map(sectionBlock).join('')}</details>`);
      run.length = 0;
    };
    (S.sections || []).forEach((section) => {
      if (BOILERPLATE.includes(section.key)) { run.push(section); return; }
      flush();
      parts.push(sectionBlock(section));
    });
    flush();
    body.innerHTML = parts.join('');
    wireDraft();
  }

  function wireDraft() {
    const body = $('draftBody');
    //  Remember the fold, or opening it and then touching anything that repaints closes it again.
    body.querySelectorAll('.dboiler').forEach((fold) => fold.addEventListener(
      'toggle', () => { sectionUI.boilerOpen = fold.open; }));
    body.querySelectorAll('.dsecedit').forEach((button) =>
      button.addEventListener('click', () => toggleEditor(button.dataset.key)));
    body.querySelectorAll('.dsecask').forEach((button) =>
      button.addEventListener('click', () => toggleAsk(button.dataset.key)));
    body.querySelectorAll('.dsecdone').forEach((button) =>
      button.addEventListener('click', () => closeEditor(button.dataset.key)));
    body.querySelectorAll('.dsecsave').forEach((button) =>
      button.addEventListener('click', () => saveSection(button.dataset.key, false)));
    body.querySelectorAll('.dsecaskclose').forEach((button) => button.addEventListener('click', () => {
      sectionUI.asking = ''; renderDraft();
    }));
    body.querySelectorAll('.dsecasksend').forEach((button) =>
      button.addEventListener('click', () => sendSectionRequest(button.dataset.key)));
    body.querySelectorAll('.dsecmic').forEach((button) =>
      button.addEventListener('click', () => dictate(button)));

    const area = body.querySelector('.dsecinput');
    if (area) {
      grow(area);
      area.addEventListener('input', () => {
        sectionUI.draft[area.dataset.key] = area.value;
        grow(area);
        message(area.dataset.key, 'Editing…');
        clearTimeout(autosaveTimer);
        autosaveTimer = setTimeout(() => saveSection(area.dataset.key, true), 2500);
      });
      area.addEventListener('blur', () => {
        clearTimeout(autosaveTimer);
        saveSection(area.dataset.key, true);
      });
      if (sectionUI.focus) { area.focus(); sectionUI.focus = false; }
    }
    const ask = body.querySelector('.dsecaskinput');
    if (ask) {
      grow(ask);
      ask.addEventListener('input', () => { sectionUI.ask[ask.dataset.key] = ask.value; grow(ask); });
      ask.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          sendSectionRequest(ask.dataset.key);
        }
      });
      if (sectionUI.askFocus) { ask.focus(); sectionUI.askFocus = false; }
    }
  }

  function grow(area) {
    area.style.height = 'auto';
    area.style.height = Math.min(1400, Math.max(120, area.scrollHeight + 4)) + 'px';
  }

  function message(key, text, tone) {
    const box = $('draftBody').querySelector(`.dsecmsg[data-key="${key}"]`);
    if (box) { box.textContent = text; box.className = 'small dsecmsg ' + (tone || 'muted'); }
    sectionUI.saved[key] = text;
  }

  function toggleEditor(key) {
    if (sectionUI.editing === key) { closeEditor(key); return; }
    if (editingBlocked()) {
      window.alert('The drafting agent is working on this application. Wait for it to finish, or ' +
        'press Stop, and the section will open for editing.');
      return;
    }
    sectionUI.editing = key;
    sectionUI.asking = '';
    sectionUI.focus = true;
    delete sectionUI.saved[key];
    renderDraft();
    const node = document.getElementById('sec-' + key);
    if (node) node.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  async function closeEditor(key) {
    clearTimeout(autosaveTimer);
    await saveSection(key, true);
    sectionUI.editing = '';
    delete sectionUI.draft[key];
    renderDraft();
  }

  async function toggleAsk(key) {
    //  An open editor holds the pane, so it has to be saved and closed before the ask box can
    //  appear at all. Doing it here rather than refusing keeps the click from doing nothing.
    if (sectionUI.editing) await closeEditor(sectionUI.editing);
    sectionUI.asking = sectionUI.asking === key ? '' : key;
    if (sectionUI.asking) sectionUI.askFocus = true;
    renderDraft();
  }

  /* One save path for the button, the debounce and the blur, so a save can never be skipped
     because it arrived through a different door. */
  async function saveSection(key, quiet) {
    const value = sectionUI.draft[key];
    if (value == null || sectionUI.saving[key]) return;
    if (value.trim() === sectionText(key).trim()) {
      if (!quiet) message(key, 'No change to save.');
      return;
    }
    sectionUI.saving[key] = true;
    message(key, 'Saving…');
    try {
      const data = await api(`/drafts/${PID}/studio/section`, {
        method: 'POST', body: JSON.stringify({ section_key: key, text: value }),
      });
      //  Fold the saved text into the state we already hold rather than refetching the whole
      //  studio: a refresh mid-edit repaints the pane and takes the caret with it.
      if (S.version && S.version.sections) S.version.sections[key] = value.trim();
      if (data.version_no) {
        S.project.latest_version_no = data.version_no;
        S.version.version_no = data.version_no;
      }
      message(key, data.saved ? 'Saved.' : 'No change to save.', data.saved ? 'good' : 'muted');
      renderChrome();
    } catch (error) {
      message(key, error.message, 'bad');
    } finally {
      sectionUI.saving[key] = false;
    }
  }

  async function sendSectionRequest(key) {
    const text = String(sectionUI.ask[key] || '').trim();
    const box = $('draftBody').querySelector(`.dsecaskmsg[data-key="${key}"]`);
    if (!text) { if (box) box.textContent = 'Say what should change.'; return; }
    const button = $('draftBody').querySelector(`.dsecasksend[data-key="${key}"]`);
    if (button) button.disabled = true;
    if (box) { box.textContent = 'Sending…'; box.className = 'small dsecaskmsg muted'; }
    try {
      await api(`/drafts/${PID}/studio/message`, {
        method: 'POST',
        body: JSON.stringify({ message: text, kind: 'section_edit', section_key: key }),
      });
      sectionUI.ask[key] = '';
      sectionUI.asking = '';
      sectionUI.editing = '';
      await refresh();
      startPolling();
    } catch (error) {
      if (box) { box.textContent = error.message; box.className = 'small dsecaskmsg bad'; }
      if (button) button.disabled = false;
    }
  }

  /* Dictation, for the same reason a phone keyboard has a microphone: describing a change to a
     claim out loud is faster than typing it, and the text lands in the box rather than being sent,
     so it can be corrected before it goes. */
  function dictate(button) {
    const key = button.dataset.key;
    if (!SPEECH) return;
    if (dictate.active) { dictate.active.stop(); return; }
    const recognition = new SPEECH();
    recognition.lang = document.documentElement.lang || 'en-US';
    recognition.interimResults = true;
    recognition.continuous = true;
    const before = String(sectionUI.ask[key] || '');
    button.classList.add('on');
    recognition.onresult = (event) => {
      let heard = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        heard += event.results[i][0].transcript;
      }
      const area = $('draftBody').querySelector(`.dsecaskinput[data-key="${key}"]`);
      const joined = (before ? before.replace(/\s*$/, ' ') : '') + heard;
      sectionUI.ask[key] = joined;
      if (area) { area.value = joined; grow(area); }
    };
    recognition.onerror = () => { button.classList.remove('on'); dictate.active = null; };
    recognition.onend = () => { button.classList.remove('on'); dictate.active = null; };
    dictate.active = recognition;
    recognition.start();
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
  function drawingAuditChecks() {
    return (S.figures || []).filter((figure) => figure.drawn).map((figure) => {
      const audit = figure.numeral_audit || {};
      if (!audit.inspected) return {
        name: `${figure.label} pixel numeral check`, status: 'fail', severity: 'error',
        detail: audit.error || 'The visible numerals could not be inspected.', items: [],
        figureMismatch: true,
      };
      const items = [];
      if (audit.missing && audit.missing.length) items.push(`Missing: ${audit.missing.join(', ')}`);
      if (audit.unexpected && audit.unexpected.length) {
        items.push(`Not in draft: ${audit.unexpected.join(', ')}`);
      }
      if (audit.duplicates && audit.duplicates.length) {
        items.push(`Duplicated: ${audit.duplicates.join(', ')}`);
      }
      return {
        name: `${figure.label} pixel numeral check`, status: audit.ok ? 'pass' : 'fail',
        severity: 'error', figureMismatch: !audit.ok, items,
        detail: audit.ok ? 'Visible labels match the current figure specification.' :
          'The numerals detected in the drawing pixels do not match the current draft.',
      };
    });
  }

  function renderReview() {
    const body = $('reviewBody');
    const qa = S.qa;
    const pixelChecks = drawingAuditChecks();
    if (!qa && !pixelChecks.length) {
      body.innerHTML = `<div class="emptypane"><h3>Nothing reviewed yet</h3>
        <p>Every iteration is checked automatically: reference numerals against the text and the
        drawings, claim numbering and dependency, whether each citation resolves to a real
        publication, and whether the claims are supported by what was disclosed.</p></div>`;
      return;
    }
    const hasPixelFailure = pixelChecks.some((check) => check.status === 'fail');
    const [tone, label] = hasPixelFailure ? VERDICT.fail :
      (VERDICT[(qa || {}).verdict] || VERDICT.unknown);
    const checks = pixelChecks.concat((qa || {}).checks || []);
    const findings = (qa || {}).findings || [];
    const order = { fail: 0, warn: 1, pass: 2 };
    checks.sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));
    body.innerHTML = `
      <div class="rvhead">
        <span class="verdict big ${tone}">${label}</span>
        <div><b>${esc(hasPixelFailure ? 'A drawing does not match the current draft.' :
          ((qa || {}).summary || 'Drawing pixels checked.'))}</b>
          <div class="small muted">version ${(qa || {}).version_no || 'current'} · reviewed by
            ${esc((qa || {}).model_name || 'the deterministic drawing check')}${(qa || {}).last_error ?
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
    body.querySelectorAll('.fixchip').forEach((button) => button.addEventListener('click', () => {
      $('chatInput').value = button.dataset.q || '';
      $('chatInput').focus();
    }));
    body.querySelectorAll('.openfigrepair').forEach((button) =>
      button.addEventListener('click', () => showPane('figures')));
  }

  function checkRow(check) {
    const tone = { pass: 'good', warn: 'warn', fail: 'bad' }[check.status] || 'muted';
    const advisory = check.severity === 'advisory'
      ? '<span class="chip tiny">heuristic</span>' : '';
    const numeralMismatch = check.figureMismatch || (check.status === 'fail' && (
      check.name === 'Every drawing numeral appears in the specification' ||
      check.name === 'Every specification numeral appears in a drawing'));
    const repair = numeralMismatch ? `<div class="ffix">
      <button type="button" class="chip openfigrepair">Fix a drawing</button>
      <button type="button" class="chip fixchip" data-q="${esc(
        `Resolve ${check.name.toLowerCase()}: ${(check.items || []).join(', ')}. ` +
        'Change the text only when the inventor disclosure supports the visible part; otherwise change the drawing.')}">
        Ask the agent to fix text</button></div>` : '';
    return `<details class="chk ${tone}"${check.status === 'fail' ? ' open' : ''}>
      <summary><span class="dot"></span><b>${esc(check.name)}</b>${advisory}
        <span class="small">${esc(check.detail)}</span></summary>
      ${list(check.items, 'chkitems')}${repair}</details>`;
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
          data-q="${esc(finding.title + ' - ' + finding.fix)}">Ask for this</button></div>` : ''}
    </article>`;
  }

  // ── drawings ───────────────────────────────────────────────────────────────
  /* The agent writes the final specification for each drawing. The drafting worker generates and
     inspects every sheet before it publishes the text. Controls here are optional revision tools. */
  function renderFigures() {
    // A state poll must not destroy unsaved canvas work. The pane is rebuilt after the editor
    // closes, when there is no local-only bitmap left to preserve.
    if (drawingEditor && document.body.contains(drawingEditor.canvas)) return;
    const figures = S.figures || [];
    $('figuresBody').innerHTML = `
      <div class="photosketch">
        <div><b>Turn a product photo into a drawing</b>
          <div class="small muted">Upload a real product or part. AI removes the background,
          colour, texture, reflections, and logos while preserving visible geometry.</div></div>
        <input type="file" id="photoSketchFile" accept="image/png,image/jpeg,image/webp">
        <input type="text" id="photoSketchCaption" maxlength="400"
          placeholder="View or part, for example: front view of pump housing">
        <button type="button" class="btn ghost sm" id="photoSketchBtn">Make line drawing</button>
        <span class="small" id="photoSketchMsg" role="status"></span>
      </div>
      <div class="drawreconcile">
        <div><b>Re-run every drawing check</b>
          <div class="small muted">Every drafting turn checks all required sheets before it can
            publish. Use this supplemental pass after an audit upgrade or a manual drawing change.
            It checks the current version without running the drafting agent.</div></div>
        <button type="button" class="btn sm" id="figReconcile">Recheck drawings</button>
        <span class="small" id="figReconcileMsg" role="status"></span>
      </div>
      <p class="small muted">Each drawing is checked against the reference numerals actually
        visible in its pixels and against the structures required by its specification.</p>
      ${figures.length ? figures.map(figureCard).join('') :
        `<div class="emptypane"><h3>No checked drawings published yet</h3><p>The active drafting
         turn creates every described sheet automatically before the version becomes available.
         </p></div>`}
      <div id="figureEditorHost"></div>`;
    document.querySelectorAll('.figdraw').forEach((button) =>
      button.addEventListener('click', () => drawFigure(button)));
    document.querySelectorAll('.figedit').forEach((button) =>
      button.addEventListener('click', () => openFigureEditor(Number(button.dataset.figure))));
    document.querySelectorAll('.figdel').forEach((button) =>
      button.addEventListener('click', () => deleteFigure(Number(button.dataset.figure))));
    document.querySelectorAll('.figfix').forEach((button) =>
      button.addEventListener('click', () => fixFigureNumerals(button)));
    document.querySelectorAll('.figfixtext').forEach((button) =>
      button.addEventListener('click', () => fixDraftNumerals(button)));
    document.querySelectorAll('.figversion').forEach((button) =>
      button.addEventListener('click', () => activateFigureVersion(button)));
    $('photoSketchBtn').addEventListener('click', photoToSketch);
    const reconcile = $('figReconcile');
    if (reconcile) reconcile.addEventListener('click', async () => {
      const message = $('figReconcileMsg');
      reconcile.disabled = true;
      message.className = 'small muted';
      message.textContent = 'Drawing and inspecting every sheet. You can keep working on the text.';
      try {
        await api(`/drafts/${PID}/studio/drawings`, { method: 'POST' });
        startPolling();
      } catch (error) {
        message.textContent = error.message;
        message.className = 'small bad';
        reconcile.disabled = false;
      }
    });
  }

  function numeralValue(value) {
    const match = String(value || '').match(/\b([A-Za-z]?\d{1,4}[A-Za-z]?)\b/);
    return match ? match[1].toUpperCase() : '';
  }

  function auditHtml(figure) {
    if (!figure.drawn) return '';
    const audit = figure.numeral_audit || {};
    const semantic = figure.semantic_audit || {};
    const leaders = figure.leader_audit || {};
    if (!semantic.inspected || !semantic.ok) {
      const reasons = (semantic.errors || []).join('; ') ||
        'The image has not passed the automatic specification review.';
      return `<div class="fignumaudit bad"><b>Drawing content check failed.</b>
        ${esc(reasons)}</div>`;
    }
    if (!leaders.inspected || !leaders.ok) {
      const reasons = (leaders.errors || []).join('; ') ||
        'The printed leaders have not been traced to the named drawing features.';
      return `<div class="fignumaudit bad"><b>Leader placement check failed.</b>
        ${esc(reasons)}</div>`;
    }
    if (!audit.inspected) return `<div class="fignumaudit warn"><b>Numeral check unavailable.</b>
      ${esc(audit.error || 'Run an AI redraw or re-save the drawing to inspect it again.')}</div>`;
    if (audit.ok) return `<div class="fignumaudit good"><b>Drawing passed.</b>
      Its visible structure matches the specification, every leader reaches the named feature,
      and every label is exact and unique.</div>`;
    const issues = [];
    if (audit.missing && audit.missing.length) issues.push(`missing: ${audit.missing.join(', ')}`);
    if (audit.unexpected && audit.unexpected.length) {
      issues.push(`not in draft: ${audit.unexpected.join(', ')}`);
    }
    if (audit.duplicates && audit.duplicates.length) {
      issues.push(`duplicated: ${audit.duplicates.join(', ')}`);
    }
    return `<div class="fignumaudit bad"><b>Numerals do not match.</b> ${esc(issues.join(' · '))}
      <button type="button" class="chip figfix" data-figure="${figure.figure_id}">Fix drawing</button>
      <button type="button" class="chip figfixtext" data-figure="${figure.figure_id}">Update draft text</button>
    </div>`;
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
      ${(figure.expected_numerals || figure.numerals || []).length ?
        `<div class="fignums" title="Numerals required by the current draft">${
          (figure.expected_numerals || figure.numerals).map((n) =>
          `<span class="chip tiny">${esc(n)}</span>`).join('')}</div>` : ''}
      ${auditHtml(figure)}
      ${figure.n_versions > 1 ? `<div class="figversions"><span class="small muted">Versions</span>${
        (figure.versions || []).map((version) => `<button type="button" class="chip tiny figversion"
          data-figure="${figure.figure_id}" data-version="${version.version_no}"
          ${Number(version.version_no) === Number(figure.active_version) ? 'disabled' : ''}>v${
            version.version_no}${Number(version.version_no) === Number(figure.active_version) ? ' current' : ''}</button>`).join('')}
        </div>` : ''}
      <div class="figrow2">
        <input type="text" class="figinstr" maxlength="1000"
          placeholder="${figure.drawn ? 'AI change, for example: move the pump into the handle'
                                      : 'Anything to add before it is drawn'}">
        <button type="button" class="btn ghost sm figdraw"
          data-label="${esc(figure.label)}" data-caption="${esc(figure.caption || '')}"
          ${figure.figure_id ? `data-figure="${figure.figure_id}"` : ''}>${
          figure.drawn ? 'Redraw' : 'Draw this figure'}</button>
        ${figure.figure_id ? `<button type="button" class="btn ghost sm figedit"
          data-figure="${figure.figure_id}">Edit by hand</button>
          <button type="button" class="chip figdel" data-figure="${figure.figure_id}">Delete</button>` : ''}
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

  async function photoToSketch() {
    const input = $('photoSketchFile');
    const button = $('photoSketchBtn');
    const message = $('photoSketchMsg');
    if (!input.files.length) { message.textContent = 'Choose a photo first.'; return; }
    const form = new FormData();
    form.append('image', input.files[0]);
    form.append('caption', $('photoSketchCaption').value.trim());
    button.disabled = true;
    message.textContent = 'Removing the photographic detail and drawing clean line art…';
    try {
      const response = await fetch(`${BASE}/drafts/${PID}/studio/photo-to-sketch`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRF-Token': window.CSRF_TOKEN || '' }, body: form,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(data.error || 'Could not convert that photo.');
      input.value = '';
      $('photoSketchCaption').value = '';
      await refresh();
      showPane('figures');
    } catch (error) {
      message.textContent = error.message;
      message.className = 'small bad';
    } finally { button.disabled = false; }
  }

  async function deleteFigure(figureId) {
    if (!window.confirm('Delete this drawing and all of its versions?')) return;
    try {
      await api(`/drafts/${PID}/studio/figure/${figureId}/delete`, { method: 'POST' });
      if (location.hash.includes(`/figures/${figureId}/`)) location.hash = '#/figures';
      await refresh();
      showPane('figures');
    } catch (error) { window.alert(error.message); }
  }

  async function activateFigureVersion(button) {
    button.disabled = true;
    try {
      await api(`/drafts/${PID}/figures/${button.dataset.figure}/activate`, {
        method: 'POST', body: JSON.stringify({ version_no: Number(button.dataset.version) }),
      });
      await refresh(); showPane('figures');
    } catch (error) { button.textContent = error.message; }
  }

  async function fixFigureNumerals(button) {
    const figure = (S.figures || []).find((item) => item.figure_id === Number(button.dataset.figure));
    if (!figure) return;
    const audit = figure.numeral_audit || {};
    const instruction = `Correct reference numerals only. Use exactly ${
      (audit.expected || []).join(', ') || 'no numerals'}; remove extras and duplicates.`;
    const card = button.closest('.figblock');
    const draw = card.querySelector('.figdraw');
    card.querySelector('.figinstr').value = instruction;
    await drawFigure(draw);
  }

  async function fixDraftNumerals(button) {
    const figure = (S.figures || []).find((item) => item.figure_id === Number(button.dataset.figure));
    if (!figure) return;
    const audit = figure.numeral_audit || {};
    const message = `Resolve the reference numeral mismatch for ${figure.label}. The drawing ` +
      `inspection found ${JSON.stringify(audit.detected || [])}; the current draft expects ` +
      `${JSON.stringify(audit.expected || [])}. Update the specification and numeral table only ` +
      `where the visible part is supported by the inventor disclosure. Otherwise remove the ` +
      `unsupported numeral from the drawing.`;
    try {
      await api(`/drafts/${PID}/studio/message`, {
        method: 'POST', body: JSON.stringify({ message, kind: 'qa_fix' }),
      });
      showPane('draft');
      await refresh();
      startPolling();
    } catch (error) { window.alert(error.message); }
  }

  // ── manual drawing editor ─────────────────────────────────────────────────
  function discardDrawingEditor() {
    const host = $('figureEditorHost');
    if (host) host.innerHTML = '';
    drawingEditor = null;
  }

  function openFigureEditor(figureId) {
    const figure = (S.figures || []).find((item) => item.figure_id === Number(figureId));
    if (!figure) return;
    showPane('figures', false);
    if (location.hash !== `#/figures/${figureId}/edit`) {
      location.hash = `#/figures/${figureId}/edit`;
    }
    const host = $('figureEditorHost');
    host.innerHTML = `<section class="draweditor" aria-label="Drawing editor">
      <div class="draweditor-head"><div><b>Edit ${esc(figure.label)}</b>
        <div class="small muted">Every save creates a new version. Undo stays local until save.</div></div>
        <button type="button" class="chip" id="drawClose">Close</button></div>
      <div class="drawtools" role="toolbar" aria-label="Drawing tools">
        <button type="button" class="chip on" data-tool="select">Select and move</button>
        <button type="button" class="chip" data-tool="pen">Pen</button>
        <button type="button" class="chip" data-tool="line">Line</button>
        <button type="button" class="chip" data-tool="erase">Delete tool</button>
        <button type="button" class="chip" data-tool="numeral">Place numeral</button>
        <input type="text" id="drawNumeral" inputmode="numeric" maxlength="5" placeholder="12">
        <button type="button" class="chip" id="drawDeleteSelection">Delete selected area</button>
        <button type="button" class="chip" id="drawUndo">Undo</button>
      </div>
      <div class="drawcanvaswrap"><canvas id="drawCanvas"></canvas><canvas id="drawOverlay"></canvas></div>
      <div class="drawaifix"><input type="text" id="drawAiInstruction" maxlength="1000"
          placeholder="Select an area, then describe what AI should redo in that area">
        <button type="button" class="btn ghost sm" id="drawAi">AI fix selected area</button>
        <button type="button" class="btn sm" id="drawSave">Save new version</button>
        <span class="small" id="drawMsg" role="status"></span></div>
    </section>`;
    host.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setupCanvasEditor(figure);
  }

  function setupCanvasEditor(figure) {
    const canvas = $('drawCanvas');
    const overlay = $('drawOverlay');
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    const octx = overlay.getContext('2d');
    const editor = drawingEditor = {
      figure, canvas, overlay, ctx, octx, tool: 'select', down: false, start: null,
      selection: null, undo: [], before: null, moving: false, pixels: null, ready: false,
    };
    $('drawMsg').textContent = 'Loading drawing…';
    const image = new Image();
    image.onload = () => {
      canvas.width = overlay.width = image.naturalWidth;
      canvas.height = overlay.height = image.naturalHeight;
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(image, 0, 0);
      editor.ready = true;
      $('drawMsg').textContent = '';
    };
    image.onerror = () => { $('drawMsg').textContent = 'The drawing could not be loaded.'; };
    image.src = `${BASE}/drafts/${PID}/figures/${figure.figure_id}.png?version=${figure.active_version}`;

    document.querySelectorAll('.drawtools [data-tool]').forEach((button) => {
      button.addEventListener('click', () => {
        editor.tool = button.dataset.tool;
        document.querySelectorAll('.drawtools [data-tool]').forEach((item) =>
          item.classList.toggle('on', item === button));
      });
    });
    overlay.addEventListener('pointerdown', (event) => editorDown(editor, event));
    overlay.addEventListener('pointermove', (event) => editorMove(editor, event));
    overlay.addEventListener('pointerup', (event) => editorUp(editor, event));
    overlay.addEventListener('pointercancel', (event) => editorUp(editor, event));
    $('drawDeleteSelection').addEventListener('click', () => deleteSelection(editor));
    $('drawUndo').addEventListener('click', () => undoDrawing(editor));
    $('drawSave').addEventListener('click', () => saveDrawing(editor));
    $('drawAi').addEventListener('click', () => aiFixRegion(editor));
    $('drawClose').addEventListener('click', () => { location.hash = '#/figures'; });
  }

  function canvasPoint(editor, event) {
    const box = editor.overlay.getBoundingClientRect();
    return { x: Math.round((event.clientX - box.left) * editor.canvas.width / box.width),
      y: Math.round((event.clientY - box.top) * editor.canvas.height / box.height) };
  }
  function keepUndo(editor, snapshot) {
    editor.undo.push(snapshot || editor.ctx.getImageData(0, 0, editor.canvas.width, editor.canvas.height));
    if (editor.undo.length > 20) editor.undo.shift();
  }
  function clearOverlay(editor) { editor.octx.clearRect(0, 0, editor.overlay.width, editor.overlay.height); }
  function showSelection(editor, rect) {
    clearOverlay(editor);
    if (!rect) return;
    editor.octx.save(); editor.octx.strokeStyle = '#2878ff'; editor.octx.lineWidth = 2;
    editor.octx.setLineDash([8, 6]); editor.octx.strokeRect(rect.x, rect.y, rect.w, rect.h);
    editor.octx.restore();
  }
  function rectFrom(a, b) {
    return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
      w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) };
  }
  function inside(point, rect) {
    return rect && point.x >= rect.x && point.y >= rect.y &&
      point.x <= rect.x + rect.w && point.y <= rect.y + rect.h;
  }
  function movedSelection(editor, point) {
    const rect = editor.selection;
    const x = Math.max(0, Math.min(editor.canvas.width - rect.w,
      rect.x + point.x - editor.start.x));
    const y = Math.max(0, Math.min(editor.canvas.height - rect.h,
      rect.y + point.y - editor.start.y));
    return { ...rect, x, y };
  }

  function editorDown(editor, event) {
    event.preventDefault();
    if (!editor.ready) return;
    editor.overlay.setPointerCapture(event.pointerId);
    const point = canvasPoint(editor, event); editor.down = true; editor.start = point;
    if (editor.tool === 'numeral') {
      const value = numeralValue($('drawNumeral').value);
      if (!value) { $('drawMsg').textContent = 'Enter a reference numeral first.'; editor.down = false; return; }
      keepUndo(editor); editor.ctx.fillStyle = '#000'; editor.ctx.font = '24px Arial';
      editor.ctx.fillText(value, point.x, point.y); editor.down = false; return;
    }
    if (editor.tool === 'select' && inside(point, editor.selection)) {
      keepUndo(editor); editor.before = editor.undo[editor.undo.length - 1];
      editor.pixels = editor.ctx.getImageData(
        editor.selection.x, editor.selection.y, editor.selection.w, editor.selection.h);
      editor.moving = true; return;
    }
    if (editor.tool === 'pen' || editor.tool === 'erase') {
      keepUndo(editor); editor.ctx.beginPath(); editor.ctx.moveTo(point.x, point.y);
      editor.ctx.strokeStyle = editor.tool === 'erase' ? '#fff' : '#000';
      editor.ctx.lineWidth = editor.tool === 'erase' ? 22 : 3;
      editor.ctx.lineCap = 'round'; editor.ctx.lineJoin = 'round';
    } else if (editor.tool === 'line') keepUndo(editor);
  }

  function editorMove(editor, event) {
    if (!editor.down) return;
    const point = canvasPoint(editor, event);
    if (editor.tool === 'pen' || editor.tool === 'erase') {
      editor.ctx.lineTo(point.x, point.y); editor.ctx.stroke(); return;
    }
    if (editor.tool === 'line') {
      clearOverlay(editor); editor.octx.beginPath(); editor.octx.moveTo(editor.start.x, editor.start.y);
      editor.octx.lineTo(point.x, point.y); editor.octx.strokeStyle = '#000'; editor.octx.lineWidth = 3;
      editor.octx.stroke(); return;
    }
    if (editor.tool === 'select' && editor.moving) {
      const moved = movedSelection(editor, point);
      editor.ctx.putImageData(editor.before, 0, 0); editor.ctx.fillStyle = '#fff';
      editor.ctx.fillRect(editor.selection.x, editor.selection.y, editor.selection.w, editor.selection.h);
      editor.ctx.putImageData(editor.pixels, moved.x, moved.y);
      showSelection(editor, moved);
    } else if (editor.tool === 'select') showSelection(editor, rectFrom(editor.start, point));
  }

  function editorUp(editor, event) {
    if (!editor.down) return;
    const point = canvasPoint(editor, event);
    if (editor.tool === 'line') {
      editor.ctx.beginPath(); editor.ctx.moveTo(editor.start.x, editor.start.y);
      editor.ctx.lineTo(point.x, point.y); editor.ctx.strokeStyle = '#000'; editor.ctx.lineWidth = 3;
      editor.ctx.stroke(); clearOverlay(editor);
    } else if (editor.tool === 'select') {
      if (editor.moving) {
        editor.selection = movedSelection(editor, point);
        editor.moving = false;
      } else {
        const rect = rectFrom(editor.start, point);
        editor.selection = rect.w >= 5 && rect.h >= 5 ? rect : null;
      }
      showSelection(editor, editor.selection);
    }
    editor.down = false;
  }

  function deleteSelection(editor) {
    if (!editor.selection) { $('drawMsg').textContent = 'Select an area first.'; return; }
    keepUndo(editor); editor.ctx.fillStyle = '#fff';
    editor.ctx.fillRect(editor.selection.x, editor.selection.y,
      editor.selection.w, editor.selection.h); editor.selection = null; clearOverlay(editor);
  }
  function undoDrawing(editor) {
    const prior = editor.undo.pop(); if (!prior) return;
    editor.ctx.putImageData(prior, 0, 0); editor.selection = null; clearOverlay(editor);
  }

  async function saveDrawing(editor) {
    const message = $('drawMsg'); $('drawSave').disabled = true;
    if (!editor.ready) { message.textContent = 'Wait for the drawing to finish loading.';
      $('drawSave').disabled = false; return; }
    message.textContent = 'Saving and checking visible numerals…';
    try {
      const blob = await new Promise((resolve) => editor.canvas.toBlob(resolve, 'image/png'));
      const form = new FormData(); form.append('image', blob, 'manual-edit.png');
      form.append('instruction', 'Manual canvas edit');
      const response = await fetch(`${BASE}/drafts/${PID}/studio/figure/${editor.figure.figure_id}/manual`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRF-Token': window.CSRF_TOKEN || '' }, body: form,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(data.error || 'Could not save the drawing.');
      discardDrawingEditor();
      location.hash = '#/figures'; await refresh(); showPane('figures');
    } catch (error) { message.textContent = error.message; message.className = 'small bad'; }
    finally { if ($('drawSave')) $('drawSave').disabled = false; }
  }

  async function aiFixRegion(editor) {
    const instruction = $('drawAiInstruction').value.trim();
    const message = $('drawMsg');
    if (!editor.ready) { message.textContent = 'Wait for the drawing to finish loading.'; return; }
    if (!editor.selection || !instruction) {
      message.textContent = 'Select an area and describe the correction first.'; return;
    }
    $('drawAi').disabled = true; message.textContent = 'Redrawing only the selected area…';
    const r = editor.selection;
    try {
      await api(`/drafts/${PID}/studio/figure`, { method: 'POST', body: JSON.stringify({
        figure_id: editor.figure.figure_id, label: editor.figure.label,
        caption: editor.figure.caption, instruction,
        region: [r.x, r.y, r.x + r.w, r.y + r.h],
      }) });
      const figureId = editor.figure.figure_id;
      discardDrawingEditor();
      await refresh();
      if (!drawingEditor || drawingEditor.figure.figure_id !== figureId) openFigureEditor(figureId);
    } catch (error) { message.textContent = error.message; message.className = 'small bad'; }
    finally { if ($('drawAi')) $('drawAi').disabled = false; }
  }

  // ── sources ────────────────────────────────────────────────────────────────
  // ── re-search ──────────────────────────────────────────────────────────────
  /* The rounds are the evidence this feature works, so they are shown as a series rather than as
     a log: what the nearest single reference disclosed of the independent claims, round by round.
     A number that does not fall is left visible and labelled as such; hiding it would make the
     panel an advertisement rather than a measurement. */
  let R = null;
  let researchPolling = null;

  async function loadResearch(force) {
    if (R !== null && !force) return;
    try {
      R = await api(`/api/drafts/${PID}/research`);
    } catch (error) { R = { rounds: [], running: false }; }
    renderResearch();
  }

  function pct(value) {
    return value == null ? '-' : Math.round(Number(value) * 100) + '%';
  }

  function roundRow(item, index, all) {
    const previous = all[index + 1];
    let move = '';
    if (item.closest_coverage != null && previous && previous.closest_coverage != null) {
      const delta = Number(item.closest_coverage) - Number(previous.closest_coverage);
      const tone = delta < -0.001 ? 'good' : delta > 0.001 ? 'bad' : 'muted';
      const sign = delta > 0 ? '+' : '';
      move = `<span class="rsdelta ${tone}">${sign}${Math.round(delta * 100)} pts</span>`;
    }
    const width = item.closest_coverage == null ? 0 : Math.round(item.closest_coverage * 100);
    const busy = !['complete', 'failed'].includes(item.status);
    return `<div class="rsround">
      <div class="rsno">Round ${item.round_no}</div>
      <div class="rsbar" title="${esc(pct(item.closest_coverage))} of the independent claims'
        elements disclosed by the nearest single reference">
        <span style="width:${width}%"></span></div>
      <div class="rsscore">${pct(item.closest_coverage)} ${move}</div>
      <div class="rsmeta small muted">${busy ? esc(item.status) + '…'
        : item.closest_pub
          ? `nearest <b>${esc(item.closest_pub)}</b>${item.n_elements
            ? ` · ${Math.round(item.closest_coverage * item.n_elements)}/${item.n_elements} elements`
            : ''} · ${item.imported_count} attached`
          : 'no reference charted against the claims'}
        ${item.note ? '· ' + esc(item.note) : ''}</div>
    </div>`;
  }

  function renderResearch() {
    const box = document.getElementById('researchBody');
    if (!box) return;
    const list = (R && R.rounds) || [];
    const running = !!(R && R.running);
    const verdict = R && R.improvement;
    box.innerHTML = `<div class="rshead">
        <div><b>Re-search</b>
          <div class="small muted">Searches the draft as it stands, attaches the closest art, and
            has the drafting agent write away from it. Run it again after each round: if the
            drafting is working, the nearest reference should reach less of the claims each time.</div></div>
        <button type="button" class="btn sm" id="rsRun" ${running ? 'disabled' : ''}>${
          running ? 'Round running…' : 'Run a round'}</button>
        <span class="small" id="rsMsg" role="status"></span>
      </div>
      ${list.length ? `<div class="rsrounds">${list.map(roundRow).join('')}</div>` : ''}
      ${verdict && verdict.comparable ? `<div class="rsverdict ${
        verdict.delta < 0 ? 'good' : verdict.delta > 0 ? 'bad' : 'muted'}">
        ${esc(verdict.verdict)} Across ${R.measured} measured round(s) the nearest reference moved
        ${R.delta > 0 ? '+' : ''}${Math.round(R.delta * 100)} points.</div>` : ''}
      ${list.length ? '' : `<p class="small muted">No rounds yet. The first one takes as long as a
        prior-art search, and you can keep working on the text while it runs.</p>`}`;
    const run = document.getElementById('rsRun');
    if (run) run.addEventListener('click', startResearch);
    if (running) startResearchPolling();
  }

  async function startResearch() {
    const button = document.getElementById('rsRun');
    const message = document.getElementById('rsMsg');
    button.disabled = true;
    message.className = 'small muted';
    message.textContent = 'Searching from the current draft…';
    try {
      await api(`/drafts/${PID}/studio/research`, { method: 'POST', body: JSON.stringify({}) });
      await loadResearch(true);
      startResearchPolling();
    } catch (error) {
      message.textContent = error.message;
      message.className = 'small bad';
      button.disabled = false;
    }
  }

  function startResearchPolling() {
    if (researchPolling) return;
    researchPolling = setInterval(async () => {
      await loadResearch(true);
      if (!(R && R.running)) { clearInterval(researchPolling); researchPolling = null; refresh(); }
    }, 15000);
  }

  function renderSources() {
    const references = S.references || [];
    const documents = S.documents || [];
    const searches = S.searches || [];
    const searchRunning = searches.some((item) => item.status === 'running');
    $('sourcesBody').innerHTML = `
      <div class="research" id="researchBody"></div>
      <div class="draftsearch">
        <div><b>Search prior art from this draft</b>
          <div class="small muted">Runs in the background from the current title, summary,
            claims, and description. You stay here while it searches.</div></div>
        <button type="button" class="btn sm" id="draftSearchBtn" ${searchRunning ? 'disabled' : ''}>${
          searchRunning ? 'Search running…' : 'Search current draft'}</button>
        <span class="small" id="draftSearchMsg" role="status"></span>
        ${searches.length ? `<div class="draftsearches">${searches.map((item) => `
          <div class="draftsearchrow" data-slug="${esc(item.slug)}">
            <span class="statuspill status-${esc(item.status)}">${esc(item.status)}</span>
            <span class="small searchmessage">${esc(item.msg || (item.ready ? 'Results ready.' : 'Searching…'))}</span>
            <span class="grow"></span>
            ${item.report_url ? `<a class="small" href="${esc(item.report_url)}"
              target="_blank" rel="noopener">Open report</a>` : ''}
            ${item.ready && !item.imported_count ? `<button type="button" class="chip importsearch"
              data-slug="${esc(item.slug)}">Add top 5 references</button>` : ''}
            ${item.imported_count ? `<span class="small good">${item.imported_count} added</span>` : ''}
          </div>`).join('')}</div>` : ''}
      </div>
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
         alone. Add art here or search the current draft above.</p>`}
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
    $('draftSearchBtn').addEventListener('click', startDraftSearch);
    $('srcPub').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); addReference(); }
    });
    document.querySelectorAll('.srcdel').forEach((button) =>
      button.addEventListener('click', () => removeReference(button.dataset.pub)));
    document.querySelectorAll('.docdel').forEach((button) =>
      button.addEventListener('click', () => removeDocument(button.dataset.id)));
    document.querySelectorAll('.importsearch').forEach((button) =>
      button.addEventListener('click', () => importDraftSearch(button)));
    if (searchRunning) startSearchPolling();
    renderResearch();
    loadResearch(false);
  }

  async function startDraftSearch() {
    const button = $('draftSearchBtn'); const message = $('draftSearchMsg');
    button.disabled = true; button.textContent = 'Starting…';
    try {
      const data = await api(`/drafts/${PID}/studio/search`, {
        method: 'POST', body: JSON.stringify({}),
      });
      S.searches = [data.search].concat(S.searches || []);
      renderSources(); startSearchPolling();
    } catch (error) {
      message.textContent = error.message; message.className = 'small bad';
      button.disabled = false; button.textContent = 'Search current draft';
    }
  }

  async function importDraftSearch(button) {
    button.disabled = true; button.textContent = 'Adding…';
    try {
      const data = await api(`/drafts/${PID}/studio/search/${button.dataset.slug}/import`, {
        method: 'POST', body: JSON.stringify({}),
      });
      button.textContent = `${data.imported} added`;
      await refresh(); showPane('sources');
    } catch (error) { button.textContent = error.message; }
  }

  function startSearchPolling() {
    if (searchPolling) return;
    searchPolling = setInterval(async () => {
      const active = (S.searches || []).filter((item) => item.status === 'running');
      if (!active.length) { clearInterval(searchPolling); searchPolling = null; return; }
      let completed = false;
      await Promise.all(active.map(async (item) => {
        try {
          const state = await api(`/status/${item.slug}`);
          item.msg = state.msg || item.msg; item.ready = !!state.ready; item.done = !!state.done;
          if (state.status === 'error') item.status = 'error';
          if (state.done && state.ready) { item.status = 'complete'; completed = true; }
        } catch (error) { /* another poll will retry; search continues server-side */ }
      }));
      if (completed) await refresh(); else renderSources();
    }, 4000);
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
      message.textContent = `Added ${data.reference.publication_number} - ${
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
          ${version.origin === 'manual' ? '<span class="chip tiny">edited by hand</span>' : ''}
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

  // ── deterministic filing-figure compiler ───────────────────────────────────
  const COMPILER_STAGES = [
    'INGESTED', 'PARSED', 'DISCLOSURE_EXTRACTED', 'MODEL_RECONCILED', 'MODEL_APPROVED',
    'FIGURES_PLANNED', 'MANIFEST_APPROVED', 'FIGURE_SPECS_COMPILED', 'RENDERED',
    'ANNOTATED', 'COMPOSED', 'VALIDATED', 'FINAL_REVIEW', 'APPROVED', 'EXPORTED',
  ];
  const compilerStage = (name) => COMPILER_STAGES.indexOf(name || '');

  async function loadCompiler(force) {
    if (compilerLoading || (C !== null && !force)) return;
    compilerLoading = true;
    const body = $('compilerBody');
    body.innerHTML = '<p class="muted small">Reading the versioned figure artifacts…</p>';
    try {
      C = (await api(`/api/drafts/${PID}/figure-compiler`)).compiler;
      renderCompiler();
    } catch (error) {
      body.innerHTML = `<div class="emptypane"><h3>Compiler unavailable</h3><p>${esc(error.message)}</p></div>`;
    } finally { compilerLoading = false; }
  }

  async function compilerPost(path, payload, button) {
    if (button) button.disabled = true;
    const message = $('compilerMsg');
    if (message) { message.className = 'small muted'; message.textContent = 'Working…'; }
    try {
      C = (await api(`/drafts/${PID}/figure-compiler/${path}`, {
        method: 'POST', body: JSON.stringify(payload || {}),
      })).compiler;
      renderCompiler();
    } catch (error) {
      if (message) { message.className = 'small bad'; message.textContent = error.message; }
      if (button) button.disabled = false;
    }
  }

  function compilerSteps(stage) {
    const at = compilerStage(stage);
    const rows = [
      ['Canonical model', 'MODEL_APPROVED'], ['Figure plan', 'MANIFEST_APPROVED'],
      ['Semantic sheets', 'COMPOSED'], ['Validation', 'FINAL_REVIEW'],
      ['Final approval', 'APPROVED'],
    ];
    return `<ol class="compilersteps">${rows.map(([label, target]) => {
      const done = at >= compilerStage(target);
      const current = !done && rows.findIndex((row) => compilerStage(row[1]) > at) ===
        rows.findIndex((row) => row[0] === label);
      return `<li class="${done ? 'done' : current ? 'current' : ''}"><span>${done ? '✓' : ''}</span>${esc(label)}</li>`;
    }).join('')}</ol>`;
  }

  function compilerRegistry(pir) {
    if (!pir) return '';
    const entities = pir.entities || [];
    const conflicts = (pir.reference_conflicts || []).filter((row) => row.status !== 'resolved');
    const coverage = pir.claim_coverage || [];
    const uncovered = coverage.filter((row) => row.drawable && !row.figure_ids.length);
    return `<section class="compilercard">
      <div class="compilerh"><div><h3>Canonical reference registry</h3>
        <p>${entities.length} disclosed objects · ${pir.relations.length} sourced relations ·
        ${coverage.length} claim limitations</p></div>
        <span class="compilerflag ${conflicts.length ? 'bad' : 'good'}">${conflicts.length ?
          conflicts.length + ' conflict(s)' : 'reconciled'}</span></div>
      ${conflicts.length ? `<div class="compilerissues bad"><b>Material conflicts block compilation.</b>
        ${conflicts.map((row) => `<div><code>${esc(row.reference || row.entity)}</code>
          ${esc((row.candidates || []).join(' / '))} · ${esc(row.status)}
          ${row.kind === 'one_reference_multiple_entities' ? `<span class="compilerchoices">${
            (row.candidates || []).map((choice) => `<button type="button" class="chip compilerresolve"
              data-conflict="${esc(row.id)}" data-choice="${esc(choice)}">Use ${esc(choice)}</button>`).join('')}</span>` :
            '<small>Revise the draft reference table to distinguish these signs.</small>'}</div>`).join('')}</div>` : ''}
      <div class="compilerregistry">${entities.map((entity) => `<div title="${esc(
        (entity.source_span_ids || []).join(', '))}"><b>${esc(entity.reference)}</b>
        <span>${esc(entity.name)}</span><small>${(entity.source_span_ids || []).length} source span(s)</small></div>`).join('')}</div>
      <details class="compilerdetails"><summary>Claim-to-figure coverage · ${uncovered.length ?
        uncovered.length + ' uncovered' : 'all drawable limitations covered'}</summary>
        ${(coverage || []).map((row) => `<div class="coverrow"><code>${esc(row.limitation_id)}</code>
          <span>${row.drawable ? esc((row.figure_ids || []).join(', ') || 'not covered') : 'not drawable'}</span></div>`).join('')}
      </details></section>`;
  }

  function compilerManifest(manifest) {
    if (!manifest) return '';
    return `<section class="compilercard"><div class="compilerh"><div><h3>Figure manifest</h3>
      <p>The smallest proposed set. Approving fixes what may be rendered.</p></div>
      <span class="compilerflag ${manifest.approval ? 'good' : ''}">${manifest.approval ? 'approved' : 'review'}</span></div>
      <div class="manifestgrid">${(manifest.figures || []).map((figure) => `<details class="manifestitem">
        <summary><b>${esc(figure.label)}</b><small>${esc(figure.view_type)} ·
          ${(figure.entity_ids || []).length} objects · ${(figure.relation_ids || []).length} relations</small></summary>
        <p>${esc(figure.caption)}</p></details>`).join('')}</div></section>`;
  }

  function compilerSvgUrl(svg) {
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(String(svg || ''));
  }

  function compilerPackage(packageData, validation, approved) {
    if (!packageData) return '';
    const issues = (validation || {}).issues || [];
    const patchRows = (packageData.figures || []).flatMap((figure) =>
      (figure.labels || []).map((label) => {
        const entity = (figure.entities || []).find((row) => row.entity_id === label.entity_id) || label;
        return `<div class="compilerpatchrow"><b>${esc(figure.label)} · ${esc(label.reference)}</b>
          <select data-patch-type aria-label="Edit ${esc(label.reference)}">
            <option value="move_label" data-x="${esc(label.x)}" data-y="${esc(label.y)}">Move numeral</option>
            <option value="move_entity" data-x="${esc(entity.x)}" data-y="${esc(entity.y)}">Move object</option>
            <option value="reroute_leader" data-x="${esc(label.target_x)}" data-y="${esc(label.target_y)}">Move leader end</option>
            <option value="delete_visible_entity">Delete object</option>
          </select>
          <label>X <input type="number" step="1" value="${esc(label.x)}" data-patch-x></label>
          <label>Y <input type="number" step="1" value="${esc(label.y)}" data-patch-y></label>
          <button type="button" class="chip compilerpatch" data-figure="${esc(figure.id)}"
            data-reference="${esc(label.reference)}">Apply</button></div>`;
      })).join('');
    return `<section class="compilercard"><div class="compilerh"><div><h3>Compiled sheets</h3>
      <p>Semantic vector geometry · ${esc(packageData.renderer_version)} ·
      artifact v${packageData.artifact_version}</p></div>
      <span class="compilerflag ${issues.length ? 'bad' : 'good'}">${issues.length ?
        issues.length + ' issue(s)' : 'valid'}</span></div>
      ${(packageData.sheets || []).map((sheet, index) => `<article class="compilersheet">
        <div class="compilersheetbar"><b>Sheet ${index + 1}</b><span>${esc(sheet.figure_id)}</span>
        ${approved ? `<a href="${BASE}/drafts/${PID}/figure-compiler/export.svg?sheet=${index + 1}">SVG ↓</a>` : ''}</div>
        <div class="compilersvg"><img src="${esc(compilerSvgUrl(sheet.svg))}"
          alt="${esc(sheet.figure_id)} semantic patent drawing"></div></article>`).join('')}
      ${!approved ? `<details class="compilerdetails"><summary>Edit objects, numerals, and leader lines</summary>
        <p class="small muted">Every edit is a typed patch and creates a new artifact version.</p>
        <div class="compilerpatches">${patchRows}</div>
      </details>` : ''}
      ${issues.length ? `<div class="compilerissues bad"><b>Validation does not alter the sheet.</b>
        ${issues.map((issue) => `<div><code>${esc(issue.code)}</code> ${esc(issue.message)}
          <small>${esc(issue.repair_action)}</small></div>`).join('')}</div>` :
        '<div class="compilerissues good"><b>No deterministic blockers.</b> Numerals match the draft in both directions.</div>'}
      ${approved ? `<div class="compilerexports"><a class="btn sm" href="${BASE}/drafts/${PID}/figure-compiler/export.pdf">PDF package ↓</a>
        <span class="small muted">Approved artifacts are immutable.</span></div>` : ''}</section>`;
  }

  function renderCompiler() {
    const body = $('compilerBody');
    if (!body) return;
    if (C === null) {
      body.innerHTML = '<div class="emptypane"><h3>Filing figure compiler</h3><p>Open this tab to load the compiler artifacts.</p></div>';
      return;
    }
    if (!C.run) {
      body.innerHTML = `<div class="compilerintro"><span class="eyebrow">Draft → semantic SVG</span>
        <h2>Compile disclosed facts, not generated pixels.</h2>
        <p>The compiler builds a source-linked object registry, checks claim coverage, asks you to
        approve the model and figure plan, then emits deterministic SVG and PDF sheets.</p>
        <div class="compilerstart"><select id="compilerRules"><option value="uspto-letter-2026.1">USPTO · US Letter</option>
          <option value="pct-a4-2026.1">PCT · A4</option></select>
          <button class="btn sm" id="compilerStart" type="button" ${S.project.latest_version_no ? '' : 'disabled'}>Build from version ${S.project.latest_version_no || '-'}</button>
          <span class="small" id="compilerMsg"></span></div></div>`;
      const start = $('compilerStart');
      if (start) start.addEventListener('click', () => compilerPost('start', {
        ruleset: $('compilerRules').value, version_no: S.project.latest_version_no,
      }, start));
      return;
    }
    const run = C.run;
    const at = compilerStage(run.stage);
    const blockers = ((C.pir || {}).hard_blockers || []).length;
    const finalApproved = at >= compilerStage('APPROVED');
    const stale = Number(run.draft_version_no) !== Number(S.project.latest_version_no);
    let action = '';
    if (run.stage === 'MODEL_RECONCILED') action = `<button class="btn sm" id="compilerApproveModel" ${blockers ? 'disabled' : ''}>Approve canonical model</button>`;
    else if (run.stage === 'FIGURES_PLANNED') action = '<button class="btn sm" id="compilerApproveManifest">Approve figure plan</button>';
    else if (run.stage === 'MANIFEST_APPROVED') action = '<button class="btn sm" id="compilerCompile">Compile + validate</button>';
    else if (C.package && (C.validation || {}).approved_for_export && !finalApproved) action = '<button class="btn sm" id="compilerApproveFinal">Approve filing package</button>';
    body.innerHTML = `<div class="compilerbar"><div><span class="eyebrow">${esc(run.ruleset)}</span>
      <b>Draft version ${run.draft_version_no}</b></div><span class="compilerstage">${esc(run.stage.replace(/_/g, ' '))}</span>
      <span class="grow"></span><button class="btn ghost xs" id="compilerRestart">Rebuild from current draft</button></div>
      ${stale ? '<div class="compilerstale">The draft changed after this run. Rebuild before filing so the registry and sheets use the current text.</div>' : ''}
      ${compilerSteps(run.stage)}${compilerRegistry(C.pir)}${compilerManifest(C.manifest)}
      ${compilerPackage(C.package, C.validation, finalApproved)}
      <div class="compileractions">${action}<span class="small" id="compilerMsg"></span></div>`;
    const actions = [
      ['compilerApproveModel', 'model/approve'], ['compilerApproveManifest', 'manifest/approve'],
      ['compilerCompile', 'compile'], ['compilerApproveFinal', 'approve'],
    ];
    actions.forEach(([id, path]) => { const button = $(id); if (button) button.addEventListener(
      'click', () => compilerPost(path, {}, button)); });
    const restart = $('compilerRestart');
    if (restart) restart.addEventListener('click', () => compilerPost('start', {
      ruleset: run.ruleset, version_no: S.project.latest_version_no,
    }, restart));
    body.querySelectorAll('[data-patch-type]').forEach((select) => select.addEventListener('change', () => {
      const row = select.closest('.compilerpatchrow');
      const option = select.selectedOptions[0];
      const deleting = select.value === 'delete_visible_entity';
      const x = row.querySelector('[data-patch-x]');
      const y = row.querySelector('[data-patch-y]');
      x.disabled = deleting;
      y.disabled = deleting;
      if (!deleting) { x.value = option.dataset.x; y.value = option.dataset.y; }
    }));
    body.querySelectorAll('.compilerpatch').forEach((button) => button.addEventListener('click', () => {
      const row = button.parentElement;
      const type = row.querySelector('[data-patch-type]').value;
      if (type === 'delete_visible_entity' && !window.confirm(
        `Delete visible object ${button.dataset.reference} from this artifact version?`)) return;
      const patch = { type, figure_id: button.dataset.figure,
        reference: button.dataset.reference, reason: 'Manual semantic figure edit' };
      if (type !== 'delete_visible_entity') {
        patch.x = Number(row.querySelector('[data-patch-x]').value);
        patch.y = Number(row.querySelector('[data-patch-y]').value);
      }
      compilerPost('patch', patch, button);
    }));
    body.querySelectorAll('.compilerresolve').forEach((button) => button.addEventListener(
      'click', () => compilerPost('model/resolve', {
        conflict_id: button.dataset.conflict, choice: button.dataset.choice,
      }, button)));
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
        <b>${report.ready ? 'Application text and drawings passed every automated filing check'
                          : report.blockers.length + ' blocker(s) remain'}</b>
        <span class="small">${report.ready ?
          'The downloadable package contains the checked specification, claims, abstract, and drawing sheets.' :
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
            schedule ↗</a> - no amounts are printed here because they change.</li></ul>
      </div>
      <div class="rdy"><h4>Patent Center submission formalities</h4>
        <ul>${(report.remaining || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div>
      <div class="rdyactions">
        <a class="btn" href="${BASE}/drafts/${PID}/download/filing.docx">Download the filing package</a>
        <a class="btn ghost sm" href="${BASE}/drafts/${PID}/download/filing.txt">Plain text</a>
        <a class="btn ghost sm" href="${esc(report.patent_center_url)}" target="_blank"
           rel="noopener">USPTO Patent Center ↗</a>
      </div>`;
  }

  // ── panes ──────────────────────────────────────────────────────────────────
  function showPane(name, updateHash = true) {
    document.querySelectorAll('.stab').forEach((tab) =>
      tab.classList.toggle('on', tab.dataset.pane === name));
    document.querySelectorAll('.spane').forEach((pane) =>
      pane.classList.toggle('on', pane.id === 'pane-' + name));
    if (name === 'filing') renderFiling();
    const targetHash = '#/' + name;
    if (updateHash && location.hash !== targetHash) location.hash = targetHash;
  }
  document.querySelectorAll('.stab').forEach((tab) =>
    tab.addEventListener('click', () => showPane(tab.dataset.pane)));
  $('stFileBtn').addEventListener('click', () => showPane('filing'));

  // ── the model ───────────────────────────────────────────────────────────────
  /* Chosen per project, and it takes effect on the NEXT turn: a turn already in flight is a
     started conversation with a particular model and cannot change tier halfway through. */
  const modelSelect = $('stModel');
  if (modelSelect) {
    modelSelect.addEventListener('change', async () => {
      const previous = S.project.draft_model || '';
      modelSelect.disabled = true;
      try {
        const data = await api(`/drafts/${PID}/studio/model`, {
          method: 'POST', body: JSON.stringify({ model: modelSelect.value }),
        });
        S.project.draft_model = data.draft_model || '';
        modelSelect.value = S.project.draft_model;
        modelSelect.title = `The drafting agent runs on ${data.label} from the next turn.`;
      } catch (error) {
        modelSelect.value = previous;
        window.alert(error.message);
      } finally { modelSelect.disabled = false; }
    });
  }

  function routeFromHash() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    const pane = ['draft', 'review', 'figures', 'sources', 'history', 'filing'].includes(parts[0])
      ? parts[0] : 'draft';
    showPane(pane, false);
    if (pane === 'figures' && parts[2] === 'edit' && /^\d+$/.test(parts[1] || '')) {
      const figureId = Number(parts[1]);
      if (!drawingEditor || drawingEditor.figure.figure_id !== figureId) openFigureEditor(figureId);
    } else if (drawingEditor) {
      discardDrawingEditor();
      renderFigures();
    }
  }
  window.addEventListener('hashchange', routeFromHash);

  // ── composing ──────────────────────────────────────────────────────────────
  const modeButton = $('chatModeBtn');
  modeButton.addEventListener('click', () => {
    const asking = modeButton.dataset.kind === 'revise';
    modeButton.dataset.kind = asking ? 'question' : 'revise';
    modeButton.textContent = asking ? 'Ask a question' : 'Revise the draft';
    modeButton.classList.toggle('asking', asking);
    $('chatInput').placeholder = asking
      ? 'Ask about the draft - the agent answers without changing anything. For example: “Why is the seal in claim 1 at all?”'
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
    queued: 'queued - the drafting agent will pick this up in a moment',
    'applying the change': 'applying the change to this section',
    preparing: 'gathering your disclosure, the prior art and the current draft',
    drafting: 'reading the sources and writing - this is the long part',
    'checking the draft': 'checking the draft before storing it',
    reviewing: 'the reviewer is checking numerals, claims and citations',
    'waiting to retry': 'that attempt failed; trying once more',
    'resuming after a restart': 'resuming after a server restart',
  };

  function renderBusy() {
    const turn = S.active_turn;
    const box = $('chatStatus');
    box.hidden = !turn && !reviewing && !drawing;
    if (turn) {
      //  A queue this application is not at the front of is the difference between "any moment
      //  now" and "after somebody else's drawing repair finishes", and saying which is the whole
      //  point of showing a status line at all.
      const ahead = Number(turn.queue_ahead || 0);
      $('chatStage').textContent = (turn.stage === 'queued' && ahead)
        ? `queued behind ${ahead} other drafting turn${ahead === 1 ? '' : 's'} on this server`
        : (STAGE_TEXT[turn.stage] || turn.stage || 'working…');
    } else if (drawing) {
      $('chatStage').textContent =
        'rechecking every drawing against the current published text';
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
    //  No version number on the page above the application. Every revision is the one draft
    //  that would be filed today; the numbering is bookkeeping and lives in History.
    $('stVersion').textContent = S.project.latest_version_no ? '' : 'no draft yet';
    $('stTitle').textContent = S.project.title;
    const select = $('stModel');
    if (select && document.activeElement !== select) {
      select.value = S.project.draft_model || '';
    }
    const verdictBox = $('stVerdict');
    const pixelFailures = drawingAuditChecks().filter((check) => check.status === 'fail').length;
    if (pixelFailures) {
      const [tone, label] = VERDICT.fail;
      verdictBox.innerHTML = `<span class="verdict ${tone} tiny">${label}: drawing mismatch</span>`;
    } else if (S.qa) {
      const [tone, label] = VERDICT[S.qa.verdict] || VERDICT.unknown;
      verdictBox.innerHTML = `<span class="verdict ${tone} tiny">${label}</span>`;
    } else verdictBox.innerHTML = '';
    const version = S.project.latest_version_no;
    if (version) {
      $('stDownloadMd').href = `${BASE}/drafts/${PID}/download/md?version=${version}`;
      $('stDownloadDocx').href = `${BASE}/drafts/${PID}/download/docx?version=${version}`;
    }
    const counts = (S.qa && S.qa.counts) || {};
    const bad = (counts.checks_failed || 0) + (counts.critical || 0) + pixelFailures;
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
    renderCompiler();
    renderHistory();
    renderBusy();
  }

  function refresh() {
    // Figure generation and polling can finish almost together. Serialising state reads prevents
    // an older in-flight response from repainting the studio after a newer immutable version.
    const job = refreshSerial.catch(() => {}).then(async () => {
      S = await api(`/api/drafts/${PID}/studio`);
      renderAll();
      routeFromHash();
    });
    refreshSerial = job;
    return job;
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
      drawing = !!state.drawing;
      if (!state.busy && !reviewing && !drawing) {
        idle += 1;
        if (idle > 4) { clearInterval(polling); polling = null; }
      } else idle = 0;
    }, 3000);
  }

  renderAll();
  routeFromHash();
  startPolling();
})();
