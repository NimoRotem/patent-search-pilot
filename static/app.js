/* ============================================================================================
   Prior-art search — front end.

   Two ideas hold the whole file together:

   1. THE SERVER SHIPS A LIST, NOT A LIBRARY. The results page used to inline every reference's
      claims, description and figure manifest, which is why 25 cards weighed 1.6 MB and the
      accessibility tree took minutes to build. Now the server renders only what you can SEE on a
      collapsed card; claims, description, drawings, opinions, citations, per-reference claim
      charts and translations are fetched on demand and cached per publication.

   2. EVERY ASYNC SURFACE HAS TERMINAL STATES. A thumbnail is loading → figure | no-drawing |
      error, and nothing else; it can never sit on a spinner forever, and it is written only by
      the card that owns it (guarded on the publication number), so one card can never display
      another card's drawing.

   No build step, no bundler, no dependencies. Path-prefix aware via window.APP_BASE.
   ============================================================================================ */
const B = (typeof window !== 'undefined' && window.APP_BASE) || '';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const normPub = s => (s || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
/* Zero-pad a publication number the way Google Patents / Espacenet need it — the JS mirror of
   src/pubnorm.py. BigQuery drops the leading zero of the 7-digit US pre-grant serial, so
   US2022153556A1 must become US20220153556A1 or both offices 404. Returns the padded concatenated
   form (with kind code preserved); returns the plain stripped form for anything that is not a US
   pre-grant number, and the raw stripped string when it cannot parse at all. */
const padPub = raw => {
  const t = (raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
  const m = t.match(/^([A-Z]{2})([0-9]{2,})([A-Z][0-9]{0,2})?$/);
  if (!m) return t;
  let cc = m[1], num = m[2], kind = m[3] || '';
  if (cc === 'US' && num.length >= 10 && num.length <= 11) {   // US + YYYY(4) + serial(<=7)
    const y = parseInt(num.slice(0, 4), 10);
    if (y >= 1999 && y <= 2035) {
      const serial = num.slice(4);
      if (serial.length <= 7) num = num.slice(0, 4) + serial.padStart(7, '0');
    }
  }
  return cc + num + kind;
};
/* THE single client-side link-builders. Always prefer these over any server-supplied URL: a
   display record cached before the padding fix still holds the dead dropped-zero form, whereas
   these are rebuilt from the (correct) publication number every time. */
const gp  = pub => 'https://patents.google.com/patent/' + (padPub(pub) || (pub || '').replace(/-/g, '')) + '/en';
const esp = pub => { const p = padPub(pub) || (pub || '').replace(/-/g, '');
  return 'https://worldwide.espacenet.com/patent/search/publication/' + p + '?q=pn%3D' + p; };
const figUrl = (pub, file) => B + '/figures/' + encodeURIComponent(pub) + '/' + encodeURIComponent(file);
/* A figure entry is EITHER a locally-recovered file (served from /figures/<pub>/<file>) OR a
   lemad-Mongo remote entry ({file:null, thumbnail, full} Google-CDN URLs). One accessor each for
   the list/thumbnail size and the full-resolution size, so every render site handles both shapes. */
const figThumb = (pub, im) => (im && im.file) ? figUrl(pub, im.file) : ((im && (im.thumbnail || im.full)) || '');
const figFull  = (pub, im) => (im && im.full) ? im.full : ((im && im.file) ? figUrl(pub, im.file) : ((im && im.thumbnail) || ''));
const FLAGS = { US: '🇺🇸', EP: '🇪🇺', WO: '🌐', DE: '🇩🇪', GB: '🇬🇧', FR: '🇫🇷', JP: '🇯🇵', CN: '🇨🇳', KR: '🇰🇷' };

function fmtDur(ms){
  const s = Math.floor(ms / 1000);
  return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
}

/* ── session expiry ────────────────────────────────────────────────────────────────────────
   Sessions last 30 days, so they lapse rarely -- but when one did, every fetch here just got
   a 401 and swallowed it: panes stayed on their spinner and, worst of all, streamJob's SSE
   dropped to polling and then retried a 401 every 2 s forever, so a run in progress looked
   frozen with no explanation at all.

   Every request goes through window.fetch, so intercept it once here rather than editing a
   dozen call sites (and every future one). On a 401 we show an inline re-auth bar and sign the
   user back in WITHOUT navigating: a redirect to /login would discard the open report, the
   scroll position and any in-flight run. */
let _authPrompted = false;

function _authBar(){
  let el = document.getElementById('authexpired');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'authexpired';
  el.setAttribute('role', 'alertdialog');
  el.setAttribute('aria-labelledby', 'authexpiredmsg');
  el.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:10000;background:#171a21;' +
    'border-top:1px solid #2d3341;color:#e6e8ee;padding:12px 16px;display:flex;gap:10px;' +
    'align-items:center;flex-wrap:wrap;font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif';
  el.innerHTML =
    '<span id="authexpiredmsg">Your session expired. Sign in again to continue — ' +
    'this page and any run in progress are kept.</span>' +
    '<input id="authexpiredpw" type="password" autocomplete="current-password" placeholder="Password" ' +
    'aria-label="Access password" style="padding:7px 10px;border-radius:7px;border:1px solid #2d3341;' +
    'background:#0f1115;color:#e6e8ee;font-size:14px">' +
    '<button type="button" id="authexpiredgo" style="padding:7px 14px;border:0;border-radius:7px;' +
    'background:#3b82f6;color:#fff;font-weight:600;cursor:pointer">Sign in</button>' +
    '<span id="authexpirederr" role="alert" style="color:#f87171"></span>';
  document.body.appendChild(el);

  const submit = async () => {
    const pw = document.getElementById('authexpiredpw').value;
    const err = document.getElementById('authexpirederr');
    err.textContent = '';
    try{
      const body = new URLSearchParams({ password: pw });
      // redirect:'manual' so a 302 (success) is reported as an opaque response rather than
      // being followed and replacing the page we are trying to preserve.
      const r = await window.__rawFetch(B + '/login', {
        method: 'POST', body, redirect: 'manual',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
      });
      if (r.status === 401){ err.textContent = 'Incorrect password.'; return; }
      if (r.status === 429){ err.textContent = 'Too many attempts — wait a few minutes.'; return; }
      el.remove();
      _authPrompted = false;
      document.dispatchEvent(new CustomEvent('auth:restored'));
    }catch(e){ err.textContent = 'Network error.'; }
  };
  document.getElementById('authexpiredgo').addEventListener('click', submit);
  document.getElementById('authexpiredpw').addEventListener('keydown', e => {
    if (e.key === 'Enter') submit();
  });
  return el;
}

function onAuthExpired(){
  if (_authPrompted) return;
  _authPrompted = true;
  _authBar();
  const pw = document.getElementById('authexpiredpw');
  if (pw) pw.focus();
}

if (typeof window !== 'undefined' && window.fetch && !window.__rawFetch){
  window.__rawFetch = window.fetch.bind(window);
  window.fetch = async function(...args){
    const r = await window.__rawFetch(...args);
    // The login POST itself legitimately answers 401 for a wrong password; don't recurse on it.
    const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
    if (r.status === 401 && !url.endsWith('/login')) onAuthExpired();
    return r;
  };
}

/* ── /api/ref cache ────────────────────────────────────────────────────────────────────────
   `light=1` skips the grounded opinion, which is a Vertex call for any reference that has none
   cached yet. Panes that only need text ask for light; the opinion and the full detail view ask
   for the real thing. A light response is upgraded in place once a full one arrives. */
const REFCACHE = {};
const REFPENDING = {};
async function fetchRef(pub, light, rationaleOnly){
  const hit = REFCACHE[pub];
  if (hit && (rationaleOnly ? hit._rationale : (!light ? hit._full : true))) return hit;
  const key = pub + (rationaleOnly ? ':rationale' : (light ? ':light' : ':full'));
  if (REFPENDING[key]) return REFPENDING[key];
  REFPENDING[key] = (async () => {
    const url = B + '/api/ref/' + encodeURIComponent(pub) +
                '?slug=' + encodeURIComponent(window.SLUG || '') + (light ? '&light=1' : '') +
                (rationaleOnly ? '&rationale=1' : '');
    const r = await fetch(url);
    if (!r.ok) throw new Error('ref ' + r.status);
    const j = await r.json();
    j._full = !light;
    j._rationale = !!j.rationale;
    const current = REFCACHE[pub];
    // Information order is full > rationale-only > light. A late, cheaper reply never downgrades
    // a richer cached response.
    if (!current || j._full || (!current._full && j._rationale) ||
        (!current._full && !current._rationale)) REFCACHE[pub] = j;
    // Every card-open path returns the worldwide family — upgrade the card's timeline strip in
    // place (this is also what makes a lazily-opened TAIL card resolve its authoritative family).
    try { if (j.display && j.display.family) upgradeFamily(pub, j.display.family); } catch (e) {}
    return REFCACHE[pub];
  })();
  try { return await REFPENDING[key]; }
  finally { delete REFPENDING[key]; }
}

/* ── semantic search: no query-term highlighting ─────────────────────────────────────────────
   Highlighting exact query words was removed on purpose. Retrieval here is embedding-based
   (dense/chunk cosine), so a card can be strongly relevant without sharing any surface words with
   the query — "vacuum lifter" legitimately surfaces "suction cup". Marking matched words framed
   the tool as lexical and, worse, drew the eye to the WRONG signal on a semantic match. What
   explains a card is the grounded "Why relevant" opinion and the best-matching passage (the
   semantically nearest chunk), not a shared token. hlNode is retained as a no-op so any residual
   caller is harmless. */
function hlNode(){ /* intentionally does nothing — semantic match is not word match */ }

/* ══════════════════════════════════════════════════════════════════════════════════════════
   THUMBNAILS — three terminal states, owner-guarded writes
   ══════════════════════════════════════════════════════════════════════════════════════════
   Direct-first: when build_view already resolved the figure manifest the template emits a real
   <img> and nothing here runs. Otherwise the card sits in state="loading" and is resolved by ONE
   batched /api/figs call for the whole page (disk-only on the server: no enrichment, no LLM).
   Anything still unresolved after that batch is settled explicitly rather than left spinning. */
function thumbFail(thumb){
  thumb.dataset.state = 'error';
  thumb.innerHTML = '<span class="tstate"><span aria-hidden="true">⚠</span>drawing<br>unavailable</span>';
}
function thumbNone(thumb){
  thumb.dataset.state = 'none';
  thumb.innerHTML = '<span class="tstate"><span aria-hidden="true">🗎</span>no drawing<br>available</span>';
}

/* States: loading (manifest in flight) → queued (manifest known) → ok | none | error.
   EAGER, iptorch-style: as soon as a card's figure manifest lands we fetch its hero drawing —
   every card, not just the ones scrolled into view. Figures are cheap CDN URLs (no download),
   so all ~25 heroes can render at first paint. 'queued' is now only a momentary placeholder
   before loadThumb runs, never a resting state waiting on a scroll. */
function setThumb(card, images){
  const thumb = card.querySelector('.rthumb');
  if (!thumb || thumb.dataset.state !== 'loading') return;     // already terminal — never overwrite
  const pub = card.dataset.pub;
  if (thumb.dataset.pub !== pub) return;                       // only the owning card may write
  if (!images || !images.length){ card.dataset.nimg = '0'; thumbNone(thumb); return; }
  card.dataset.nimg = String(images.length);
  thumb._figs = images;
  thumb.dataset.state = 'queued';
  thumb.innerHTML = '<span class="tstate"><span aria-hidden="true">🖼</span>' +
    images.length + ' fig' + (images.length !== 1 ? 's' : '') + '</span>';
  loadThumb(thumb);                                            // eager: load now, don't wait to scroll
}

/* Deliberately NOT loading="lazy" on a detached Image: Chrome will not start the fetch for a lazy
   image that is not in the document, so the old preload never completed and the card sat on a
   spinner. We now drive the fetch eagerly (see setThumb) so every card's hero resolves at first
   paint; the onload/onerror pair keeps it terminal — a figure, or an explicit failure state. */
function loadThumb(thumb){
  if (thumb.dataset.state !== 'queued') return;
  const pub = thumb.dataset.pub, images = thumb._figs || [];
  if (!images.length){ thumbNone(thumb); return; }
  thumb.dataset.state = 'fetching';
  let i = 0;
  const attempt = () => {
    if (thumb.dataset.state !== 'fetching' || thumb.dataset.pub !== pub) return;
    if (i >= images.length){ thumbFail(thumb); return; }
    const n = i++, im = images[n], src = figThumb(pub, im);
    if (!src){ attempt(); return; }
    const img = new Image();
    img.decoding = 'async';
    img.alt = (im.from_pdf ? 'Page' : 'Figure') + ' ' + (n + 1) + ' of ' + pub;
    // A manifest can contain one stale CDN URL followed by valid drawings. Try every candidate;
    // only show "unavailable" once the whole manifest has failed.
    img.onload = () => {
      if (thumb.dataset.state !== 'fetching' || thumb.dataset.pub !== pub) return;
      thumb.dataset.state = 'ok';
      thumb.textContent = '';
      thumb.appendChild(img);
      thumb.insertAdjacentHTML('beforeend',
        '<span class="figbadge">' + images.length + ' fig' + (images.length !== 1 ? 's' : '') + '</span>' +
        '<span class="zoomhint" aria-hidden="true">🔍</span>');
    };
    img.onerror = attempt;
    img.src = src;
  };
  attempt();
}

/* Server-rendered thumbnails arrive before this script. If their first CDN image already failed,
   detect it (including an error that fired before DOMContentLoaded), fetch the cached detail
   manifest, and let loadThumb try its remaining figures. */
function recoverBrokenInitialThumbs(){
  document.querySelectorAll('.rthumb[data-state="ok"]').forEach(thumb => {
    const img = thumb.querySelector('img');
    if (!img) return;
    const recover = () => {
      if (thumb.dataset.state !== 'ok') return;
      thumb.dataset.state = 'error';
      thumbFail(thumb);
      const pub = thumb.dataset.pub;
      fetchRef(pub, true).then(j => {
        if (j && j.display && j.display.images) backfillThumb(pub, j.display.images);
      }).catch(() => {});
    };
    if (img.complete && !img.naturalWidth) recover();
    else img.addEventListener('error', recover, {once:true});
  });
}

/* PDF links are rendered inert; one batched manifest promotes the ones that actually resolve.
   /pdf/<pub> 404s unless a file or a cached pdf_url exists, and most references have neither --
   23 of 34 links on the gold report were dead. Same batching shape as resolveThumbs(). */
async function resolvePdfLinks(){
  const pending = [...document.querySelectorAll('.pdflink[aria-disabled="true"]')];
  if (!pending.length) return;
  for (let i = 0; i < pending.length; i += 40){
    const chunk = pending.slice(i, i + 40);
    const settle = (el, ok) => {
      if (ok){
        const a = document.createElement('a');
        a.href = B + '/pdf/' + encodeURIComponent(el.dataset.pub);
        a.target = '_blank'; a.rel = 'noopener';
        a.textContent = 'PDF';
        a.title = 'Open the cached PDF for ' + el.dataset.pub;
        el.replaceWith(a);
      } else {
        el.title = 'No PDF cached for this reference.';
      }
    };
    try{
      const q = chunk.map(el => el.dataset.pub).join(',');
      const map = await (await fetch(B + '/api/pdfs?pubs=' + encodeURIComponent(q))).json();
      chunk.forEach(el => settle(el, !!map[el.dataset.pub]));
    }catch(e){
      // Manifest failed: leave every link in the chunk inert rather than promoting a guess.
      chunk.forEach(el => settle(el, false));
    }
  }
}

/* One request resolves every unresolved thumbnail on the page. */
async function resolveThumbs(){
  const pending = [...document.querySelectorAll('.refcard')]
    .filter(c => { const t = c.querySelector('.rthumb'); return t && t.dataset.state === 'loading'; });
  if (!pending.length) return;
  for (let i = 0; i < pending.length; i += 40){
    const chunk = pending.slice(i, i + 40);
    try{
      const q = chunk.map(c => c.dataset.pub).join(',');
      const map = await (await fetch(B + '/api/figs?pubs=' + encodeURIComponent(q))).json();
      chunk.forEach(c => setThumb(c, map[c.dataset.pub] || []));
    }catch(e){
      // The batch failed; settle every card in it rather than leaving spinners behind.
      chunk.forEach(c => {
        const t = c.querySelector('.rthumb');
        if (t && t.dataset.state === 'loading'){
          t.dataset.state = 'error';
          t.innerHTML = '<span class="tstate"><span aria-hidden="true">⚠</span>drawing<br>unavailable</span>';
        }
      });
    }
  }
}

/* ══════════════════════════════════════════════════════════════════════════════════════════
   PREFETCH the TOP-N (Feature 2) — resolve drawings + family WITHOUT a click
   ══════════════════════════════════════════════════════════════════════════════════════════
   The server recovers drawings/PDF (Google → EPO OPS → PDF) and the worldwide family only when a
   card is clicked. Here we ask it to do that eagerly for the top-N ranked cards (bounded + throttled
   server-side), then poll the DISK-ONLY /api/figs and /api/family until the worker lands each one —
   so a top-N card's "no drawing available" turns into a figure with no click, while the long tail
   stays lazy. A genuinely drawing-less publication still resolves to an honest "no drawing available"
   (it just resolves proactively). Kicked off AFTER the list is shown, so it never delays render. */
async function prefetchTopN(){
  if (!window.SLUG || window.PARTIAL) return;
  let sched;
  try {
    sched = await (await fetch(B + '/api/prefetch/' + encodeURIComponent(window.SLUG),
                               { method: 'POST' })).json();
  } catch (e) { return; }
  const pubs = (sched && sched.pubs) || [];
  if (!pubs.length) return;
  const q = pubs.map(encodeURIComponent).join(',');
  let tries = 0;
  const sweep = async () => {
    tries++;
    try {
      const figs = await (await fetch(B + '/api/figs?pubs=' + q)).json();
      pubs.forEach(p => { const im = figs[p]; if (im && im.length) backfillThumb(p, im); });
    } catch (e) {}
    try {
      const fams = await (await fetch(B + '/api/family?pubs=' + q)).json();
      pubs.forEach(p => { if (fams[p]) upgradeFamily(p, fams[p]); });
    } catch (e) {}
    let st = null;
    try { st = await (await fetch(B + '/api/prefetch/' + encodeURIComponent(window.SLUG))).json(); } catch (e) {}
    // Stop once the worker reports done (one final sweep has just run) or after a bounded budget
    // of polls, so a wedged fetch can never loop forever.
    if ((st && !st.running) || tries >= 24) return;
    setTimeout(sweep, 2500);
  };
  setTimeout(sweep, 1500);
}

/* ══════════════════════════════════════════════════════════════════════════════════════════
   EAGER DETAIL WARM — every visible card's full detail, iptorch-style
   ══════════════════════════════════════════════════════════════════════════════════════════
   iptorch prefetches EVERY visible card's document in parallel and caches it, so its pill tabs
   open instantly. We do the same: a bounded fan of light /api/ref calls (light=1 skips the Vertex
   opinion and, server-side, reads the family from cache only — so this spends no LLM and no OPS)
   populates REFCACHE for the whole shown set. A subsequent tab click then renders from cache with
   no spinner, and any Mongo figures the call surfaces back-fill the card's thumbnail immediately.
   Kicked off after the list is interactive so it never delays first paint; concurrency-limited so
   it can never swamp the RAM-tight server. */
async function warmDetails(){
  if (window.PARTIAL) return;
  const pubs = [...document.querySelectorAll('.refcard')].map(c => c.dataset.pub).filter(Boolean);
  if (!pubs.length) return;
  let i = 0;
  const worker = async () => {
    while (i < pubs.length){
      const pub = pubs[i++];
      try {
        const j = await fetchRef(pub, true);
        if (j && j.display && j.display.images) backfillThumb(pub, j.display.images);
      } catch (e) {}
    }
  };
  await Promise.all([worker(), worker(), worker()]);   // 3-wide: instant tabs, gentle on the box
}

/* Generate the expensive, full-text-grounded explanation for the highest-ranked cards in the
   background. Previously that deeper read happened only after a user clicked "Why relevant",
   leaving the list itself with a short batch-reranker opinion. Two workers and eight cards bound
   both spend and pressure; fetchRef's pending map also makes a simultaneous click share the call. */
async function warmRationales(){
  if (window.PARTIAL || !window.SLUG) return;
  const pubs = [...document.querySelectorAll('.refcard')]
    .slice(0, 8).map(c => c.dataset.pub).filter(Boolean);
  let i = 0;
  const worker = async () => {
    while (i < pubs.length){
      const pub = pubs[i++];
      try {
        // light=1 keeps worldwide-family lookup cache-only; rationale=1 still performs the deep
        // text analysis. This avoids racing the drawing/family prefetch for the same OPS budget.
        const j = await fetchRef(pub, true, true);
        if (j && j.display && j.display.images) backfillThumb(pub, j.display.images);
        if (j && j.rationale) applyCardRationale(pub, j.rationale);
      } catch (e) {}
    }
  };
  await Promise.all([worker(), worker()]);
}

function rationaleBasis(r){
  const labels = { 'claims+description':'claims + description', claims:'claims',
                   description:'description', 'abstract-only':'abstract only',
                   'title-only':'title only' };
  return labels[(r && r.text_basis) || ''] || ((r && r.text_basis) || 'reference text');
}

function rationaleMeta(r){
  if (!r) return '';
  const bits = [];
  if (r.n_passages) bits.push(r.n_passages + ' passages');
  if (r.text_basis) bits.push(rationaleBasis(r));
  const locs = [...new Set((r.citations || []).map(c => c.label).filter(Boolean))].slice(0, 4);
  if (locs.length) bits.push(locs.join(', '));
  return bits.length ? '<div class="why-meta">Grounded in ' + esc(bits.join(' · ')) + '</div>' : '';
}

function applyCardRationale(pub, r){
  if (!r || !r.why) return;
  const card = document.querySelector('.refcard[data-pub="' + CSS.escape(pub) + '"]');
  if (!card) return;
  const main = card.querySelector('.rmain');
  if (!main) return;
  let p = main.querySelector('.relop');
  if (!p){
    p = document.createElement('p');
    p.className = 'relop';
    const before = main.querySelector('.rcovers');
    if (before) main.insertBefore(p, before); else main.appendChild(p);
  }
  p.dataset.rationaleState = 'full';
  p.innerHTML = '<span class="relop-l">Why relevant</span>' + esc(r.why) + rationaleMeta(r);
}

/* Uploaded-patent Claim x Reference grid. The server starts this analysis as soon as the final
   ranking exists; the page merely polls and paints it. No button click is required. */
function renderQueryClaimGrid(d){
  const body = document.getElementById('queryClaimGridBody');
  const summary = document.getElementById('queryClaimGridSummary');
  if (!body || !summary || !d) return;
  if (d.status === 'error'){
    summary.textContent = 'Background analysis failed';
    body.innerHTML = '<div class="pempty">The claim-level grid could not be completed. The element grid and ranked results are unaffected.</div>';
    return;
  }
  if (d.status === 'unavailable'){
    summary.textContent = 'Not available';
    body.innerHTML = '<div class="pempty">' + esc(d.reason || 'No uploaded claims were available.') + '</div>';
    return;
  }
  if (d.status !== 'done'){
    const nc = d.n_claims || '', nr = d.n_refs || '';
    summary.textContent = 'Building ' + (nc ? nc + ' claims' : 'uploaded claims') +
      (nr ? ' × ' + nr + ' references' : '') + ' in the background…';
    return;
  }
  if (!d.available){
    summary.textContent = 'Not available';
    body.innerHTML = '<div class="pempty">' + esc(d.reason || 'No uploaded claims were available.') + '</div>';
    return;
  }
  const counts = d.counts || {};
  summary.textContent = d.n_claims_shown + ' claims × ' + d.n_refs_shown + ' references · ' +
    (counts.disclosed || 0) + ' disclosed · ' + (d.seconds || 0) + 's background analysis';
  const marks = {disclosed:'✓', partial:'~', uncertain:'?', absent:'—'};
  const cls = {disclosed:'discloses', partial:'weak', uncertain:'unchecked', absent:'unrelated'};
  let h = '<p class="gridline">Each row keeps an uploaded claim intact. A green cell has a grounded quotation and survived a separate refutation pass; it is not a legal claim-construction conclusion. ' +
    '<span class="lgi"><span class="lg lg-ok"></span>✓ disclosed</span>' +
    '<span class="lgi"><span class="lg lg-weak"></span>~ partial</span>' +
    '<span class="lgi"><span class="lg lg-unk"></span>? uncertain</span>' +
    '<span class="lgi"><span class="lg lg-bad"></span>— absent</span></p>';
  if (d.truncated_claims || d.truncated_refs){
    const notes = [];
    if (d.truncated_claims) notes.push('showing ' + d.n_claims_shown + ' of ' + d.n_claims_total + ' claims (independent claims first)');
    if (d.truncated_refs) notes.push('top ' + d.n_refs_shown + ' of ' + d.n_refs_total + ' ranked references');
    h += '<p class="claimgrid-bound">Bounded background analysis: ' + esc(notes.join(' · ')) + '.</p>';
  }
  h += '<div class="chartwrap"><table class="chart query-claim-chart"><caption class="vh">Which ranked references disclose each uploaded patent claim</caption><thead><tr>' +
    '<th class="elh" scope="col">Uploaded claim</th>' +
    (d.columns || []).map(c => '<th scope="col"><button type="button" class="pnlink" data-pub="' + esc(c.pub) +
      '" onclick="jumpRef(this.dataset.pub)">' + esc(c.pub) + '</button><span class="colcount">rank ' + esc(c.rank || '') + '</span></th>').join('') +
    '</tr></thead><tbody>';
  (d.rows || []).forEach(row => {
    const txt = row.text || '', short = txt.length > 210 ? txt.slice(0, 210) + '…' : txt;
    const claimText = txt.length > 210
      ? '<details class="qclaim"><summary>' + esc(short) + '</summary><p>' + esc(txt) + '</p></details>'
      : '<div class="qclaim-short">' + esc(short) + '</div>';
    h += '<tr><th class="elh" scope="row"><span class="qclaim-no">Claim ' + esc(row.claim_no) +
      (row.independent ? ' · independent' : '') + '</span>' + claimText + '</th>';
    (row.cells || []).forEach(cell => {
      const v = marks[cell.verdict] ? cell.verdict : 'uncertain';
      const tip = [v, cell.location, cell.quote].filter(Boolean).join(' · ').slice(0, 600);
      h += '<td class="cell cell-' + cls[v] + '"><button type="button" data-pub="' + esc(cell.pub) +
        '" onclick="jumpRef(this.dataset.pub)" title="' + esc(tip) + '"><span class="cmark">' +
        marks[v] + '</span><span class="cs">' + esc(v) + '</span>' +
        (cell.location ? '<span class="cc">' + esc(cell.location) + '</span>' : '') + '</button></td>';
    });
    h += '</tr>';
  });
  h += '</tbody></table></div>';
  body.innerHTML = h;
}

async function warmQueryClaimGrid(){
  if (window.PARTIAL || !window.SLUG || !document.getElementById('queryClaimGrid')) return;
  let d = null;
  try {
    const r = await fetch(B + '/api/query-claim-grid/' + encodeURIComponent(window.SLUG), {method:'POST'});
    d = await r.json();
    renderQueryClaimGrid(d);
  } catch (e) { return; }
  if (d && (d.status === 'done' || d.status === 'error' || d.status === 'unavailable')) return;
  let tries = 0;
  const poll = async () => {
    tries++;
    try {
      d = await (await fetch(B + '/api/query-claim-grid/' + encodeURIComponent(window.SLUG))).json();
      renderQueryClaimGrid(d);
    } catch (e) {}
    if (d && (d.status === 'done' || d.status === 'error' || d.status === 'unavailable')) return;
    if (tries < 240) setTimeout(poll, 2500); // bounded ten-minute observer; server work continues
  };
  setTimeout(poll, 1500);
}

/* Back-fill: /api/ref sometimes downloads figures that were not on disk when the page rendered.
   When detail arrives with drawings a card doesn't show, upgrade that card's thumbnail. */
function backfillThumb(pub, images){
  if (!images || !images.length) return;
  const card = document.querySelector('.refcard[data-pub="' + CSS.escape(pub) + '"]');
  if (!card) return;
  const thumb = card.querySelector('.rthumb');
  // Only upgrade a thumbnail that gave up; never interrupt one that is mid-flight or already good.
  if (!thumb || !['none', 'error'].includes(thumb.dataset.state)) return;
  thumb.dataset.state = 'loading';
  setThumb(card, images);
}

/* ══════════════════════════════════════════════════════════════════════════════════════════
   WORLDWIDE FAMILY TIMELINE (Feature 1) — Google-Patents-style year → jurisdiction strip
   ══════════════════════════════════════════════════════════════════════════════════════════
   The card renders a corpus-only baseline server-side (families inside our corpus only). The
   authoritative worldwide INPADOC family (EPO OPS) arrives later — via the top-N prefetch poll
   or when a card is opened (/api/ref) — and upgrades the strip IN PLACE. Never a downgrade: an
   authoritative strip is never replaced by a corpus one, and an empty family never clobbers a
   baseline that already has content. */
function famInner(family){
  const nj = family.n_jurisdictions || 0;
  const sum = 'Family of ' + (family.n_members || 0) + ' in ' + nj +
    ' jurisdiction' + (nj !== 1 ? 's' : '') + (family.source === 'corpus' ? ' · corpus-only' : '');
  let h = '<span class="famsum">' + esc(sum) + '</span>';
  (family.timeline || []).forEach(g => {
    h += '<span class="famyear"><span class="fy">' + esc(g.year) + '</span>';
    (g.codes || []).forEach(c => {
      const t = esc(c.pub || '') + (c.date ? (' · filed ' + esc(c.date)) : '');
      h += '<span class="fcc" title="' + t + '">' + esc(c.cc) + '</span>';
    });
    h += '</span>';
  });
  return h;
}
function upgradeFamily(pub, family){
  if (!family || !(family.timeline || []).length) return;      // never clobber a baseline with nothing
  if (family.source !== 'ops' && family.source !== 'lens') return;   // only the authoritative one upgrades
  const el = document.querySelector('.famstrip[data-pub="' + CSS.escape(pub) + '"]');
  if (!el || el.dataset.src === 'ops' || el.dataset.src === 'lens') return;   // already authoritative
  el.dataset.src = family.source;
  el.innerHTML = famInner(family);
}

/* ══════════════════════════════════════════════════════════════════════════════════════════
   CARD PANES — lazily hydrated, one pane visible at a time
   ══════════════════════════════════════════════════════════════════════════════════════════ */
function paneOf(card){ return card.querySelector('.rpane'); }

/* Tab strip modelled on iptorch.com's per-result card: one strip, one shared pane, content
   fetched only when a tab is picked and then cached by fetchRef. Re-clicking the open tab
   closes it, so the card collapses back to its one-line form.

   GRACEFUL DEGRADATION, as iptorch does it: a section the server already knows is absent gets
   no tab at all (the template omits it); a section whose absence is only discoverable after
   /api/ref returns renders a single plain "not available" line with the office links, never a
   spinner that never settles and never an error. */
const RPANES = {
  why:      paneWhy,
  abstract: paneAbstract,
  claims:   paneClaims,
  desc:     paneDesc,
  class:    paneClass,
  figs:     paneFigs,
  cites:    paneCites,
  text:     paneText,          // retained: the detail view still asks for the combined pane
};

/* ONE listener for every card's tab strip, instead of an inline onclick per button. With ~25
   cards x ~9 buttons that attribute was several KB of page weight on its own, and delegation
   also means strips added later (or re-rendered) work with no re-binding. */
document.addEventListener('click', function(ev){
  const b = ev.target.closest && ev.target.closest('.rtabs button');
  if (!b) return;
  const pub = b.closest('.refcard').dataset.pub;
  if (b.dataset.t) rtab(b);
  else if (b.dataset.a === 'detail') openDetail(pub);
  else if (b.dataset.a === 'similar') openSimilar(pub);
});

async function rtab(btn){
  const card = btn.closest('.refcard'), t = btn.dataset.t, p = paneOf(card);
  const wasOpen = btn.getAttribute('aria-expanded') === 'true';
  card.querySelectorAll('.rtab[data-t]').forEach(b => b.setAttribute('aria-expanded', 'false'));
  if (wasOpen){ p.classList.remove('open'); p.hidden = true; return; }
  btn.setAttribute('aria-expanded', 'true');
  p.classList.add('open'); p.hidden = false;
  p.innerHTML = '<span class="ploading"><span class="spin sm" aria-hidden="true"></span> loading…</span>';
  try{
    const fn = RPANES[t];
    if (!fn){ p.innerHTML = ''; return; }
    const html = await fn(card, p);
    if (html != null) p.innerHTML = html;         // a pane may mount itself and return null
    hlNode(p);
  }catch(e){
    p.innerHTML = '<div class="pempty">Couldn\'t load this section. ' +
      '<a href="' + gp(card.dataset.pub) + '" target="_blank" rel="noopener">Open on Google Patents</a>.</div>';
  }
}

/* The "this section does not exist" line. One sentence, plus the two office links that WILL
   have it — the useful thing to offer when the local corpus is thin on a document. */
function paneMissing(pub, what, d){
  d = d || {};
  return '<div class="pempty">No ' + esc(what) + ' for this publication in the local corpus — ' +
    '<a href="' + esc(gp(pub)) + '" target="_blank" rel="noopener">Google Patents</a>' +
    ' · <a href="' + esc(esp(pub)) + '" target="_blank" rel="noopener">Espacenet</a>' +
    '.</div>';
}

async function paneAbstract(card){
  const pub = card.dataset.pub;
  const j = await fetchRef(pub, true);
  const d = j.display || {};
  if (!d.abstract) return paneMissing(pub, 'abstract', d);
  let h = '<h4>Abstract</h4><div class="abstract">' + esc(d.abstract) + '</div>';
  if (d.lang_flags && d.lang_flags.abstract)
    h += '<p class="small muted" style="margin-top:8px">This abstract is not in English. ' +
      '<button type="button" class="linkish" onclick="translatePane(this,\'' + esc(pub) + '\')">Translate</button></p>';
  return h;
}

async function paneClaims(card){
  const pub = card.dataset.pub;
  const j = await fetchRef(pub, true);
  const s = j.sections || {};
  if (!s.claims || !s.claims.length) return paneMissing(pub, 'claims', j.display);
  return '<h4>Claims (' + s.claims.length + ')</h4>' +
    '<div class="scrollbox">' + claimsHTML(s.claims, j.matched) + '</div>';
}

async function paneDesc(card){
  const pub = card.dataset.pub;
  const j = await fetchRef(pub, true);
  const s = j.sections || {};
  if (!s.paragraphs || !s.paragraphs.length) return paneMissing(pub, 'description text', j.display);
  return '<h4>Description (' + s.paragraphs.length + ' paragraphs)</h4>' +
    '<div class="scrollbox">' + parasHTML(s.paragraphs, j.matched) + '</div>';
}

async function paneClass(card){
  const pub = card.dataset.pub;
  const j = await fetchRef(pub, true);
  const d = j.display || {};
  const cls = d.classifications || [];
  if (!cls.length) return paneMissing(pub, 'classification data', d);
  return '<h4>Classifications (' + cls.length + ')</h4><div class="clslist">' +
    cls.map(c => '<div class="clsrow' + (c.first ? ' first' : '') + '">' +
      '<code>' + esc(c.code) + '</code>' +
      (c.first ? '<span class="ind">first</span>' : '') +
      (c.description ? '<span class="muted small">' + esc(c.description) + '</span>' : '') +
      '</div>').join('') + '</div>';
}

/* In-pane English translation, for the German and Japanese publications this corpus is full of.
   Same /api/translate endpoint the full detail view uses; rendered inline so reading a DE
   abstract does not cost a trip through the slideover. */
async function translatePane(btn, pub){
  const host = btn.closest('p');
  btn.disabled = true;
  btn.textContent = 'translating…';
  try{
    const j = await (await fetch(B + '/api/translate/' + encodeURIComponent(pub))).json();
    const f = (j.fields || {}).abstract;
    if (!j.found || !f || !f.text){
      host.innerHTML = '<span class="small muted">No translation available for this publication.</span>';
      return;
    }
    const box = document.createElement('div');
    box.innerHTML = '<h4 style="margin-top:14px">Abstract — English' +
      (f.lang ? ' (from ' + esc(f.lang) + ')' : '') + '</h4>' +
      '<div class="abstract">' + esc(f.text) + '</div>';
    host.replaceWith(box);
  }catch(e){
    host.innerHTML = '<span class="small muted">Translation unavailable.</span>';
  }
}

/* Citations reuse the existing graph loader, which mounts asynchronously into the node it is
   given and renders its own "unavailable" state — so this pane returns null and lets it own
   the element rather than racing it for innerHTML. */
async function paneCites(card, p){
  loadGraph(card.dataset.pub, p);
  return null;
}

async function paneWhy(card){
  const j = await fetchRef(card.dataset.pub);           // full: the opinion IS the point here
  if (j.display && j.display.images) backfillThumb(card.dataset.pub, j.display.images);
  let h = '<h4>Why this reference is relevant <span class="muted" style="text-transform:none;letter-spacing:0">— grounded AI opinion</span></h4>' +
          renderWhy(j.rationale);
  const m = j.matched;
  if (m && m.coord)
    h += '<div class="para matched" style="margin-top:11px"><span class="coord">' +
         esc((m.kind || '') + ' ' + m.coord) + '</span>best semantic match · ' +
         (m.score || 0).toFixed(3) + ' cosine</div>';
  return h;
}
function renderWhy(r){
  if (!r || !r.why) return '<div class="pempty">No AI opinion was generated for this reference.</div>';
  let h = '<div class="why">' + esc(r.why) + '</div>' + rationaleMeta(r);
  if (r.reads_on && r.reads_on.length)
    h += '<div class="readson"><span class="lbl2">reads on</span>' +
      r.reads_on.map(x => '<span class="chip el">' + esc(x) + '</span>').join('') + '</div>';
  return h;
}

async function paneFigs(card){
  const pub = card.dataset.pub;
  const j = await fetchRef(pub, true);
  const d = j.display || {};
  const imgs = d.images || [];
  backfillThumb(pub, imgs);
  if (!imgs.length)
    return '<h4>Drawings</h4><div class="nodig">No drawings are digitized for this document. ' +
      'View it on <a href="' + esc(gp(pub)) + '" target="_blank" rel="noopener">Google Patents</a>' +
      ' or <a href="' + esc(esp(pub)) + '" target="_blank" rel="noopener">Espacenet</a>.</div>';
  const kind = d.figs_from_pdf ? 'Page ' : 'Figure ';
  return '<h4>Drawings (' + imgs.length + ')</h4>' +
    (d.figs_from_pdf ? '<p class="small muted" style="margin:-4px 0 9px">Extracted from the PDF facsimile.</p>' : '') +
    '<div class="g">' + imgs.map((im, i) =>
      '<figure><img loading="lazy" decoding="async" src="' + esc(figThumb(pub, im)) + '" ' +
      'data-full="' + esc(figFull(pub, im)) + '" ' +
      'alt="' + esc(kind + (i + 1) + ' of ' + pub) + '" data-pub="' + esc(pub) + '" onclick="openLb(this)">' +
      '<figcaption>' + (d.figs_from_pdf ? 'p. ' : 'fig ') + (i + 1) + '</figcaption></figure>').join('') +
    '</div>';
}

async function paneText(card){
  const pub = card.dataset.pub;
  const j = await fetchRef(pub, true);
  const d = j.display || {}, s = j.sections || {};
  const matched = j.matched;
  let h = '';
  if (d.abstract) h += '<h4>Abstract</h4><div class="abstract">' + esc(d.abstract) + '</div>';
  if (s.claims && s.claims.length)
    h += '<h4 style="margin-top:16px">Claims (' + s.claims.length + ')</h4>' +
      '<div class="scrollbox">' + claimsHTML(s.claims, matched) + '</div>';
  if (s.paragraphs && s.paragraphs.length)
    h += '<h4 style="margin-top:16px">Description (' + s.paragraphs.length + ' paragraphs)</h4>' +
      '<div class="scrollbox">' + parasHTML(s.paragraphs, matched) + '</div>';
  if (!h) h = '<div class="pempty">No full text for this publication in the local corpus — ' +
    '<a href="' + B + '/pdf/' + encodeURIComponent(pub) + '" target="_blank" rel="noopener">open the PDF</a> or ' +
    '<a href="' + gp(pub) + '" target="_blank" rel="noopener">Google Patents</a>.</div>';
  return h;
}
function claimsHTML(claims, matched){
  const mc = matched && matched.coord_raw;
  return claims.map(cl =>
    '<div class="claim' + (cl.independent ? ' indep' : '') +
      (mc && mc.claim_no === cl.claim_no ? ' matched' : '') + '">' +
    '<span class="cn">' + esc(cl.claim_no) + '.</span>' +
    (cl.independent ? '<span class="ind">INDEP</span>' : '') +
    (mc && mc.claim_no === cl.claim_no ? '<span class="ind mm">best match</span>' : '') +
    '<div class="ctext">' + esc(cl.resolved_text || cl.text) + '</div></div>').join('');
}
function parasHTML(paras, matched){
  const mc = matched && matched.coord_raw;
  return paras.map(p =>
    '<div class="para' + (mc && mc.para_no === p.para_no ? ' matched' : '') + '">' +
    '<span class="coord">' + esc(p.para_no || '') + '</span>' +
    (p.heading ? '<span class="hd">' + esc(p.heading) + '</span> ' : '') + esc(p.text) + '</div>').join('');
}

/* ── citation graph + more-like-this ─────────────────────────────────────────────────────── */
async function loadGraph(pub, mount){
  mount.innerHTML = '<span class="ploading"><span class="spin sm" aria-hidden="true"></span> loading citations…</span>';
  try{
    const g = await (await fetch(B + '/api/graph/' + encodeURIComponent(pub))).json();
    mount.innerHTML = graphHTML(g) +
      '<div class="morelike"><h5>More like this — in-corpus, semantic</h5>' +
      '<div class="cglist mlslot"><span class="ploading"><span class="spin sm" aria-hidden="true"></span> finding similar…</span></div></div>';
    fetch(B + '/api/morelike/' + encodeURIComponent(pub)).then(r => r.json()).then(ml => {
      const slot = mount.querySelector('.mlslot'); if (!slot) return;
      const items = (ml.results || []).filter(r => normPub(r.pub) !== normPub(pub)).slice(0, 10);
      slot.innerHTML = items.length ? items.map(r =>
        '<div class="cgitem"><button type="button" class="pnlink" onclick="openDetail(\'' + esc(r.pub) + '\')">' +
        esc(r.pub) + '</button><span class="muted small">' + esc((r.title || '').slice(0, 54)) + '</span>' +
        (r.score != null ? '<span class="chip" style="margin-left:auto">' + Math.round(r.score * 100) + '</span>' : '') +
        '</div>').join('') : '<span class="muted small">none</span>';
    }).catch(() => { const s = mount.querySelector('.mlslot'); if (s) s.innerHTML = '<span class="muted small">unavailable</span>'; });
  }catch(e){ mount.innerHTML = '<div class="pempty">Citations unavailable.</div>'; }
}
function graphHTML(g){
  const col = (title, items) => {
    let h = '<div class="cgcol"><h5>' + title + ' (' + items.length + ')</h5><div class="cglist">';
    if (!items.length) h += '<span class="muted small">none</span>';
    items.forEach(it => {
      h += '<div class="cgitem">' + (it.in_corpus
        ? '<button type="button" class="pnlink" onclick="openDetail(\'' + esc(it.pub) + '\')">' + esc(it.pub) + '</button>'
        : '<a href="' + gp(it.pub) + '" target="_blank" rel="noopener">' + esc(it.pub) + '</a>') +
        (it.examiner ? '<span class="exam" title="examiner-cited">X</span>' : '') +
        (it.in_corpus ? '<span class="chip" style="margin-left:auto">in corpus</span>' : '') + '</div>';
    });
    return h + '</div></div>';
  };
  return '<div class="cgcols">' + col('Backward — cites', g.backward || []) +
    col('Forward — cited by', g.forward || []) + col('Similar', g.similar || []) + '</div>';
}

/* ── the searched text ───────────────────────────────────────────────────────────────────── */
// The header holds the WHOLE query string; `.clamped` only hides the overflow behind a three-line
// -webkit-line-clamp. So this lifts a CSS class — it never fetches, and there is nothing to
// truncate on the way back. Collapsing scrolls the header back into view, because a 40-line brief
// collapsing to three lines otherwise leaves the viewport parked far below where it started.
function toggleQuery(){
  const w = document.getElementById('qwrap'), b = document.getElementById('qmore');
  if (!w || !b) return;
  const open = w.classList.toggle('open');
  b.setAttribute('aria-expanded', open ? 'true' : 'false');
  b.firstChild.nodeValue = open ? 'Show less ' : 'Show full search ';
  if (!open) w.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/* ── jump / element filter ───────────────────────────────────────────────────────────────── */
function jumpRef(pub){
  const el = document.querySelector('.refcard[data-pub="' + CSS.escape(pub) + '"]');
  if (!el){ openDetail(pub); return; }                 // not in the top list → open it directly
  el.classList.remove('hide');
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1400);
}
function filterByElement(el){
  const sel = document.getElementById('felement'); if (!sel) return;
  const idx = (window.ELEMENTS || []).findIndex(e => e === el);
  if (idx < 0) return;
  sel.value = String(idx); applyControls();
  document.querySelector('.controls').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/* ── lightbox ────────────────────────────────────────────────────────────────────────────── */
// LB.imgs is a list of PLAIN OBJECTS {src, alt, pub} rather than live DOM nodes, because the
// three places a figure can be clicked expose their set differently.
//
// The old build was `scope.querySelectorAll('.g img, #gMain, .cmpimg')`, which matched the
// slideover gallery's actual markup (`.gallery > .gmain > #gMain` + `.gthumbs img`) with exactly
// one selector -- '#gMain' -- and '.g img' with none. That yielded a ONE-element array, and an
// explicit `if (img.id === 'gMain') LB.imgs = [img]` forced it to one even when it wasn't. So
// prev/next rendered, were focusable, had correct aria-labels, and could never move: a gallery of
// 11 figures paged through 1. Take the list from GAL, which already holds every figure URL.
let LB = { imgs: [], i: 0 };
let lbOpener = null;      // element that opened the modal, so focus can go back to it

function _lbFromNodes(nodes){
  // Prefer a data-full (the full-resolution URL, remote or local) over the possibly-thumbnail src,
  // so the lightbox always shows the high-res figure even when the pane rendered a small thumbnail.
  return [...nodes].map(n => ({ src: (n.dataset && n.dataset.full) || n.src, alt: n.alt || '',
                                pub: n.dataset ? n.dataset.pub : '' }));
}

function openLb(img){
  lbOpener = document.activeElement && document.activeElement !== document.body
    ? document.activeElement : img;
  const gallery = img.closest('.gallery');
  if (gallery && GAL.imgs.length){
    // Slideover drawings: GAL is the authoritative full list (main image + every thumbnail).
    const thumbs = document.querySelectorAll('.gthumbs img');
    LB.imgs = GAL.imgs.map((src, i) => ({
      src,
      alt: (thumbs[i] && thumbs[i].alt) || ('Figure ' + (i + 1)),
      pub: img.dataset.pub || ''
    }));
    // Clicking the main image opens whichever figure the gallery is currently showing; clicking a
    // thumbnail opens that one.
    const ti = [...thumbs].indexOf(img);
    LB.i = ti >= 0 ? ti : GAL.i;
  } else {
    const scope = img.closest('.rpane') || img.closest('.cmpgrid') || document;
    LB.imgs = _lbFromNodes(scope.querySelectorAll('.rthumb img, .cmpimg, .g img'));
    const j = LB.imgs.findIndex(o => o.src === img.src);
    LB.i = j >= 0 ? j : 0;
    if (!LB.imgs.length) { LB.imgs = _lbFromNodes([img]); LB.i = 0; }
  }
  showLb();
  const lb = document.getElementById('lb');
  lb.classList.add('open');
  lbSetBackgroundInert(true);
  document.getElementById('lbclose').focus();
}
function showLb(){
  const im = LB.imgs[LB.i]; if (!im) return;
  const stage = document.getElementById('lbstage');
  const el = document.createElement('img');
  el.src = im.src;
  el.alt = im.alt || ('Figure ' + (LB.i + 1) + (im.pub ? ' of ' + im.pub : ''));
  stage.replaceChildren(el);
  document.getElementById('lbcap').textContent =
    'Figure ' + (LB.i + 1) + ' of ' + LB.imgs.length + (im.pub ? ' · ' + im.pub : '');
  // Single-figure sets have nothing to page to; don't offer dead controls.
  const only = LB.imgs.length < 2;
  ['lbprev', 'lbnext'].forEach(id => { const b = document.getElementById(id); if (b) b.hidden = only; });
}
function lbNav(d){ if (!LB.imgs.length) return; LB.i = (LB.i + d + LB.imgs.length) % LB.imgs.length; showLb(); }

// Inerting the background stops Tab REACHING anything behind the modal, but the browser still
// walks off the end of the document and parks focus on <body>. Cycle within the dialog so Tab
// from the last control returns to the first (and Shift+Tab the other way round).
function lbTrapTab(e){
  const lb = document.getElementById('lb');
  const items = [...lb.querySelectorAll('button, [href], input, select, textarea, [tabindex]')]
    .filter(el => !el.hidden && !el.disabled && el.tabIndex !== -1 && el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  const active = document.activeElement;
  if (e.shiftKey && (active === first || !lb.contains(active))){ e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (active === last || !lb.contains(active))){ e.preventDefault(); first.focus(); }
}

// The modal sets role=dialog/aria-modal, but that alone does not stop Tab reaching the ~380
// buttons behind it -- three Tabs from Close landed on "Skip to main content". `inert` removes
// the background from both the focus order and the accessibility tree.
//
// App A can simply inert every body child because its login dialog IS a body child. #lb is NOT:
// it sits inside <main id="main">, so inerting body's children would inert the modal's own
// ancestor and therefore the modal itself -- Close/Prev/Next stop taking focus entirely.
// (Programmatic .click() still works, which makes that failure easy to miss in a scripted check.)
// Walk up from #lb instead and inert only the SIBLINGS at each level, leaving the ancestor chain
// live. Nothing outside the modal is reachable; everything inside it stays interactive.
//
// The same walk is needed by BOTH modals, so it is one function taking the modal element.
// #lb (the figure lightbox) and #soOverlay (the full-document slideover) sit at different depths,
// which is exactly why "inert every body child" is wrong for both: it would inert an ANCESTOR of
// the modal and therefore the modal itself, leaving Close/Prev/Next unfocusable. Walking up and
// inerting only the SIBLINGS at each level leaves the ancestor chain live.
//
// VERIFY THIS BY TABBING, NOT BY .click(). A programmatic click still works on an inert element,
// so a scripted "the button responds" check passes even when the trap is completely broken.
function _inertSiblingsUp(modal, store){
  for (let node = modal; node && node !== document.documentElement; node = node.parentElement){
    const parent = node.parentElement;
    if (!parent) break;
    [...parent.children].forEach(sib => {
      if (sib === node || sib.inert) return;
      sib.inert = true;
      sib.setAttribute('aria-hidden', 'true');
      store.push(sib);
    });
  }
}
function _releaseInert(store){
  store.forEach(el => { el.inert = false; el.removeAttribute('aria-hidden'); });
  store.length = 0;
}

let _lbInerted = [];
function lbSetBackgroundInert(on){
  if (!on){ _releaseInert(_lbInerted); return; }
  lbSetBackgroundInert(false);      // never stack two applications
  const lb = document.getElementById('lb');
  if (!lb) return;
  _inertSiblingsUp(lb, _lbInerted);
}

//
// THE SLIDEOVER IS THE ONE THAT ACTUALLY MATTERS HERE.
//
// A previous pass inerted the background for #lb only. But #lb is the small figure lightbox; the
// real full-document viewer on this page is #soOverlay, and it declares role="dialog"
// aria-modal="true" while #main carried no `inert` at all. aria-modal is a promise to assistive
// tech, not a focus policy: 14 Tabs with the slideover open walked straight out of it and landed
// on a chart button in the background. Same treatment, same walker.
let _soInerted = [];
function soSetBackgroundInert(on){
  if (!on){ _releaseInert(_soInerted); return; }
  soSetBackgroundInert(false);
  const so = document.getElementById('soOverlay');
  if (!so) return;
  _inertSiblingsUp(so, _soInerted);
}

// Cycle Tab inside the slideover so focus cannot escape at the ends either.
function soTrapTab(e){
  if (e.key !== 'Tab') return;
  const so = document.getElementById('soOverlay');
  if (!so || !so.classList.contains('open')) return;
  const f = [...so.querySelectorAll('a[href],button:not([disabled]),input,select,textarea,[tabindex]:not([tabindex="-1"])')]
    .filter(el => el.offsetParent !== null || el === document.activeElement);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1], active = document.activeElement;
  if (e.shiftKey && (active === first || !so.contains(active))){ e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (active === last || !so.contains(active))){ e.preventDefault(); first.focus(); }
}

function closeLb(){
  document.getElementById('lb').classList.remove('open');
  lbSetBackgroundInert(false);
  // Return focus to whatever opened the modal; otherwise keyboard users are dumped at the top of
  // the document and lose their place in the card list.
  if (lbOpener && document.contains(lbOpener) && typeof lbOpener.focus === 'function') lbOpener.focus();
  lbOpener = null;
}

/* ── sort + filter ───────────────────────────────────────────────────────────────────────── */
function applyControls(){
  const cont = document.getElementById('cards'); if (!cont) return;
  const cards = [...cont.querySelectorAll('.refcard')];
  const val = id => { const e = document.getElementById(id); return e ? e.value : ''; };
  const on = id => { const e = document.getElementById(id); return !!(e && e.checked); };
  const elName = val('felement') !== '' ? (window.ELEMENTS || [])[+val('felement')] : null;
  const fj = val('fjuris'), fflag = val('fflag');
  let shown = 0;
  cards.forEach(c => {
    let ok = true;
    if (on('fprior') && !['public_prior_art', 'secret_prior_art'].includes(c.dataset.basis)) ok = false;
    if (on('fdraw') && !(+c.dataset.nimg > 0)) ok = false;
    if (fj && c.dataset.juris !== fj) ok = false;
    if (elName && !(c.dataset.covers || '').split('||').includes(elName)) ok = false;
    if (fflag){ const fl = c.dataset.flag || ''; if (fflag === 'unflagged' ? !!fl : fl !== fflag) ok = false; }
    c.classList.toggle('hide', !ok); if (ok) shown++;
  });
  const key = {
    rank: c => +c.dataset.rank, score: c => -c.dataset.rel, covers: c => -c.dataset.ncovers,
    date: c => -(Date.parse(c.dataset.date) || 0), datea: c => (Date.parse(c.dataset.date) || 9e15),
    juris: c => c.dataset.juris
  }[val('sortby')] || (c => +c.dataset.rank);
  cards.sort((a, b) => { const ka = key(a), kb = key(b); return ka < kb ? -1 : ka > kb ? 1 : 0; });
  cards.forEach(c => cont.appendChild(c));
  const sh = document.getElementById('shown');
  if (sh) sh.textContent = shown + ' of ' + cards.length + ' references shown';
}

/* Live re-rank: given the authoritative order as a list of pub ids (from the SSE 'rank' event),
   move the already-rendered cards into that order and renumber them. Cards are MOVED, not
   re-created, so lazily-hydrated panes and thumbnail state survive. Any card not named in the
   order keeps its relative position after the named ones. Then applyControls re-applies the
   current sort/filter — the default 'Relevance' sort reads data-rank, so the new order sticks.
   A missing/empty order is a no-op, leaving the deterministic server-rendered order. */
function reorderCards(order){
  const cont = document.getElementById('cards');
  if (!cont || !Array.isArray(order) || !order.length) return;
  const byPub = {};
  cont.querySelectorAll('.refcard').forEach(c => { byPub[normPub(c.dataset.pub)] = c; });
  let rank = 0, moved = false;
  order.forEach(pub => {
    const c = byPub[normPub(pub)];
    if (!c) return;                         // e.g. a federated-only card not yet in this DOM
    cont.appendChild(c);                    // move in place — preserves open panes / thumbs
    rank += 1;
    c.dataset.rank = String(rank);
    const rk = c.querySelector('.rtitle .rank');
    if (rk) rk.textContent = '#' + rank;
    moved = true;
  });
  if (moved && typeof applyControls === 'function') applyControls();
}

/* ── selection, export, compare ──────────────────────────────────────────────────────────── */
const selectedPubs = () => [...document.querySelectorAll('.selbox:checked')].map(b => b.value);
function updateBar(){
  const n = selectedPubs().length;
  document.getElementById('selcount').textContent = n + ' selected';
  document.getElementById('exportbar').classList.toggle('show', n > 0);
}
function clearSel(){ document.querySelectorAll('.selbox:checked').forEach(b => (b.checked = false)); updateBar(); }
function doExport(fmt){
  const sel = selectedPubs();
  if (!sel.length){ alert('Select at least one reference first (the checkbox on the left of a card).'); return; }
  document.getElementById('exportpubs').value = sel.join(',');
  document.getElementById('exportfmt').value = fmt;
  // The form posts into a new tab, so this side of the connection never learns when the file is
  // ready. Building one is not instant either — the first PDF/DOCX/XLSX for a report may pull a
  // figure per reference off the CDN — so say so on the button rather than looking dead. The
  // label restores on a timer because there is no completion event to hang it off.
  const btn = (typeof event !== 'undefined' && event && event.currentTarget) || null;
  if (btn){
    const was = btn.textContent;
    btn.textContent = 'building…'; btn.disabled = true;
    setTimeout(() => { btn.textContent = was; btn.disabled = false; }, 6000);
  }
  document.getElementById('exportform').submit();
}
function openCompare(){
  const sel = selectedPubs();
  if (sel.length < 2 || sel.length > 3){ alert('Select 2 or 3 references to compare side by side.'); return; }
  window.open(B + '/compare?slug=' + encodeURIComponent(window.SLUG) + '&pubs=' + encodeURIComponent(sel.join(',')), '_blank');
}

/* ── triage flags ────────────────────────────────────────────────────────────────────────── */
async function loadFlags(){
  try{
    const flags = await (await fetch(B + '/api/flags/' + encodeURIComponent(window.SLUG))).json();
    Object.entries(flags || {}).forEach(([pub, e]) => {
      const card = document.querySelector('.refcard[data-pub="' + CSS.escape(pub) + '"]');
      if (!card || !e.flag) return;
      card.dataset.flag = e.flag;
      const p = card.querySelector('.fp-' + e.flag);
      if (p){ p.classList.add('on'); p.setAttribute('aria-pressed', 'true'); }
    });
  }catch(e){}
}
async function setFlag(btn){
  const card = btn.closest('.refcard'), pub = card.dataset.pub, want = btn.dataset.flag;
  const next = (card.dataset.flag || '') === want ? '' : want;
  card.querySelectorAll('.fp').forEach(b => { b.classList.remove('on'); b.setAttribute('aria-pressed', 'false'); });
  if (next){ card.dataset.flag = next; btn.classList.add('on'); btn.setAttribute('aria-pressed', 'true'); }
  else card.removeAttribute('data-flag');
  try{
    await fetch(B + '/api/flags/' + encodeURIComponent(window.SLUG), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pub, flag: next })
    });
  }catch(e){}
}

/* ══════════════════════════════════════════════════════════════════════════════════════════
   DETAIL DRAWER — the lead answer first, everything else one disclosure away
   ══════════════════════════════════════════════════════════════════════════════════════════ */
const detailStack = [];
let GAL = { imgs: [], i: 0 };
const overlay = () => document.getElementById('soOverlay');
let lastFocus = null;

function galSet(i){
  if (!GAL.imgs.length) return;
  GAL.i = (i + GAL.imgs.length) % GAL.imgs.length;
  const main = document.getElementById('gMain');
  const thumbs = document.querySelectorAll('.gthumbs img');
  if (main){
    main.src = GAL.imgs[GAL.i];
    if (thumbs[GAL.i] && thumbs[GAL.i].alt) main.alt = thumbs[GAL.i].alt;
  }
  const c = document.getElementById('gCount');
  if (c) c.textContent = (GAL.i + 1) + ' / ' + GAL.imgs.length;
  thumbs.forEach((t, idx) => t.classList.toggle('sel', idx === GAL.i));
}
const galPrev = () => galSet(GAL.i - 1);
const galNext = () => galSet(GAL.i + 1);

function setHash(pn){ try{ history.replaceState(null, '', B + '/report/' + window.SLUG + '#patent=' + encodeURIComponent(pn)); }catch(e){} }
function clearHash(){ try{ history.replaceState(null, '', B + '/report/' + window.SLUG); }catch(e){} }

async function openDetail(pn, push = true){
  if (!pn) return;
  if (!overlay().classList.contains('open')) lastFocus = document.activeElement;
  if (push && detailStack[detailStack.length - 1] !== pn) detailStack.push(pn);
  document.getElementById('soBack').hidden = detailStack.length <= 1;
  document.getElementById('soBreadcrumb').textContent = detailStack.join('  ›  ');
  setHash(pn);
  overlay().classList.add('open');
  soSetBackgroundInert(true);
  document.body.style.overflow = 'hidden';
  document.getElementById('soClose').focus();
  const body = document.getElementById('soBody');
  body.innerHTML = '<div class="so-loading"><span class="spin" aria-hidden="true"></span>' +
    '<div>Loading ' + esc(pn) + ' — drawings, claims, citations…</div></div>';
  try{ renderDetail(pn, await fetchRef(pn)); }
  catch(e){
    body.innerHTML = '<div class="so-loading"><div>Couldn\'t load ' + esc(pn) + '.</div>' +
      '<a class="btn ghost sm" href="' + gp(pn) + '" target="_blank" rel="noopener">Open on Google Patents</a></div>';
  }
}
function closeDetail(){
  overlay().classList.remove('open');
  soSetBackgroundInert(false);
  detailStack.length = 0;
  document.body.style.overflow = '';
  clearHash();
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

function renderDetail(pn, j){
  const d = j.display || {}, sec = j.sections || {}, matched = j.matched, rat = j.rationale;
  if (d.images) backfillThumb(pn, d.images);
  const flag = (d.country && FLAGS[d.country]) || '';
  let h = '<div class="so-title">' + (flag ? flag + ' ' : '') + esc(d.title || pn) + '</div>';
  h += '<div class="so-sub"><a class="pn" href="' + esc(gp(pn)) + '" target="_blank" rel="noopener">' +
    esc(pn) + ' ↗</a>' +
    (d.assignees && d.assignees.length ? ' · ' + esc(d.assignees.join('; ')) : '') +
    (d.publication_date ? ' · published ' + esc(d.publication_date) : '') +
    (d.inventors && d.inventors.length ? ' · ' + esc(d.inventors.slice(0, 4).join(', ')) : '') + '</div>';

  h += '<div class="so-chips">';
  if (d.pdf_url) h += '<a class="chip ch" href="' + B + '/pdf/' + encodeURIComponent(pn) + '" target="_blank" rel="noopener">PDF ⭳</a>';
  h += '<a class="chip" href="' + esc(esp(pn)) + '" target="_blank" rel="noopener">Espacenet ↗</a>';
  h += '<a class="chip" href="' + esc(gp(pn)) + '" target="_blank" rel="noopener">Google Patents ↗</a>';
  h += '<button type="button" class="chip el" onclick="openSimilar(\'' + esc(pn) + '\')">More like this</button>';
  if (window.SLUG) h += '<button type="button" class="chip el" id="soChartBtn" onclick="loadChart(\'' + esc(pn) + '\')">Build claim chart</button>';
  if (d.lang_flags && d.lang_flags.abstract)
    h += '<button type="button" class="chip el" onclick="loadTranslation(\'' + esc(pn) + '\')">Translate to English</button>';
  h += '</div>';

  /* THE LEAD: opinion + best-matching passage, with no interaction required. */
  if (rat && rat.why){
    h += '<div class="so-lead"><h3>Why this is relevant</h3>' + renderWhy(rat) + '</div>';
  }
  if (matched && matched.coord){
    h += '<div class="so-lead"><h3>Best-matching passage</h3><div class="para matched">' +
      '<span class="coord">' + esc((matched.kind || '') + ' ' + matched.coord) + '</span>' +
      esc(matched.text || '') + '</div>' +
      '<p class="small muted" style="margin:8px 0 0">' + (matched.score || 0).toFixed(3) + ' cosine similarity to the query.</p></div>';
  }
  h += '<div id="soChart"></div><div id="soTrans"></div>';

  const imgs = (d.images || []).map(im => figFull(d.pub || pn, im));
  if (imgs.length){
    GAL = { imgs, i: 0 };
    const kind = d.figs_from_pdf ? 'Page ' : 'Figure ';
    h += '<h2>Drawings — ' + imgs.length + (d.figs_from_pdf ? ', from the PDF facsimile' : '') + '</h2>';
    h += '<div class="gallery"><div class="gmain">' +
      (imgs.length > 1 ? '<button class="gnav l" onclick="galPrev()" aria-label="Previous figure">‹</button>' : '') +
      '<img id="gMain" src="' + imgs[0] + '" data-pub="' + esc(pn) + '" onclick="openLb(this)" ' +
      'alt="' + esc(kind + '1 of ' + pn) + '">' +
      (imgs.length > 1 ? '<button class="gnav r" onclick="galNext()" aria-label="Next figure">›</button>' : '') +
      '<span class="gcount" id="gCount">1 / ' + imgs.length + '</span></div>';
    if (imgs.length > 1)
      h += '<div class="gthumbs">' + imgs.map((u, i) =>
        '<img src="' + u + '" alt="' + esc(kind + (i + 1) + ' of ' + pn) + '" class="' + (i === 0 ? 'sel' : '') +
        '" onclick="galSet(' + i + ')" loading="lazy">').join('') + '</div>';
    h += '</div>';
  }

  h += '<h2>Details</h2>';
  const claims = (sec.claims && sec.claims.length) ? sec.claims : null;
  const paras = (sec.paragraphs && sec.paragraphs.length) ? sec.paragraphs : null;
  if (d.abstract)
    h += '<details class="sec2" open><summary>Abstract</summary><div class="secbody">' + esc(d.abstract) + '</div></details>';
  if (claims)
    h += '<details class="sec2"><summary>Claims<span class="cnt">' + claims.length + '</span></summary>' +
      '<div class="secbody"><div class="scrollbox">' + claimsHTML(claims, matched) + '</div></div></details>';
  if (paras)
    h += '<details class="sec2"><summary>Description<span class="cnt">' + paras.length + ' paragraphs</span></summary>' +
      '<div class="secbody"><div class="scrollbox">' + parasHTML(paras, matched) + '</div></div></details>';

  const bib = [['Inventors', (d.inventors || []).join(', ')], ['Assignee', (d.assignees || []).join('; ')],
    ['Priority', d.priority_date], ['Filing', d.filing_date], ['Published', d.publication_date],
    ['Country', d.country], ['Family', d.family_id]].filter(([, v]) => v);
  if (bib.length || (d.classifications || []).length){
    h += '<details class="sec2"><summary>Bibliographic data</summary><div class="secbody"><div class="biblio">';
    bib.forEach(([k, v]) => { h += '<div class="k">' + k + '</div><div>' + esc(v) + '</div>'; });
    if ((d.classifications || []).length)
      h += '<div class="k">CPC</div><div>' + d.classifications.slice(0, 14).map(c =>
        '<span class="chip cpc" title="' + esc(c.description || '') + '">' + esc(c.code) + '</span>').join('') + '</div>';
    h += '</div></div></details>';
  }
  h += '<details class="sec2" id="soCitesWrap"><summary>Citations &amp; similar patents</summary>' +
    '<div class="secbody" id="soCites"></div></details>';
  if (d.pdf_url)
    h += '<details class="sec2"><summary>PDF facsimile<span class="cnt">' +
      '<a href="' + B + '/pdf/' + encodeURIComponent(pn) + '" target="_blank" rel="noopener">open ↗</a></span></summary>' +
      '<div class="secbody"><div class="pdfwrap"><iframe data-src="' + B + '/pdf/' + encodeURIComponent(pn) +
      '" title="PDF facsimile of ' + esc(pn) + '" loading="lazy"></iframe></div></div></details>';
  if (!d.abstract && !claims && !paras)
    h += '<div class="nodig" style="margin-top:10px">No full text for this publication in the local corpus.</div>';

  const body = document.getElementById('soBody');
  body.innerHTML = h; body.scrollTop = 0;
  body.querySelectorAll('.secbody, .why, .so-lead .para').forEach(hlNode);
  // Citations and the PDF iframe cost a request each — only when their section is opened.
  const cw = document.getElementById('soCitesWrap');
  cw.addEventListener('toggle', () => {
    if (cw.open && !cw.dataset.loaded){ cw.dataset.loaded = '1'; loadGraph(pn, document.getElementById('soCites')); }
  });
  body.querySelectorAll('iframe[data-src]').forEach(f => {
    const det = f.closest('details');
    det.addEventListener('toggle', () => { if (det.open && !f.src) f.src = f.dataset.src; });
  });
}

/* per-reference grounded claim chart — /api/chart is a live Vertex call, so it is opt-in */
async function loadChart(pn){
  const mount = document.getElementById('soChart'); if (!mount) return;
  const btn = document.getElementById('soChartBtn'); if (btn) btn.disabled = true;
  mount.innerHTML = '<div class="so-lead"><h3>Element-by-element claim chart</h3>' +
    '<span class="ploading"><span class="spin sm" aria-hidden="true"></span> grounding each element against this document…</span></div>';
  try{
    const j = await (await fetch(B + '/api/chart/' + encodeURIComponent(pn) + '?slug=' + encodeURIComponent(window.SLUG))).json();
    if (j.error){ mount.innerHTML = '<div class="so-lead"><div class="pempty">' + esc(j.error) + '</div></div>'; return; }
    const rows = j.rows || [];
    const sum = j.summary || {};
    mount.innerHTML = '<div class="so-lead"><h3>Element-by-element claim chart</h3>' +
      '<div class="msum">' + (sum.disclosed || 0) + ' disclosed · ' + (sum.partial || 0) + ' partial · ' +
      (sum.absent || 0) + ' absent' + (j.method ? ' · ' + esc(j.method) : '') + '</div>' +
      '<div class="mchart">' + rows.map(r =>
        '<div class="mrow"><div class="mel">' + esc(r.element) + '</div>' +
        '<div><span class="mv mv-' + esc(r.verdict) + '">' + esc(r.verdict) + '</span></div>' +
        '<div class="mq">' + (r.quote ? '“' + esc(r.quote) + '”' : '<span class="muted">no supporting passage found</span>') +
        (r.location ? '<span class="mloc">' + esc(r.location) + '</span>' : '') + '</div></div>').join('') +
      '</div></div>';
  }catch(e){
    mount.innerHTML = '<div class="so-lead"><div class="pempty">Claim chart unavailable.</div></div>';
    if (btn) btn.disabled = false;
  }
}

/* on-demand English translation of a non-English reference */
async function loadTranslation(pn){
  const mount = document.getElementById('soTrans'); if (!mount) return;
  mount.innerHTML = '<div class="so-lead"><h3>English translation</h3>' +
    '<span class="ploading"><span class="spin sm" aria-hidden="true"></span> translating…</span></div>';
  try{
    const j = await (await fetch(B + '/api/translate/' + encodeURIComponent(pn))).json();
    const f = j.fields || {};
    if (!j.found || !Object.keys(f).length){
      mount.innerHTML = '<div class="so-lead"><div class="pempty">Nothing to translate for this publication.</div></div>';
      return;
    }
    let h = '<div class="so-lead"><h3>English translation</h3>';
    Object.entries(f).forEach(([name, v]) => {
      if (!v || !v.text) return;
      h += '<h4 style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);margin:10px 0 5px">' +
        esc(name) + (v.lang ? ' — from ' + esc(v.lang) : '') + '</h4>' +
        '<div class="abstract" style="white-space:pre-wrap">' + esc(v.text) + '</div>';
    });
    mount.innerHTML = h + '</div>';
  }catch(e){ mount.innerHTML = '<div class="so-lead"><div class="pempty">Translation unavailable.</div></div>'; }
}

async function openSimilar(pn){
  detailStack.push(pn + ' · similar');
  document.getElementById('soBack').hidden = detailStack.length <= 1;
  document.getElementById('soBreadcrumb').textContent = detailStack.join('  ›  ');
  overlay().classList.add('open');
  soSetBackgroundInert(true);
  document.body.style.overflow = 'hidden';
  const body = document.getElementById('soBody');
  body.innerHTML = '<div class="so-loading"><span class="spin" aria-hidden="true"></span>' +
    '<div>Finding patents similar to ' + esc(pn) + '…</div></div>';
  try{
    const ml = await (await fetch(B + '/api/morelike/' + encodeURIComponent(pn))).json();
    const list = (ml.results || []).filter(r => normPub(r.pub) !== normPub(pn));
    let h = '<div class="so-title">Similar to ' + esc(pn) + '</div>' +
      '<div class="so-sub">ranked by embedding cosine · in-corpus only</div>';
    h += list.length ? '<div style="margin-top:14px">' + list.map(r =>
      '<button type="button" class="simrow" onclick="openDetail(\'' + esc(r.pub) + '\')">' +
      '<span class="simscore">' + Math.round((r.score || 0) * 100) + '</span>' +
      '<span style="min-width:0"><b class="pn">' + esc(r.pub) + '</b>' +
      (r.country ? ' <span class="chip">' + esc(r.country) + '</span>' : '') +
      (r.near_identical ? ' <span class="chip" title="Embedding-identical text — usually the same application published again">same text</span>' : '') +
      '<span class="muted small" style="display:block;margin-top:2px">' + esc(r.title || '(untitled)') + '</span></span></button>').join('') + '</div>'
      : '<div class="nodig" style="margin-top:14px">No similar in-corpus patents found.</div>';
    body.innerHTML = h; body.scrollTop = 0;
  }catch(e){ body.innerHTML = '<div class="so-loading">Error finding similar patents.</div>'; }
}

/* ══════════════════════════════════════════════════════════════════════════════════════════
   PROGRESS NARRATIVE — shared by the generating page and the in-place "refining" banner
   ══════════════════════════════════════════════════════════════════════════════════════════
   The agent emits elements / per-search progress / partial / seeded / round / reranking /
   federating / done. The stage list is a state machine that never moves
   backwards, each stage keeps the numbers it learned, and the active stage shows how long it has
   been running — a silent server still reads as visible, honest progress. */
const LIVE_CORPUS_N = Number((typeof window !== 'undefined' && window.CORPUS_PUBLICATIONS) || 0);
const LIVE_CORPUS_NOTE = LIVE_CORPUS_N
  ? 'Searching ' + LIVE_CORPUS_N.toLocaleString() + ' publications in the latest measured corpus snapshot through dense, sparse, CPC, citation and cross-lingual retrieval channels.'
  : 'Searching the live indexed corpus through dense, sparse, CPC, citation and cross-lingual retrieval channels.';
const STAGES = [
  { key: 'decompose', rank: 0, name: 'Reading the disclosure',
    note: 'Decomposing the invention into independent technical elements.' },
  { key: 'search',    rank: 1, name: 'Searching the corpus',
    note: LIVE_CORPUS_NOTE },
  { key: 'expand',    rank: 2, name: 'Expanding the candidate set',
    note: 'Following citations, patent families and EN/DE equivalents outward from the seed hits.' },
  { key: 'rounds',    rank: 3, name: 'Refinement rounds',
    note: 'Re-querying on the elements that are still uncovered, until new families stop appearing.' },
  { key: 'rerank',    rank: 4, name: 'Reranking and grounding',
    note: 'A bge-reranker cross-encoder rescores the closest 25 references, then each claim-chart cell is grounded in real text.' },
  { key: 'federate',  rank: 5, name: 'Wider search — external APIs',
    note: 'Querying every configured external source in parallel, then fusing the results by reciprocal rank.' },
  { key: 'done',      rank: 6, name: 'Report ready', note: '' }
];
const KIND_RANK = {
  elements: 1, search_progress: 1, seeded: 2, seed_progress: 2, partial: 2,
  round: 3, round_progress: 3, reranking: 4, rerank_progress: 4,
  federating: 5, done: 6
};

function createProgress(mount, opts){
  opts = opts || {};
  const wide = !!opts.wide;
  const stages = STAGES.filter(s => s.key !== 'federate' || wide);
  const state = { rank: 0, detail: {}, since: Date.now(), started: Date.now(), msg: '' };
  mount.innerHTML = '<ul class="stages">' + stages.map(s =>
    '<li data-rank="' + s.rank + '"><span class="ico" aria-hidden="true">✓</span>' +
    '<span class="txt"><span class="st-name">' + s.name + '</span>' +
    '<span class="st-note">' + s.note + '</span></span></li>').join('') + '</ul>';

  function facts(){
    const d = state.detail, out = [];
    if (d.elements) out.push(d.elements + ' element' + (d.elements !== 1 ? 's' : '') + ' identified');
    if (d.families) out.push(d.families.toLocaleString() + ' candidate families');
    if (d.round) out.push('round ' + d.round);
    if (state.rank <= 3 && d.search_done) {
      out.push(d.search_done + ' of up to ' + d.search_max + ' retrieval passes');
      if (d.search_seconds != null) out.push('last pass ' + Number(d.search_seconds).toFixed(1) + 's');
    }
    return out;
  }
  function paint(){
    stages.forEach(s => {
      const li = mount.querySelector('li[data-rank="' + s.rank + '"]');
      if (!li) return;
      li.classList.toggle('done', s.rank < state.rank);
      li.classList.toggle('active', s.rank === state.rank && state.rank < 6);
      const el = li.querySelector('.st-note');
      if (s.rank === state.rank){
        const bits = facts();
        // Only the ACTIVE stage carries the counters and a running clock, so a quiet server still
        // shows movement. Finished stages fall back to their plain description — leaving a frozen
        // "Running 12s" on a completed row reads as a hang, which is the bug this replaced.
        el.textContent = s.note + (bits.length ? ' ' + bits.join(' · ') + '.' : '') +
          (s.rank < 6 ? ' Running ' + fmtDur(Date.now() - state.since) + '.' : '');
      } else {
        el.textContent = s.note;
      }
    });
    const live = mount.parentElement && mount.parentElement.querySelector('[data-progress-live]');
    if (live){
      const cur = stages.find(s => s.rank === state.rank) || stages[stages.length - 1];
      const bits = facts();
      live.textContent = cur.name + (bits.length ? ' — ' + bits.join(', ') : '');
    }
    const bar = document.getElementById('bar');
    if (bar) bar.style.width = Math.min(97, (state.rank / 6) * 100 + 6).toFixed(0) + '%';
    const el = document.getElementById('elapsed');
    if (el) el.textContent = fmtDur(Date.now() - state.started) + ' elapsed';
  }
  paint();
  const timer = setInterval(paint, 1000);

  return {
    apply(ev){
      const r = KIND_RANK[ev.kind];
      if (r != null && r > state.rank){ state.rank = r; state.since = Date.now(); }
      if (ev.detail) Object.assign(state.detail, ev.detail);
      if (ev.msg) state.msg = ev.msg;
      paint();
    },
    fail(msg){
      clearInterval(timer);
      mount.insertAdjacentHTML('afterend', '<div class="callout" role="alert"><span class="ci">⚠</span>' +
        '<div><h3>The search failed</h3><p>' + esc(msg || 'Unknown error') + '</p></div></div>');
    },
    stop(){ clearInterval(timer); }
  };
}

/* One transport helper for both progress surfaces: SSE first, polling if SSE never connects. */
function streamJob(slug, onEvent){
  let finished = false;
  const done = () => { finished = true; };
  function poll(){
    if (finished) return;
    fetch(B + '/status/' + encodeURIComponent(slug))
      .then(r => {
        // A lapsed session answers 401 with a JSON error body. Feeding that to onEvent() would
        // be read as a malformed progress frame; the global fetch hook has already raised the
        // re-auth bar, so just keep waiting and resume once the user signs back in.
        if (r.status === 401){ setTimeout(poll, 2000); return null; }
        if (!r.ok){ setTimeout(poll, 2000); return null; }
        return r.json();
      })
      .then(j => { if (j == null) return; if (!onEvent(j)) setTimeout(poll, 2000); else done(); })
      .catch(() => setTimeout(poll, 2000));
  }
  if (!window.EventSource){ poll(); return; }
  let es, opened = false;
  try{ es = new EventSource(B + '/events/' + encodeURIComponent(slug)); }
  catch(e){ poll(); return; }
  const guard = setTimeout(() => { if (!opened && !finished){ try{ es.close(); }catch(e){} poll(); } }, 5000);
  es.onopen = () => { opened = true; clearTimeout(guard); };
  es.onmessage = ev => {
    let j; try{ j = JSON.parse(ev.data); }catch(e){ return; }
    if (onEvent(j)){ done(); try{ es.close(); }catch(e){} }
  };
  es.onerror = () => {
    clearTimeout(guard);
    try{ es.close(); }catch(e){}
    if (!finished) poll();
  };
}

/* Server-rendered figures (the direct-first card thumbnails and the compare page) bypass the
   loader above, so they need the same promise: a figure or an explicit failure state, never a
   broken-image icon. Covers images that already failed before this ran. */
function guardStaticFigures(){
  document.querySelectorAll('.rthumb img, .cmpimg').forEach(img => {
    const fail = () => {
      const thumb = img.closest('.rthumb');
      if (thumb){ thumbFail(thumb); return; }
      const box = document.createElement('div');
      box.className = 'nodig';
      box.textContent = 'Drawing unavailable.';
      img.replaceWith(box);
    };
    if (img.complete && img.naturalWidth === 0) fail(); else img.addEventListener('error', fail);
  });
}

/* ── init ────────────────────────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  guardStaticFigures();
  const lb = document.getElementById('lb');
  if (lb){
    document.addEventListener('keydown', e => {
      if (!lb.classList.contains('open')) return;
      if (e.key === 'Tab') lbTrapTab(e);
      if (e.key === 'Escape') closeLb();
      if (e.key === 'ArrowLeft') lbNav(-1);
      if (e.key === 'ArrowRight') lbNav(1);
    });
  }
  if (!document.getElementById('cards')) return;

  document.getElementById('soClose').addEventListener('click', closeDetail);
  document.getElementById('soBack').addEventListener('click', () => {
    detailStack.pop();
    const prev = detailStack[detailStack.length - 1];
    if (prev) openDetail(prev.replace(/ · similar$/, ''), false); else closeDetail();
  });
  overlay().addEventListener('click', e => { if (e.target === overlay()) closeDetail(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && overlay().classList.contains('open') && !document.getElementById('lb').classList.contains('open'))
      closeDetail();
  });
  // Capture phase: the trap has to see Tab before anything inside the panel consumes it.
  document.addEventListener('keydown', soTrapTab, true);
  document.getElementById('soLink').addEventListener('click', () => {
    const pn = (detailStack[detailStack.length - 1] || '').replace(/ · similar$/, '');
    if (!pn) return;
    const url = location.origin + B + '/report/' + window.SLUG + '#patent=' + encodeURIComponent(pn);
    const btn = document.getElementById('soLink'), t = btn.textContent;
    const ok = () => { btn.textContent = '✓ Copied'; setTimeout(() => (btn.textContent = t), 1400); };
    if (navigator.clipboard) navigator.clipboard.writeText(url).then(ok, ok); else prompt('Copy link:', url);
  });

  document.querySelectorAll('.fp').forEach(b =>
    b.addEventListener('click', e => { e.stopPropagation(); setFlag(b); }));
  loadFlags();
  document.querySelectorAll('.refcard .rsnip').forEach(hlNode);
  applyControls();
  recoverBrokenInitialThumbs();
  resolveThumbs();
  resolvePdfLinks();
  // A partial report is replaced shortly and the agent is still consuming the same server. Avoid
  // warming disposable cards; the final page performs both bounded prefetches after reload.
  if (!window.PARTIAL){
    prefetchTopN();        // proactively resolve drawings + worldwide family for shown final cards
    setTimeout(warmDetails, 1200); // eager-warm final card details so their tabs open instantly
    setTimeout(warmRationales, 2600); // deeper top-card explanations, no click required
    warmQueryClaimGrid();  // uploaded claims x references; server already runs it in background
  }

  const m = (location.hash || '').match(/patent=([^&]+)/);
  if (m){ try{ openDetail(decodeURIComponent(m[1])); }catch(e){} }
});
