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
/*  /refdrawing/, not /figures/. The figure compiler is a separate app mounted at /figures/ on this
    host with an `^~` nginx location, which beats every regex, so reference drawings served from
    /figures/<pub>/<file> reached the compiler and came back as a 302 to its login. A redirected
    <img> renders as a broken one, which is why every drawing on every report looked missing while
    the files were on disk all along. See webapp.REFDRAW_PREFIX. */
const figUrl = (pub, file) => B + '/refdrawing/' + encodeURIComponent(pub) + '/' + encodeURIComponent(file);
/* A figure entry is EITHER a locally-recovered file (served from /refdrawing/<pub>/<file>) OR a
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
let _authRestorePromise = null;
let _authRestoreResolve = null;

function _authBar(){
  let el = document.getElementById('authexpired');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'authexpired';
  el.setAttribute('role', 'alertdialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-labelledby', 'authexpiredmsg');
  el.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:10000;background:#171a21;' +
    'border-top:1px solid #2d3341;color:#e6e8ee;padding:12px 16px;display:flex;gap:10px;' +
    'align-items:center;flex-wrap:wrap;font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif';
  el.innerHTML =
    '<span id="authexpiredmsg">Your session expired. Sign in again to continue — ' +
    'this page and any run in progress are kept.</span>' +
    (window.NAMED_ACCOUNT_SESSION ?
      '<input id="authexpiredemail" type="email" autocomplete="username" placeholder="Email" ' +
      'aria-label="Account email" style="padding:7px 10px;border-radius:7px;border:1px solid #2d3341;' +
      'background:#0f1115;color:#e6e8ee;font-size:14px">' : '') +
    '<input id="authexpiredpw" type="password" autocomplete="current-password" placeholder="Password" ' +
    'aria-label="Access password" style="padding:7px 10px;border-radius:7px;border:1px solid #2d3341;' +
    'background:#0f1115;color:#e6e8ee;font-size:14px">' +
    '<button type="button" id="authexpiredgo" style="padding:7px 14px;border:0;border-radius:7px;' +
    'background:#3b82f6;color:#fff;font-weight:600;cursor:pointer">Sign in</button>' +
    '<span id="authexpirederr" role="alert" style="color:#f87171"></span>';
  document.body.appendChild(el);
  const emailInput = document.getElementById('authexpiredemail');
  if (emailInput) emailInput.value = window.CURRENT_USER_EMAIL || '';

  const submit = async () => {
    const pw = document.getElementById('authexpiredpw').value;
    const email = emailInput ? emailInput.value : '';
    const err = document.getElementById('authexpirederr');
    err.textContent = '';
    try{
      const body = new URLSearchParams({ password: pw, email });
      const r = await window.__rawFetch(B + '/login', {
        method: 'POST', body,
        headers: { 'Content-Type': 'application/x-www-form-urlencoded',
          'Accept': 'application/json', 'X-Reauth': '1' }
      });
      if (r.status === 401){ err.textContent = 'Email or password is incorrect.'; return; }
      if (r.status === 429){ err.textContent = 'Too many attempts — wait a few minutes.'; return; }
      const data = await r.json();
      if (!r.ok || !data.csrf_token){ err.textContent = data.error || 'Sign-in failed.'; return; }
      window.CSRF_TOKEN = data.csrf_token;
      document.querySelectorAll('input[name="csrf_token"]').forEach(input => {
        input.value = data.csrf_token;
      });
      el.remove();
      _authPrompted = false;
      const resolve = _authRestoreResolve;
      _authRestorePromise = null; _authRestoreResolve = null;
      if (resolve) resolve();
      document.dispatchEvent(new CustomEvent('auth:restored'));
    }catch(e){ err.textContent = 'Network error.'; }
  };
  document.getElementById('authexpiredgo').addEventListener('click', submit);
  document.getElementById('authexpiredpw').addEventListener('keydown', e => {
    if (e.key === 'Enter') submit();
  });
  el.addEventListener('keydown', e => {
    if (e.key !== 'Tab') return;
    const focusable = [...el.querySelectorAll('input,button')].filter(node => !node.disabled);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
  });
  return el;
}

function onAuthExpired(){
  if (!_authRestorePromise)
    _authRestorePromise = new Promise(resolve => { _authRestoreResolve = resolve; });
  if (!_authPrompted){
    _authPrompted = true;
    _authBar();
    const pw = document.getElementById('authexpiredpw');
    if (pw) pw.focus();
  }
  return _authRestorePromise;
}

if (typeof window !== 'undefined' && window.fetch && !window.__rawFetch){
  window.__rawFetch = window.fetch.bind(window);
  window.fetch = async function(...args){
    // Snapshot the request before the first fetch can consume its body.  On retry we construct a
    // fresh Request and replace an existing CSRF header with the token minted by inline login.
    // This is what lets the *interrupted mutation itself* finish, instead of merely making the
    // next button click work.
    let retryRequest = null;
    try{
      retryRequest = (typeof Request !== 'undefined' && args[0] instanceof Request)
        ? new Request(args[0].clone(), args[1]) : new Request(args[0], args[1]);
    }catch(e){}
    const r = await window.__rawFetch(...args);
    // The login POST itself legitimately answers 401 for a wrong password; don't recurse on it.
    const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
    if (r.status === 401 && !url.endsWith('/login')){
      await onAuthExpired();
      if (retryRequest){
        const headers = new Headers(retryRequest.headers);
        if (headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', window.CSRF_TOKEN || '');
        return window.__rawFetch(new Request(retryRequest, {headers}));
      }
      return window.__rawFetch(...args);
    }
    return r;
  };
}

/* ── /api/ref cache ────────────────────────────────────────────────────────────────────────
   `light=1` skips the grounded opinion, which is a Vertex call for any reference that has none
   cached yet. Panes that only need text ask for light; the opinion and the full detail view ask
   for the real thing. A light response is upgraded in place once a full one arrives. */
const REFCACHE = {};
const REFPENDING = {};
const PREVIEWPENDING = {};
const SECTIONCACHE = {};

async function fetchTimed(url, timeoutMs = 12000){
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try { return await fetch(url, {signal: controller.signal}); }
  finally { clearTimeout(timer); }
}

async function fetchSection(pub, section){
  const key = pub + ':' + section;
  if (SECTIONCACHE[key]) return SECTIONCACHE[key];
  const url = B + '/api/ref-batch/' + encodeURIComponent(window.SLUG || '') +
    '?pubs=' + encodeURIComponent(pub) + '&section=' + encodeURIComponent(section);
  try{
    const r = await fetchTimed(url, 5000);
    if (!r.ok) throw new Error('preview ' + r.status);
    const data = await r.json(), item = data.items && data.items[pub];
    if (!item) throw new Error('preview unavailable');
    SECTIONCACHE[key] = item;
    return item;
  }catch(e){}
  // Cache generation may still be finishing. The live fallback is still section-shaped on the
  // wire and has its own hard deadline; it never silently turns one click into a full document.
  const live = B + '/api/ref/' + encodeURIComponent(pub) + '?slug=' +
    encodeURIComponent(window.SLUG || '') + '&light=1&section=' + encodeURIComponent(section);
  const r = await fetchTimed(live, 12000);
  if (!r.ok) throw new Error('section ' + r.status);
  SECTIONCACHE[key] = await r.json();
  return SECTIONCACHE[key];
}

async function fetchWhy(pub){
  const key = pub + ':why';
  if (SECTIONCACHE[key] && SECTIONCACHE[key].rationale) return SECTIONCACHE[key];
  const url = B + '/api/ref/' + encodeURIComponent(pub) + '?slug=' +
    encodeURIComponent(window.SLUG || '') + '&light=1&rationale=1&section=why';
  const r = await fetchTimed(url, 20000);
  if (!r.ok) throw new Error('rationale ' + r.status);
  SECTIONCACHE[key] = await r.json();
  return SECTIONCACHE[key];
}
async function fetchPreview(pub){
  if (REFCACHE[pub]) return REFCACHE[pub];
  if (PREVIEWPENDING[pub]) return PREVIEWPENDING[pub];
  PREVIEWPENDING[pub] = (async () => {
    const r = await fetch(B + '/api/ref-batch/' + encodeURIComponent(window.SLUG || '') +
                          '?pubs=' + encodeURIComponent(pub));
    if (!r.ok) return null;
    const data = await r.json(), item = data.items && data.items[pub];
    if (!item) return null;
    item._full = false; item._rationale = false; item._preview = true;
    if (!REFCACHE[pub]) REFCACHE[pub] = item;
    return REFCACHE[pub];
  })();
  try { return await PREVIEWPENDING[pub]; }
  finally { delete PREVIEWPENDING[pub]; }
}
async function fetchRef(pub, light, rationaleOnly){
  const hit = REFCACHE[pub];
  if (hit && (rationaleOnly ? hit._rationale : (!light ? hit._full : true))) return hit;
  if (light && !rationaleOnly){
    try { const preview = await fetchPreview(pub); if (preview) return preview; } catch (e) {}
  }
  const key = pub + (rationaleOnly ? ':rationale' : (light ? ':light' : ':full'));
  if (REFPENDING[key]) return REFPENDING[key];
  REFPENDING[key] = (async () => {
    const url = B + '/api/ref/' + encodeURIComponent(pub) +
                '?slug=' + encodeURIComponent(window.SLUG || '') + (light ? '&light=1' : '') +
                (rationaleOnly ? '&rationale=1' : '');
    const r = await fetchTimed(url, light ? 15000 : 25000);
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
  const all = [...document.querySelectorAll('.refcard')].map(c => c.dataset.pub).filter(Boolean);
  // Partial results are provisional, so warm four; final reports warm six. The server separately
  // prepares the top eight grounded rationales and all cached tab previews after ranking.
  const pubs = all.slice(0, window.PARTIAL ? 4 : 6);
  if (!pubs.length) return;
  try {
    const q = pubs.map(encodeURIComponent).join(',');
    const r = await fetch(B + '/api/ref-batch/' + encodeURIComponent(window.SLUG) + '?pubs=' + q);
    if (r.ok){
      const data = await r.json();
      Object.entries(data.items || {}).forEach(([pub, item]) => {
        if (!REFCACHE[pub]){
          item._full = false; item._rationale = false; item._preview = true;
          REFCACHE[pub] = item;
          if (item.display && item.display.images) backfillThumb(pub, item.display.images);
        }
      });
    }
  } catch (e) {}
  const missing = pubs.filter(pub => !REFCACHE[pub]);
  let i = 0;
  const worker = async () => {
    while (i < missing.length){
      const pub = missing[i++];
      try {
        const j = await fetchRef(pub, true);
        if (j && j.display && j.display.images) backfillThumb(pub, j.display.images);
      } catch (e) {}
    }
  };
  await Promise.all([worker(), worker(), worker()]);   // 3-wide: instant tabs, gentle on the box
}

/* A partial card can initially have no drawing because /api/figs is intentionally disk-only.
   Previously only the first four cards were warmed, so a later card could say "no drawing" while
   opening its full view immediately recovered several real figures.  Resolve those missing partial
   thumbnails in the background with the section-only endpoint: two workers, a 12 s hard deadline,
   and no full-document payload.  Final reports are handled durably by the server prefetch worker,
   including when the browser has already been closed. */
async function warmMissingThumbs(){
  if (!window.PARTIAL || !window.SLUG) return;
  const pubs = [...document.querySelectorAll('.refcard')].filter(card => {
    const thumb = card.querySelector('.rthumb');
    return thumb && ['none','error'].includes(thumb.dataset.state);
  }).map(card => card.dataset.pub).filter(Boolean);
  let i = 0;
  const worker = async () => {
    while (i < pubs.length){
      const pub = pubs[i++];
      try {
        const url = B + '/api/ref/' + encodeURIComponent(pub) + '?slug=' +
          encodeURIComponent(window.SLUG) + '&light=1&section=figs';
        const r = await fetchTimed(url, 12000);
        if (!r.ok) continue;
        const item = await r.json();
        if (item.display && item.display.images && item.display.images.length){
          SECTIONCACHE[pub + ':figs'] = item;
          backfillThumb(pub, item.display.images);
        }
      } catch (e) {}
    }
  };
  await Promise.all([worker(), worker()]);
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
  let h = '<p class="gridline">Each row keeps an uploaded claim intact, and each cell carries the ' +
    'passage the verdict rests on — click a cell for the whole quotation. A green cell has a ' +
    'grounded quotation and survived a separate refutation pass; it is not a legal ' +
    'claim-construction conclusion. ' +
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
  h += '<div class="chartwrap"><table class="chart query-claim-chart readgrid"><caption class="vh">Which ranked references disclose each uploaded patent claim</caption><thead><tr>' +
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
      const q = cell.quote || '';
      const tip = [v, cell.location, q].filter(Boolean).join(' · ').slice(0, 600);
      // "partial" on its own says a verdict was reached and nothing about what it rests on. The
      // cell now carries the passage, clipped to what fits, and opens the whole one on click.
      h += '<td class="cell cell-' + cls[v] + '"><button type="button" class="qcell"' +
        ' data-pub="' + esc(cell.pub) + '" data-el="Claim ' + esc(row.claim_no) + '"' +
        ' data-verdict="' + esc(cell.verdict || v) + '" data-loc="' + esc(cell.location || '') + '"' +
        ' data-quote="' + esc(q) + '" data-note="' + esc(cell.note || '') + '"' +
        ' onclick="showQuote(this)" title="' + esc(tip) + '"><span class="cmark">' +
        marks[v] + '</span><span class="cs">' + esc(v) + '</span>' +
        (q ? '<span class="cq">' + esc(q.length > 110 ? q.slice(0, 110) + '…' : q) + '</span>' : '') +
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
  analysis: paneAnalysis,
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

// Pointer/keyboard intent gets priority over the background queue. On a slow connection this
// usually completes while the user is moving from the tab label to the content pane.
function warmIntendedTab(ev){
  const b = ev.target.closest && ev.target.closest('.rtabs button[data-t]');
  if (!b) return;
  const card = b.closest('.refcard');
  if (card && card.dataset.pub){
    if (b.dataset.t === 'why') fetchWhy(card.dataset.pub).catch(() => {});
    else if (b.dataset.t === 'analysis') fetchDeep(card.dataset.pub).catch(() => {});
    else if (['abstract','claims','desc','class','figs'].includes(b.dataset.t))
      fetchSection(card.dataset.pub, b.dataset.t).catch(() => {});
    else if (!REFCACHE[card.dataset.pub]) fetchRef(card.dataset.pub, true).catch(() => {});
  }
}
document.addEventListener('pointerover', warmIntendedTab, {passive:true});
document.addEventListener('focusin', warmIntendedTab);

async function rtab(btn){
  const card = btn.closest('.refcard'), t = btn.dataset.t, p = paneOf(card);
  const wasOpen = btn.getAttribute('aria-expanded') === 'true';
  card.querySelectorAll('.rtab[data-t]').forEach(b => b.setAttribute('aria-expanded', 'false'));
  if (wasOpen){ p.classList.remove('open'); p.hidden = true; return; }
  btn.setAttribute('aria-expanded', 'true');
  p.classList.add('open'); p.hidden = false;
  p.setAttribute('aria-busy', 'true');
  p.innerHTML = '<span class="ploading"><span class="spin sm" aria-hidden="true"></span> loading…</span>';
  try{
    const fn = RPANES[t];
    if (!fn){ p.innerHTML = ''; return; }
    const html = await fn(card, p);
    if (html != null) p.innerHTML = html;         // a pane may mount itself and return null
    hlNode(p);
  }catch(e){
    p.innerHTML = '<div class="pempty">Couldn\'t load this section. ' +
      '<button type="button" class="linkish pane-retry" data-tab="' + esc(t) + '">Retry</button> or ' +
      '<a href="' + gp(card.dataset.pub) + '" target="_blank" rel="noopener">open on Google Patents</a>.</div>';
  }finally{ p.removeAttribute('aria-busy'); }
}

document.addEventListener('click', function(ev){
  const retry = ev.target.closest && ev.target.closest('.pane-retry');
  if (!retry) return;
  const card = retry.closest('.refcard');
  const tab = card && card.querySelector('.rtab[data-t="' + retry.dataset.tab + '"]');
  if (tab){ tab.setAttribute('aria-expanded', 'false'); rtab(tab); }
});

/*  THE ANALYSIS PANE — what this reference actually discloses, feature by feature and, when the
    search started from a patent, claim by claim.

    Every cell shows the verbatim quote and the real location (claim 7, paragraph 41) that code
    resolved from that quote. A cell the refuting pass would not confirm is shown as UNCERTAIN
    with the refuter's reason, not quietly left green: the point of the table is that a reader can
    disagree with it, and for that they need the quote, the place, and the doubt.                */
const DEEPCACHE = {};

async function fetchDeep(pub){
  if (DEEPCACHE[pub]) return DEEPCACHE[pub];
  const r = await fetch(B + '/api/deep/' + encodeURIComponent(window.SLUG) +
                        '?pub=' + encodeURIComponent(pub), {credentials: 'same-origin'});
  const d = await r.json();
  if (d.status === 'done') DEEPCACHE[pub] = d;
  return d;
}

function verdictCell(row){
  const v = esc(row.verdict || 'absent');
  const cls = 'vt vt-' + v;
  let cell = '<td class="' + cls + '"><b>' + v + '</b>';
  if (row.refuted) cell += '<span class="vtwhy" title="' + esc(row.refuted) +
    '">checked and downgraded</span>';
  cell += '</td>';
  let quote = '<td class="vq">';
  if (row.quote){
    quote += '<q>' + esc(row.quote) + '</q>';
    if (row.location) quote += '<span class="vloc">' + esc(row.location) + '</span>';
    if (row.note) quote += '<span class="vnote">' + esc(row.note) + '</span>';
  } else {
    const why = {
      'model-absent': 'nothing in this reference teaches it',
      'dropped-ungrounded-quote': 'the reader offered a quote that is not in this reference — dropped',
      'dropped-unlocatable-quote': 'the quote could not be traced to a passage — dropped',
      'no-reference-text': 'the corpus holds no text for this publication',
      'no-row-returned': 'no answer was returned for this row',
    }[row.grounding] || 'not disclosed';
    quote += '<span class="vnone">' + esc(why) + '</span>';
  }
  quote += '</td>';
  return cell + quote;
}

function deepTable(caption, rows){
  if (!rows || !rows.length) return '';
  return '<div class="deeptbl"><b class="deepcap">' + esc(caption) + '</b>' +
    '<table><thead><tr><th>' + (rows[0].kind === 'claim' ? 'Subject claim' : 'Feature') +
    '</th><th>Verdict</th><th>What this reference discloses, and where</th></tr></thead><tbody>' +
    rows.map(r => '<tr><td class="vi">' + esc(r.item) + '</td>' + verdictCell(r) + '</tr>').join('') +
    '</tbody></table></div>';
}

async function paneAnalysis(card){
  const pub = card.dataset.pub;
  const d = await fetchDeep(pub);
  if (d.status === 'running'){
    const n = d.done || 0, t = d.total || 0;
    return '<div class="pempty">Reading the top references in full — ' + n + ' of ' + t +
      ' done. This reference has not been read yet; reopen this tab in a moment.</div>';
  }
  if (d.status === 'error')
    return '<div class="pempty">The full-text analysis failed: ' + esc(d.error || '') + '</div>';
  if (d.status !== 'done' || !d.found)
    return '<div class="pempty">This reference was not among the ones read in full ' +
      '(the deepest ' + esc(String(window.DEEP_TOP_N || 50)) + ' are).</div>';

  const r = d.reference;
  let head = '<div class="deephead">';
  if (r.method === 'no-text')
    head += '<span class="vwarn">The local corpus holds no text for this publication, so nothing ' +
      'could be read. Open it at the office to judge it.</span>';
  else
    head += '<span class="muted small">Read ' + Number(r.chars || 0).toLocaleString() +
      ' characters — ' + (r.n_claims_read || 0) + ' claims and ' + (r.n_paragraphs_read || 0) +
      ' description paragraphs' + (r.text_truncated ? ', truncated at the text budget' : '') + '.' +
      (r.refuted ? ' ' + r.refuted + ' cell(s) downgraded by the refuting check.' : '') + '</span>';
  head += '</div>';

  const body = deepTable('Features of the search input', r.features) +
               deepTable('Claims of the subject patent', r.claims);
  return head + (body || '<div class="pempty">Nothing to chart for this reference.</div>');
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
  const j = await fetchSection(pub, 'abstract');
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
  const j = await fetchSection(pub, 'claims');
  const s = j.sections || {};
  if (!s.claims || !s.claims.length) return paneMissing(pub, 'claims', j.display);
  return '<h4>Claims (' + s.claims.length + ')</h4>' +
    '<div class="scrollbox">' + claimsHTML(s.claims, j.matched) + '</div>';
}

async function paneDesc(card){
  const pub = card.dataset.pub;
  const j = await fetchSection(pub, 'desc');
  const s = j.sections || {};
  if (!s.paragraphs || !s.paragraphs.length) return paneMissing(pub, 'description text', j.display);
  return '<h4>Description (' + s.paragraphs.length + ' paragraphs)</h4>' +
    '<div class="scrollbox">' + parasHTML(s.paragraphs, j.matched) + '</div>';
}

async function paneClass(card){
  const pub = card.dataset.pub;
  const j = await fetchSection(pub, 'class');
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
  const j = await fetchWhy(card.dataset.pub);
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
      r.reads_on.map(x => '<span class="chip">' + esc(x) + '</span>').join('') + '</div>';
  return h;
}

async function paneFigs(card){
  const pub = card.dataset.pub;
  const j = await fetchSection(pub, 'figs');
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
      '<figure><button type="button" class="figbutton" onclick="openLb(this.querySelector(\'img\'))" ' +
      'aria-label="Open ' + esc(kind + (i + 1) + ' of ' + pub) + '"><img loading="lazy" decoding="async" src="' + esc(figThumb(pub, im)) + '" ' +
      'data-full="' + esc(figFull(pub, im)) + '" ' +
      'alt="' + esc(kind + (i + 1) + ' of ' + pub) + '" data-pub="' + esc(pub) + '"></button>' +
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
  b.firstChild.nodeValue = open ? 'Show less' : 'Show full search';
  // The same control now also reveals every setting the search ran with (#qparams): the query
  // string alone is the one input the searcher already had.
  const p = document.getElementById('qparams');
  if (p) p.hidden = !open;
  if (!open) w.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/* ── the passage behind a grid cell ──────────────────────────────────────────────────────────
   Both grids have to fit a quote into a table cell, so both truncate. The evidence is the whole
   passage, the location it came from and the reader's note, and none of that fits — so the cell
   shows what it can and this shows the rest. One dialog for every cell in both grids. */
const QVERDICT = { disclosed: 'Discloses', partial: 'Partial', uncertain: 'Unconfirmed',
                   absent: 'Absent' };
function showQuote(btn){
  const pop = document.getElementById('qpop');
  if (!pop || !btn) return;
  const d = btn.dataset, verdict = d.verdict || 'disclosed';
  const setText = (id, s) => { const n = document.getElementById(id); if (n) n.textContent = s || ''; };
  const v = document.getElementById('qpopVerdict');
  if (v){ v.textContent = QVERDICT[verdict] || verdict; v.className = 'qpop-v qpop-' + verdict; }
  setText('qpopEl', d.el || '');
  setText('qpopQuote', d.quote ? '“' + d.quote + '”' : 'No quotable passage was recorded for this cell.');
  // The second-pass note matters: it says the first reading missed this and a concept-led re-read
  // found it, which is the difference between "we looked" and "we looked twice".
  setText('qpopNote', (d.note || '') + (d.second === 'true'
    ? (d.note ? ' ' : '') + 'Found on the second, concept-led reading of this reference.' : ''));
  setText('qpopLoc', [d.pub, d.loc].filter(Boolean).join(' · '));
  /* A cell whose passage the corpus holds in another language: fetch it with its machine
     translation the moment somebody actually looks, never at page build (a report carries
     hundreds of these). The translation is labelled; it never impersonates a verbatim quote. */
  if (!d.quote && /non-English passage/.test(d.note || '') && d.pub && d.loc){
    const q = document.getElementById('qpopQuote'), noteEl = document.getElementById('qpopNote');
    if (q){
      q.textContent = 'Fetching the passage and its machine translation…';
      fetch(B + '/api/passage?pub=' + encodeURIComponent(d.pub) +
            '&loc=' + encodeURIComponent(d.loc), {credentials: 'same-origin'})
        .then(function (r) { return r.json(); })
        .then(function (p) {
          if (!p.found){
            q.textContent = 'No quotable passage was recorded for this cell.';
            return;
          }
          if (p.translation){
            q.textContent = '\u201c' + p.translation + '\u201d';
            if (noteEl){
              const orig = p.original.length > 700 ? p.original.slice(0, 700) + '\u2026' : p.original;
              noteEl.textContent = 'Machine translation' + (p.lang ? ' from ' + p.lang : '') +
                ', not a verbatim quotation. ' + (d.note || '') + ' Original: ' + orig;
            }
          } else {
            q.textContent = '\u201c' + p.original + '\u201d';
          }
        })
        .catch(function () {
          q.textContent = 'No quotable passage was recorded for this cell.';
        });
    }
  }
  const ref = document.getElementById('qpopRef');
  if (ref){ ref.textContent = 'Go to ' + (d.pub || 'reference'); ref.dataset.pub = d.pub || ''; }
  pop.hidden = false;
  const close = document.getElementById('qpopClose');
  if (close) close.focus();
}
function closeQuote(){
  const pop = document.getElementById('qpop');
  if (pop) pop.hidden = true;
}
document.addEventListener('click', e => {
  const pop = document.getElementById('qpop');
  if (!pop || pop.hidden) return;
  if (e.target.id === 'qpopClose' || e.target === pop){ closeQuote(); return; }
  if (e.target.id === 'qpopRef'){ closeQuote(); jumpRef(e.target.dataset.pub); }
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeQuote(); });

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
  if (!img) return;
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
  const bar = document.getElementById('exportbar');
  bar.hidden = n === 0;
  bar.inert = n === 0;
  bar.classList.toggle('show', n > 0);
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
function startDraft(){
  /*  Selecting references is a shortcut, not a requirement: the drafting intake can attach art
      later, or none at all, so an empty selection opens the intake rather than refusing.       */
  const sel = selectedPubs();
  const query = new URLSearchParams({search_slug: window.SLUG});
  if (sel.length) query.set('pubs', sel.join(','));
  location.href = B + '/drafts/start?' + query.toString();
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
      method: 'POST', headers: { 'Content-Type': 'application/json',
        'X-CSRF-Token': window.CSRF_TOKEN || '' },
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
  overlay().hidden = false;
  overlay().inert = false;
  overlay().setAttribute('aria-hidden', 'false');
  overlay().classList.add('open');
  soSetBackgroundInert(true);
  document.body.style.overflow = 'hidden';
  document.getElementById('soClose').focus();
  const body = document.getElementById('soBody');
  body.innerHTML = '<div class="so-loading"><span class="spin" aria-hidden="true"></span>' +
    '<div>Loading ' + esc(pn) + ' — drawings, claims, citations…</div></div>';
  let shown = false;
  try{
    const preview = REFCACHE[pn] || await fetchPreview(pn);
    if (preview){ renderDetail(pn, preview); shown = true; }
    const full = await fetchRef(pn);
    if ((detailStack[detailStack.length - 1] || '').replace(/ · similar$/, '') === pn)
      renderDetail(pn, full);
  }
  catch(e){
    if (shown) return;
    body.innerHTML = '<div class="so-loading"><div>Couldn\'t load ' + esc(pn) + '.</div>' +
      '<a class="btn ghost sm" href="' + gp(pn) + '" target="_blank" rel="noopener">Open on Google Patents</a></div>';
  }
}
function closeDetail(){
  overlay().classList.remove('open');
  overlay().inert = true;
  overlay().setAttribute('aria-hidden', 'true');
  overlay().hidden = true;
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
      '<button type="button" class="gzoom" onclick="openLb(this.querySelector(\'img\'))" aria-label="Open current drawing full size">' +
      '<img id="gMain" src="' + imgs[0] + '" data-pub="' + esc(pn) + '" ' +
      'alt="' + esc(kind + '1 of ' + pn) + '"></button>' +
      (imgs.length > 1 ? '<button class="gnav r" onclick="galNext()" aria-label="Next figure">›</button>' : '') +
      '<span class="gcount" id="gCount">1 / ' + imgs.length + '</span></div>';
    if (imgs.length > 1)
      h += '<div class="gthumbs">' + imgs.map((u, i) =>
        '<button type="button" onclick="galSet(' + i + ')" aria-label="Show ' + esc(kind + (i + 1)) + '">' +
        '<img src="' + u + '" alt="' + esc(kind + (i + 1) + ' of ' + pn) + '" class="' + (i === 0 ? 'sel' : '') +
        '" loading="lazy"></button>').join('') + '</div>';
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
const LIVE_CORPUS_CLAIMS = Number((typeof window !== 'undefined' && window.CORPUS_CLAIMS_PUBS) || 0);
/*  THIS SENTENCE SAID SOMETHING FALSE until 2026-08-25. It read "N publications with full claims
    and description text", where N is every publication in the corpus. MEASURED: N is 4,984,254 and
    the number with parsed claims is 814,523, which is 16%; description text is rarer still. An
    attorney reading it would believe the run had searched five million patents' claims. The count
    now comes from the same snapshot /corpus shows, and the sentence says only what is true: how
    many publications there are, and how many of them the search can read claims for. */
const LIVE_CORPUS_NOTE = (LIVE_CORPUS_N
  ? 'Our own pgvector corpus of ' + LIVE_CORPUS_N.toLocaleString() + ' publications'
    + (LIVE_CORPUS_CLAIMS
        ? ', ' + LIVE_CORPUS_CLAIMS.toLocaleString() + ' of them with parsed claim text and the '
          + 'rest reached by title, abstract and classification, '
        : ', ')
  : 'Our own pgvector corpus, ')
  + 'searched through eight channels at once: dense and claim-dense embeddings, sparse BM25 over '
  + 'claims and full text, CPC classification, backward and forward citations, query-by-example '
  + 'and cross-lingual EN/DE.';
/*  THE STAGE LIST IS WHAT THE USER BELIEVES IS HAPPENING, so it has to match what is.

    It did not. Two defects, both visible on a five-hour run:
      * `KIND_RANK` topped out at `reranking`, so the deep read and the orphan-claim rescue — which
        together are the overwhelming majority of a search's wall clock — had no stage at all. The
        page parked on "Reranking and grounding" and its note for over two hours while the elapsed
        timer counted, which reads as a hang and describes the wrong work entirely.
      * the rerank note said "the closest 25 references" when retrieval.RERANK_TOP is 50, and the
        corpus note listed a handful of channels as though the local index were the whole search.  */
const STAGES = [
  { key: 'decompose', rank: 0, name: 'Reading the disclosure',
    note: 'Extracting the claims verbatim, splitting them into their separate limitations, and condensing the document into a search brief.' },
  { key: 'search',    rank: 1, name: 'Searching our corpus',
    note: LIVE_CORPUS_NOTE },
  { key: 'expand',    rank: 2, name: 'Expanding the candidate set',
    note: 'Following citations, patent families and EN/DE equivalents outward from the seed hits, plus the query document\'s own text chunks and a drawing-image match against the corpus figure index.' },
  { key: 'federate',  rank: 3, name: 'Every external source, in parallel',
    note: 'BigQuery Google Patents (all 170M publications), SerpApi Google Patents, PQAI, EPO OPS, USPTO ODP, OpenAlex, HimmPat (CN/JP/KR with English full text), IP Australia, Lens.org and KIPRIS — whichever are configured — fused with the local results by reciprocal rank.' },
  { key: 'rounds',    rank: 4, name: 'Refinement rounds',
    note: 'Re-querying on the limitations that are still uncovered, until new families stop appearing.' },
  { key: 'screen',    rank: 5, name: 'Screening the candidates',
    note: 'Every candidate scored 0-100 from its title, abstract and first claims, to decide which are worth reading in full.' },
  { key: 'read',      rank: 6, name: 'Reading references in full',
    note: 'Each selected reference read end to end against every claim limitation and disclosure. Every cell carries a verbatim quote that must be found in the reference and located in a specific passage, then survive an independent refuter. This is the longest stage.' },
  { key: 'rescue',    rank: 7, name: 'Going back for uncovered claims',
    note: 'Any limitation still without prior art gets its own search, and what comes back is read in full against the same checklist.' },
  { key: 'rerank',    rank: 8, name: 'Reranking and grounding',
    note: 'A bge-reranker cross-encoder rescores the closest ' + (window.RERANK_TOP || 50) + ' references, then the page is ordered by rarity-weighted grounded evidence and by how many claims each reference answers.' },
  { key: 'done',      rank: 9, name: 'Report ready', note: '' }
];
const KIND_RANK = {
  elements: 1, search_progress: 1, seeded: 2, seed_progress: 2, partial: 2,
  federating: 3, round: 4, round_progress: 4,
  screen_start: 5, screen_progress: 5,
  claim_reach_start: 6, claim_reach_progress: 6, chart_progress: 6, reread_start: 6,
  batch_read_progress: 6,
  rescue_start: 7, rescue_reread_start: 7, rescue_search_start: 7,
  rescue_search_progress: 7, rescue_read_start: 7, rescue_read_progress: 7,
  worldset_build_start: 7, worldset_built: 7, worldset_ingest_start: 7, limq_build_start: 7,
  reranking: 8, rerank_progress: 8, done: 9
};
function createProgress(mount, opts){
  opts = opts || {};
  const wide = !!opts.wide;
  const stages = STAGES.filter(s => s.key !== 'federate' || wide);
  const DONE_RANK = STAGES[STAGES.length - 1].rank;   // was hardcoded 6; the list is longer now
  const state = { rank: 0, detail: {}, since: Date.now(), started: Date.now(), msg: '',
                  tokens: 0, elapsed: 0, elapsedTotal: 0, attempt: 0, feed: [], feedSeen: {} };
  /*  ONE LINE, NOT NINE ROWS. The checklist made sense when a stage could hold the page for two
      hours: the list told you where you were in a long run. The search is fast now, so the list
      is nine paragraphs of text that never change wrapped around one that does, and the reader
      has to find the live row to learn anything. This renders only the CURRENT stage, which is
      the only row that was ever carrying information. */
  /*  AND ONE LINE MEANS ONE LINE. It still rendered the stage NAME and then the stage NOTE
      underneath it, and the note is two sentences of explanation plus the counters plus the
      running clock, which wrapped to three more. Stacked with the banner's own message and title
      that was five lines of moving text above a results list.

      What a person watching a search needs on the page is one line: what is happening now, with
      its numbers and how long it has been going. Everything else is worth exactly one click, so
      it is behind one: the stage's explanation and the whole run narrative, every stage with its
      own note, which is the checklist this used to be.  */
  /*  WHAT IS HAPPENING, NOT WHAT THE PIPELINE IS. The panel behind the chevron used to be the
      nine stages with their two-sentence notes: 1,600 characters of fixed prose that is identical
      on every run and on every report, opened by somebody who wanted to know why the counter had
      not moved in ten minutes. It answered a question nobody was asking.

      It is a feed now: the references as they are read, newest first, with what came back from
      each. The stage notes are still there, under it, for the one reading in which they help. */
  mount.innerHTML = '<div class="stagenow">' +
    '<button type="button" class="st-toggle" aria-expanded="false">' +
      '<span class="st-chev" aria-hidden="true"></span>' +
      '<span class="st-line"></span></button>' +
    '<div class="st-detail" hidden><ol class="st-feed" reversed></ol>' +
      '<p class="st-note"></p><ol class="st-all"></ol></div></div>' +
    /* The overall clock lives INSIDE the component: the generating page has its own #elapsed
       footer, but most of a long run is watched from the report page's refining banner, which
       had no counter at all — "no overall time and token counter" was reported verbatim. */
    '<div class="pgclock cc" style="margin-top:6px" aria-live="off"></div>';

  function facts(){
    const d = state.detail, out = [];
    if (d.elements) out.push(d.elements + ' element' + (d.elements !== 1 ? 's' : '') + ' identified');
    if (d.families) out.push(d.families.toLocaleString() + ' candidate families');
    if (d.round) out.push('round ' + d.round);
    /*  WHAT IS DOING THE WORK AND HOW FAST. Reading is 97% of the bill and the only stage that
        scales, so while it runs the line names the model and the measured rate rather than only
        counting documents. The rate is this run's own tokens over its own elapsed time, so it is
        what the search is actually getting rather than a quoted figure. */
    if (state.rank >= 6 && state.rank <= 7) {
      if (window.READ_MODEL) { out.push(window.READ_MODEL); }
      var secs = state.elapsed || Math.round((Date.now() - state.started) / 1000);
      if (state.tokens > 0 && secs > 5) {
        out.push(Math.round(state.tokens / secs).toLocaleString() + ' tok/s');
      }
    }
    if (state.rank <= 3 && d.search_done) {
      out.push(d.search_done + ' of up to ' + d.search_max + ' retrieval passes');
      if (d.search_seconds != null) out.push('last pass ' + Number(d.search_seconds).toFixed(1) + 's');
    }
    return out;
  }
  const tog = mount.querySelector('.st-toggle');
  if (tog){
    tog.addEventListener('click', function(){
      const box = mount.querySelector('.st-detail');
      const open = tog.getAttribute('aria-expanded') === 'true';
      tog.setAttribute('aria-expanded', open ? 'false' : 'true');
      if (box) box.hidden = open;
    });
  }

  function paint(){
    //  Only the stage actually running is drawn. Its own clock keeps ticking so a quiet server
    //  still shows movement rather than reading as a hang.
    const cur0 = stages.find(s => s.rank === state.rank) || stages[stages.length - 1];
    const lineEl = mount.querySelector('.stagenow .st-line');
    const noteEl = mount.querySelector('.stagenow .st-note');
    const allEl  = mount.querySelector('.stagenow .st-all');
    if (lineEl){
      /*  The server's own message when there is one: "Screening candidates: batch 8 of 25" says
          more than "Screening the candidates" and is the same length. The stage name is the
          fallback, not the headline. */
      const bits = facts();
      const head = (state.msg && state.msg !== 'done') ? state.msg : cur0.name;
      lineEl.textContent = head +
        (bits.length ? ' · ' + bits.join(' · ') : '') +
        (cur0.rank < DONE_RANK ? ' · ' + fmtDur(Date.now() - state.since) : '');
    }
    if (noteEl) noteEl.textContent = cur0.note;
    const feedEl = mount.querySelector('.stagenow .st-feed');
    if (feedEl){
      /*  Newest first, because the interesting one is the one that just landed. Rebuilt whole:
          the list is capped at 25 server-side, so this is 25 <li>s once a second at worst. */
      if (!state.feed.length){
        feedEl.hidden = true;
      } else {
        feedEl.hidden = false;
        feedEl.innerHTML = state.feed.slice().reverse().map(function (r){
          const bits = [];
          if (r.chars > 0) bits.push(r.chars >= 1000 ? Math.round(r.chars / 1000) + 'k chars'
                                                     : r.chars + ' chars');
          if (r.n_features > 0) bits.push(r.n_features + ' limitation'
                                          + (r.n_features === 1 ? '' : 's') + ' answered');
          if (r.reused) bits.push('reused from this run');
          if (r.found === false) bits.push('no full text');
          if (r.note) bits.unshift(r.note);
          return '<li><b>' + esc(r.pub) + '</b>'
               + (r.title ? ' <span class="ttl">' + esc(r.title) + '</span>' : '')
               + (bits.length ? ' <span class="muted">' + esc(bits.join(' · ')) + '</span>' : '')
               + '</li>';
        }).join('');
      }
    }
    if (allEl && allEl.childElementCount !== stages.length){
      allEl.innerHTML = stages.map(s =>
        '<li data-k="' + s.key + '"><b></b><span></span></li>').join('');
      stages.forEach((s, i) => {
        const li = allEl.children[i];
        li.querySelector('b').textContent = s.name;
        li.querySelector('span').textContent = s.note;
      });
    }
    if (allEl){
      stages.forEach((s, i) => {
        const li = allEl.children[i];
        if (li) li.className = s.rank < state.rank ? 'done'
                             : (s.rank === state.rank ? 'now' : '');
      });
    }
    const live = mount.parentElement && mount.parentElement.querySelector('[data-progress-live]');
    if (live){
      const cur = stages.find(s => s.rank === state.rank) || stages[stages.length - 1];
      const bits = facts();
      live.textContent = cur.name + (bits.length ? ' — ' + bits.join(', ') : '');
    }
    const bar = document.getElementById('bar');
    if (bar) bar.style.width = Math.min(97, (state.rank / DONE_RANK) * 100 + 6).toFixed(0) + '%';
    const el = document.getElementById('elapsed');
    if (el){
      //  The server's own clock wins when it has one: this page may have been opened long after
      //  the search started, and a client-side stopwatch would then read minutes short.
      const secs = state.elapsed || Math.round((Date.now() - state.started) / 1000);
      let txt = fmtDur(secs * 1000) + ' elapsed';
      if (state.tokens > 0){
        const t = state.tokens;
        txt += ' · ~' + (t >= 1e6 ? (t / 1e6).toFixed(1) + 'M' :
                         t >= 1e3 ? Math.round(t / 1e3) + 'k' : t) + ' tokens';
      }
      el.textContent = txt;
      el.title = state.tokens > 0
        ? 'Estimated model tokens spent by this search so far (prompt + completion). Approximate: '
          + 'the counter is process-wide, so a second search running at the same time is counted here too.'
        : '';
    }
    const clock = mount.querySelector('.pgclock');
    if (clock){
      const attemptSecs = state.elapsed || Math.round((Date.now() - state.started) / 1000);
      const totalSecs = Math.max(state.elapsedTotal || 0, attemptSecs);
      let txt = '⏱ ' + fmtDur(totalSecs * 1000) + ' overall';
      if (state.attempt > 1 && attemptSecs < totalSecs)
        txt += ' · this attempt ' + fmtDur(attemptSecs * 1000);
      if (state.tokens > 0){
        const t = state.tokens;
        txt += ' · ~' + (t >= 1e6 ? (t / 1e6).toFixed(1) + 'M' :
                         t >= 1e3 ? Math.round(t / 1e3) + 'k' : t) + ' tokens this attempt';
      }
      if (state.attempt > 1)
        txt += ' · attempt ' + state.attempt +
               ' (restarted by a server update — banked reading is reused)';
      clock.textContent = txt;
    }
  }
  paint();
  const timer = setInterval(paint, 1000);

  return {
    apply(ev){
      const r = KIND_RANK[ev.kind];
      if (r != null && r > state.rank){ state.rank = r; state.since = Date.now(); }
      /*  THE FEED, before the merge: `detail` is cumulative (Object.assign), so a `pub` that
          arrived three events ago is still sitting in it and cannot be told from a new one. The
          server sends the whole rolling log, so a page opened mid-run is caught up rather than
          starting empty; entries are keyed so a re-render never duplicates a row. */
      const log = ev.read_log;
      if (Array.isArray(log)){
        log.forEach(function (r){
          const k = (r && r.pub) + '#' + (r && r.n);
          if (!r || !r.pub || state.feedSeen[k]) return;
          state.feedSeen[k] = 1;
          state.feed.push(r);
        });
        if (state.feed.length > 25) state.feed = state.feed.slice(-25);
      }
      if (ev.detail) Object.assign(state.detail, ev.detail);
      if (typeof ev.tokens === 'number' && ev.tokens > state.tokens) state.tokens = ev.tokens;
      if (typeof ev.elapsed_sec === 'number' && ev.elapsed_sec > state.elapsed) state.elapsed = ev.elapsed_sec;
      if (typeof ev.elapsed_total_sec === 'number' && ev.elapsed_total_sec > state.elapsedTotal)
        state.elapsedTotal = ev.elapsed_total_sec;
      if (typeof ev.attempt === 'number' && ev.attempt > state.attempt) state.attempt = ev.attempt;
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
/*  SAVED PATENTS — the star on each card.

    A triage flag belongs to this report; a saved patent belongs to the person. The two look
    similar on screen and are deliberately separate underneath: clearing a flag must not remove a
    document somebody chose to keep. The current state is fetched once per report render rather
    than per card, so a page of 25 costs one request.                                            */
function bindLibrary(){
  const btns = [...document.querySelectorAll('.libbtn')];
  if (!btns.length) return;

  function paint(btn, saved){
    btn.classList.toggle('on', !!saved);
    btn.setAttribute('aria-pressed', saved ? 'true' : 'false');
    btn.textContent = saved ? '★' : '☆';
    btn.title = saved ? 'Saved — press to remove from your saved patents'
                      : 'Keep this publication in your saved patents';
  }

  fetch(B + '/api/library/state?pubs=' + encodeURIComponent(btns.map(b => b.dataset.pub).join(',')),
        {credentials: 'same-origin'})
    .then(r => r.ok ? r.json() : null)
    .then(d => { if (!d) return; const set = new Set(d.saved || []);
                 btns.forEach(b => paint(b, set.has(b.dataset.pub))); })
    .catch(() => {});

  btns.forEach(btn => btn.addEventListener('click', async e => {
    e.stopPropagation();
    const pub = btn.dataset.pub;
    const wasSaved = btn.classList.contains('on');
    const card = btn.closest('.refcard');
    const title = card ? (card.querySelector('.rtitle') || {}).textContent || '' : '';
    btn.disabled = true;
    paint(btn, !wasSaved);                       // optimistic: the round trip is not the feedback
    try{
      const r = await fetch(B + '/api/library', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': window.CSRF_TOKEN || ''},
        body: JSON.stringify({action: wasSaved ? 'remove' : 'save', pub,
                              title: title.trim().slice(0, 300), slug: window.SLUG || ''})});
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
      paint(btn, !!d.saved);
    }catch(err){
      paint(btn, wasSaved);                      // put it back; do not lie about what was stored
      btn.title = 'Could not save: ' + (err.message || err);
    }finally{ btn.disabled = false; }
  }));
}

/*  REFINE AND SEARCH AGAIN — edit the query, keep the settings, start a NEW report.
    Deliberately not an in-place re-run: a different query is a different search, and overwriting
    the report somebody may already have cited is not a refinement.                              */
function bindRefine(){
  const btn = document.getElementById('qeditbtn');
  const form = document.getElementById('qeditbox');
  if (!btn || !form) return;
  const ta = document.getElementById('qeditta');
  const status = document.getElementById('qeditstatus');
  btn.addEventListener('click', () => {
    const open = !form.hidden;
    form.hidden = open;
    btn.setAttribute('aria-expanded', open ? 'false' : 'true');
    if (!open) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
  });
  document.getElementById('qeditcancel').addEventListener('click', () => {
    form.hidden = true; btn.setAttribute('aria-expanded', 'false'); btn.focus();
  });
  document.getElementById('qeditimprove').addEventListener('click', async e => {
    const b = e.target;
    b.disabled = true; const was = b.textContent; b.textContent = 'Thinking…';
    status.textContent = '';
    try{
      const r = await fetch(B + '/api/improve-query', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': window.CSRF_TOKEN || ''},
        body: JSON.stringify({query: ta.value})});
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
      if (!d.changed) { status.textContent = 'Already reads well for this engine.'; return; }
      ta.value = d.improved;
      status.textContent = 'Rewritten — read it before searching.' +
        ((d.questions || []).length ? ' Still unclear: ' + d.questions[0] : '');
    }catch(err){ status.textContent = 'Could not improve that: ' + (err.message || err); }
    finally{ b.disabled = false; b.textContent = was; }
  });
  form.addEventListener('submit', () => {
    status.textContent = 'Starting a new search…';
  });
}

/*  THE RANKED TAIL — cheap rows beyond the analysed cards. Deliberately a table, not more cards:
    these have had no drawing, claim match or explanation computed, and dressing them as cards
    would imply an analysis that did not happen.                                                 */
function bindMoreReferences(){
  const btn = document.getElementById('moreRefBtn');
  if (!btn) return;
  const wrap = document.getElementById('moreRefWrap');
  const body = document.getElementById('moreRefBody');
  const note = document.getElementById('moreRefNote');
  const esc = s => (s == null ? '' : String(s)).replace(/[&<>"]/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

  btn.addEventListener('click', async () => {
    const offset = Number(btn.dataset.offset || 25);
    btn.disabled = true;
    const was = btn.textContent;
    btn.textContent = 'Loading…';
    try{
      const r = await fetch(B + '/api/more-references/' + encodeURIComponent(window.SLUG) +
                            '?offset=' + offset, {credentials: 'same-origin'});
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
      d.rows.forEach(row => {
        const tr = document.createElement('tr');
        tr.innerHTML =
          `<td data-label="#">${row.rank}</td>` +
          `<td data-label="Publication"><b>${esc(row.pub)}</b></td>` +
          `<td data-label="Title">${esc(row.title) || '<span class="muted">no title in the corpus</span>'}</td>` +
          `<td data-label="Dates"><span>${esc(row.publication_date) || '—'}</span>` +
          `<span class="muted">priority ${esc(row.priority_date) || '—'}</span></td>` +
          `<td data-label=""><a href="${esc(row.google_patents)}" target="_blank" rel="noopener">Google ↗</a>` +
          (row.espacenet ? ` · <a href="${esc(row.espacenet)}" target="_blank" rel="noopener">Espacenet ↗</a>` : '') +
          `</td>`;
        body.appendChild(tr);
      });
      wrap.hidden = false;
      btn.dataset.offset = d.next;
      note.textContent = body.children.length + ' further references shown of ' +
        d.total.toLocaleString() + ' families ranked.';
      if (d.exhausted || !d.rows.length){
        btn.remove();
        if (!body.children.length) note.textContent =
          'No further references could be resolved — the remaining families are federated-only.';
      } else {
        btn.disabled = false;
        btn.textContent = 'Show 25 more';
      }
    }catch(err){
      note.textContent = 'Could not load more: ' + (err.message || err);
      btn.disabled = false;
      btn.textContent = was;
    }
  });
}

/*  Progress for the full-text reading. It takes minutes, so the strip says how far it has got
    and then gets out of the way; the per-reference tables are behind each card's tab.          */
function bindDeepStrip(){
  const strip = document.getElementById('deepStrip');
  if (!strip) return;
  const text = document.getElementById('deepText');
  const link = document.getElementById('deepAll');
  let tries = 0;
  async function poll(){
    if (tries++ > 240) return;
    try{
      const r = await fetch(B + '/api/deep/' + encodeURIComponent(window.SLUG),
                            {credentials: 'same-origin'});
      const d = await r.json();
      if (d.status === 'done'){
        const c = d.counts || {};
        strip.classList.add('done');
        text.textContent = 'Read ' + (d.n_analysed || 0) + ' of ' + (d.n_references || 0) +
          ' references in full — ' + (c.disclosed || 0) + ' disclosed, ' + (c.partial || 0) +
          ' partial, ' + (c.uncertain || 0) + ' uncertain, ' + (c.absent || 0) + ' absent' +
          (d.refuted ? ' (' + d.refuted + ' downgraded on checking)' : '') + '.';
        if (link) link.hidden = false;
        return;
      }
      if (d.status === 'error'){
        strip.classList.add('failed');
        text.textContent = 'The full-text reading failed: ' + (d.error || 'unknown error');
        return;
      }
      if (d.status === 'running' && d.total){
        text.textContent = 'Reading the top ' + d.total + ' references in full — ' +
          (d.done || 0) + ' done' + (d.pub ? ' (' + d.pub + ')' : '') + '…';
      }
    }catch(e){ /* a dropped poll is not a failed analysis */ }
    setTimeout(poll, 4000);
  }
  poll();
}

function guardStaticFigures(){
  document.querySelectorAll('.rthumb img, .cmpimg').forEach(img => {
    const fail = () => {
      const thumb = img.closest('.rthumb');
      if (thumb){ thumbFail(thumb); return; }
      const box = document.createElement('div');
      box.className = 'nodig';
      box.textContent = 'Drawing unavailable.';
      const button = img.closest('.cmpimgbtn');
      if (button) button.replaceWith(box); else img.replaceWith(box);
    };
    if (img.complete && img.naturalWidth === 0) fail(); else img.addEventListener('error', fail);
  });
}

/* ── init ────────────────────────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  guardStaticFigures();
  document.querySelectorAll('.directdraft').forEach(form => form.addEventListener('submit', () => {
    const button = form.querySelector('button[type=submit]');
    if (button) { button.disabled = true; button.textContent = 'Starting draft…'; }
  }));
  const compactReport = window.matchMedia && window.matchMedia('(max-width:640px)').matches;
  document.querySelectorAll('.reportoverview,.resultfilters').forEach(d => { d.open = !compactReport; });
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
  bindLibrary();
  bindRefine();
  bindMoreReferences();
  bindDeepStrip();
  document.querySelectorAll('.refcard .rsnip').forEach(hlNode);
  applyControls();
  recoverBrokenInitialThumbs();
  resolveThumbs();
  resolvePdfLinks();
  // Text tabs are a first-result feature, not a final-report feature. Warm a bounded partial head
  // immediately; the final reload replaces it with one batched cache fill for all visible cards.
  setTimeout(warmDetails, window.PARTIAL ? 250 : 700);
  if (!window.PARTIAL){
    prefetchTopN();        // proactively resolve drawings + worldwide family for shown final cards
    setTimeout(warmRationales, 2600); // deeper top-card explanations, no click required
    warmQueryClaimGrid();  // uploaded claims x references; server already runs it in background
  } else {
    setTimeout(warmMissingThumbs, 1200);
    streamNewCards();
  }

  const m = (location.hash || '').match(/patent=([^&]+)/);
  if (m){ try{ openDetail(decodeURIComponent(m[1])); }catch(e){} }
});

/* ══════════════════════════════════════════════════════════════════════════════════════════
   LIVE CARD STREAM — references appear as the agent admits them
   ══════════════════════════════════════════════════════════════════════════════════════════
   A wide search runs for minutes. It used to show one snapshot of first matches and then, at the
   very end, replace the entire page — so everything found by the refinement rounds, the citation
   and family expansion and the external fan-out was invisible until it was over, and the page
   looked frozen throughout. The agent now writes a snapshot after each stage and this asks for
   whatever is new.

   APPEND ONLY, and always the server's own card markup (templates/_refcard.html). Re-rendering
   the list would throw away opened tabs, triage flags and loaded drawings; the authoritative
   ORDER arrives separately and moves the existing nodes (reorderCards). */
function bindStreamedCards(root){
  root.querySelectorAll('.fp').forEach(b =>
    b.addEventListener('click', e => { e.stopPropagation(); setFlag(b); }));
  root.querySelectorAll('.rsnip').forEach(hlNode);
}

async function streamNewCards(){
  if (!window.PARTIAL || !window.SLUG) return;
  const host = document.getElementById('cards');
  if (!host) return;
  let quiet = 0;
  const tick = async () => {
    if (!window.PARTIAL) return;                 // the final report took over; nothing to stream
    let d = null;
    try {
      const have = host.querySelectorAll('.refcard').length;
      const r = await fetch(B + '/api/cards/' + encodeURIComponent(window.SLUG) + '?offset=' + have,
                            {credentials: 'same-origin'});
      if (r.ok) d = await r.json();
    } catch (e) {}
    if (d && d.cards){
      const frag = document.createElement('div');
      frag.innerHTML = d.cards;
      const added = [...frag.querySelectorAll('.refcard')];
      // A card can only be new to this page; the server sliced by offset, but a snapshot written
      // between two polls can renumber, so dedupe by publication before inserting either way.
      const seen = new Set([...host.querySelectorAll('.refcard')].map(c => c.dataset.pub));
      added.forEach(card => {
        if (seen.has(card.dataset.pub)) return;
        seen.add(card.dataset.pub);
        card.classList.add('justfound');
        host.appendChild(card);
        bindStreamedCards(card);
      });
      if (added.length){
        resolveThumbs(); resolvePdfLinks(); applyControls();
        const n = document.querySelector('.runfacts b');
        if (n) n.textContent = host.querySelectorAll('.refcard').length;
      }
      quiet = added.length ? 0 : quiet + 1;
    } else { quiet++; }
    if (d && d.ready) return;                    // the streamJob poll reloads onto the full report
    // Back off while nothing is arriving: between snapshots there is nothing to fetch, and the
    // server rebuilds a partial view per new snapshot rather than per request.
    setTimeout(tick, Math.min(3000 + quiet * 2000, 15000));
  };
  setTimeout(tick, 3000);
}

/* ── rebuild settings ─────────────────────────────────────────────────────────────────────────
   "Manage / rebuild" used to jump straight to the document picker, so the model, the prompt and
   the compliance pass were all implicit in a step that spends a model call per document. The
   dialog shows them, then submits to the same route. Loaded on first open, never on page load. */
(function buildSettings() {
  var btn = document.querySelector('[data-buildsettings]');
  var dlg = document.getElementById('buildDlg');
  if (!btn || !dlg || !dlg.showModal) return;
  var loaded = false;

  function esc2(x){ return String(x == null ? '' : x)
    .replace(/[&<>"]/g, function (c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

  function paint(d) {
    var sel = document.getElementById('buildModel');
    var opts = ['<option value="">Default for this pass (' + esc2(d.default_tier) + ' tier)</option>'];
    (d.models || []).forEach(function (m) {
      /*  An unavailable model is SHOWN and disabled, with the reason. Hiding it makes the list
          look like the whole world and turns "why can I not pick sonnet" into a support question. */
      opts.push('<option value="' + esc2(m.name) + '"' + (m.available ? '' : ' disabled') + '>'
        + esc2(m.name) + ': ' + esc2(m.model) + ' (' + esc2(m.tier) + ' tier)'
        + (m.available ? '' : ', unavailable: ' + esc2(m.why)) + '</option>');
    });
    sel.innerHTML = opts.join('');
    document.getElementById('buildModelNote').textContent = d.default_note || '';
    document.getElementById('buildPromptWhere').textContent =
      (d.prompt && (d.prompt.name + ' · ' + d.prompt.where + ' · max ' + d.prompt.max_tokens + ' tokens')) || '';
    document.getElementById('buildPrompt').textContent = (d.prompt && d.prompt.text) || '';
    document.getElementById('buildKnobs').innerHTML = (d.settings || []).map(function (s) {
      if (s.key === 'skip_compliance')
        return '<label class="small"><input type="checkbox" name="skip_compliance" value="1"> <b>'
          + esc2(s.label) + '</b><br><span class="muted">' + esc2(s.help) + '</span></label>';
      return '<label class="small"><b>' + esc2(s.label) + '</b><br>'
        + '<input class="input" style="max-width:8rem" type="number" min="1" value="1" name="'
        + esc2(s.key) + '"><br><span class="muted">' + esc2(s.help) + '</span></label>';
    }).join('');
  }

  btn.addEventListener('click', function () {
    dlg.showModal();
    if (loaded) return;
    loaded = true;
    fetch((window.APP_BASE || '') + '/api/build-settings', {credentials: 'same-origin'})
      .then(function (r) { return r.json(); })
      .then(paint)
      .catch(function () {
        /*  The dialog must not become a dead end because one fetch failed: the submit button
            posts to the builder either way, which is exactly what the old link did. */
        document.getElementById('buildModelNote').textContent =
          'The model list could not be read, so this rebuild will use the default.';
      });
  });
  var close = document.getElementById('buildClose');
  if (close) close.addEventListener('click', function () { dlg.close(); });
})();

/* ── the public link: publish, password, revoke ───────────────────────────────────────────────
   The owner's control. Everything it does is one POST; the dialog exists so a password and a
   revoke are not two more buttons on an already busy toolbar. */
(function publishLink() {
  var btn = document.getElementById('publishBtn');
  var dlg = document.getElementById('publishDlg');
  if (!btn || !dlg || !dlg.showModal) return;
  var slug = btn.dataset.slug;
  var url = (window.APP_BASE || '') + '/public-report/' + slug;
  var out = document.getElementById('publishUrl');
  var pw = document.getElementById('publishPw');
  var pwState = document.getElementById('publishPwState');
  var state = document.getElementById('publishState');
  var save = document.getElementById('publishSave');
  var clearPw = document.getElementById('publishClearPw');
  var tog = document.getElementById('publishToggle');
  var togLab = document.getElementById('publishToggleLab');

  function paint() {
    var live = btn.dataset.published === 'true';
    var hasPw = btn.dataset.haspassword === 'true';
    /*  The address is shown either way. See the dialog's comment: it is derived from the slug,
        not minted, so hiding it until publication only stopped the owner reading what they were
        about to hand out. The toggle is the access control; the field is just the address. */
    out.value = location.origin + url;
    out.classList.toggle('dim', !live);
    tog.checked = live;
    togLab.textContent = live ? 'Published' : 'Not published';
    save.hidden = !live;
    save.textContent = hasPw ? 'Change password' : 'Set password';
    clearPw.hidden = !(live && hasPw);
    pwState.textContent = !live ? ''
      : (hasPw ? 'A password is set. Type a new one to change it, or remove it below.'
               : 'No password — anyone with the link can open it.');
    state.textContent = live
      ? 'Live. Anyone with this link can read the report, with no account, and nothing on the public page can change or re-run the search.'
      : 'The address above does not resolve. Turn this on and anyone holding it can read the report without an account.';
    btn.textContent = live ? 'Public link ✓' : 'Export';
  }

  function post(body) {
    return fetch((window.APP_BASE || '') + '/report/' + slug + '/publish', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': window.CSRF_TOKEN || ''},
      body: JSON.stringify(Object.assign({csrf_token: window.CSRF_TOKEN || ''}, body))
    }).then(function (r) { return r.json(); });
  }

  btn.addEventListener('click', function () {
    /*  The button now lives inside the More options menu, so the menu has to shut behind it:
        a <details> left open under a modal is a panel the reader has to close twice. */
    var menu = btn.closest('details.moreops');
    if (menu) menu.open = false;
    paint();
    dlg.showModal();
  });

  /*  ONE SWITCH, BOTH DIRECTIONS. Publish and Revoke were two buttons that were never both
      available, which is a toggle wearing a disguise. Revoking still asks, because a link that
      has been sent to somebody stops working; publishing does not, because it is undoable by
      the same switch. On failure the checkbox is put back where it was, so what is drawn is
      never ahead of what the server did. */
  tog.addEventListener('change', function () {
    var want = tog.checked;
    if (!want && !confirm('Un-publish? Anyone holding the link will get a 404. The viewer log is kept.')) {
      tog.checked = true;
      return;
    }
    tog.disabled = true;
    post(want ? {password: pw.value || ''} : {revoke: true}).then(function (d) {
      tog.disabled = false;
      if (!d || !d.ok) {
        tog.checked = !want;
        state.textContent = (d && d.error) || 'That did not work. Try again.';
        return;
      }
      btn.dataset.published = want ? 'true' : 'false';
      if (want) { btn.dataset.haspassword = d.has_password ? 'true' : 'false'; pw.value = ''; }
      paint();
    }).catch(function () { tog.disabled = false; tog.checked = !want; });
  });

  save.addEventListener('click', function () {
    save.disabled = true;
    post({password: pw.value || ''}).then(function (d) {
      save.disabled = false;
      if (!d || !d.ok) { pwState.textContent = 'Could not save the password. Try again.'; return; }
      btn.dataset.published = 'true';
      btn.dataset.haspassword = d.has_password ? 'true' : 'false';
      pw.value = '';
      paint();
    }).catch(function () { save.disabled = false; });
  });
  clearPw.addEventListener('click', function () {
    post({clear_password: true}).then(function (d) {
      if (d && d.ok) { btn.dataset.haspassword = 'false'; paint(); }
    });
  });
  document.getElementById('publishCopy').addEventListener('click', function () {
    if (!out.value) return;
    out.select();
    (navigator.clipboard ? navigator.clipboard.writeText(out.value) : Promise.reject())
      .catch(function () { try { document.execCommand('copy'); } catch (e) {} });
    var b = document.getElementById('publishCopy');
    b.textContent = 'Copied'; setTimeout(function () { b.textContent = 'Copy'; }, 1400);
  });
})();

/* ── what only the page can know about its reader ─────────────────────────────────────────────
   The server already has the address, the user agent, the languages and the client hints. It
   cannot have the screen, the timezone, the capabilities, or TIME ON PAGE — which is not a
   property of a request at all. It is a property of a session, so it has to be measured while it
   happens and sent as it ends.

   A heartbeat every 15s while the page is VISIBLE, plus a final sendBeacon on pagehide. The server
   keeps the largest reading it has been told, so a closed laptop or a killed tab leaves the last
   good number rather than resetting it to zero. Visible-only, because a tab left open in the
   background overnight is not four hundred minutes of reading. */
(function publicVisitTelemetry() {
  var meta = document.getElementById('publicVisit');
  if (!meta) return;
  var key = meta.dataset.visitKey, slug = meta.dataset.slug;
  if (!key) return;
  var url = (window.APP_BASE || '') + '/public-report/' + slug + '/beacon';
  var visible = 0, last = Date.now(), maxScroll = 0, sent = 0;

  function tickVisible() {
    var now = Date.now();
    if (document.visibilityState === 'visible') visible += (now - last) / 1000;
    last = now;
  }
  function scrollPct() {
    var h = document.documentElement;
    var total = (h.scrollHeight - h.clientHeight);
    if (total <= 0) return 100;
    return Math.max(0, Math.min(100, Math.round((h.scrollTop || window.pageYOffset) / total * 100)));
  }
  function facts() {
    var n = navigator || {}, s = screen || {};
    var conn = n.connection || {};
    var uad = n.userAgentData || {};
    var nav0 = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || {};
    return {
      visit_key: key,
      seconds_on_page: Math.round(visible),
      max_scroll_pct: maxScroll,
      screen_w: s.width, screen_h: s.height, avail_w: s.availWidth, avail_h: s.availHeight,
      viewport_w: window.innerWidth, viewport_h: window.innerHeight,
      color_depth: s.colorDepth, pixel_ratio: window.devicePixelRatio,
      timezone: (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone,
      timezone_offset: new Date().getTimezoneOffset(),
      languages: n.languages ? Array.prototype.slice.call(n.languages) : [],
      language: n.language, platform: n.platform, vendor: n.vendor, user_agent: n.userAgent,
      hardware_concurrency: n.hardwareConcurrency, device_memory: n.deviceMemory,
      max_touch_points: n.maxTouchPoints,
      connection: conn.effectiveType, downlink: conn.downlink, rtt: conn.rtt,
      save_data: !!conn.saveData,
      cookies_enabled: n.cookieEnabled, do_not_track: n.doNotTrack,
      referrer: document.referrer,
      page_load_ms: nav0.duration ? Math.round(nav0.duration) : null,
      prefers_dark: window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches,
      prefers_reduced_motion: window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches,
      orientation: (s.orientation || {}).type,
      ua_brands: (uad.brands || []).map(function (b) { return b.brand + ' ' + b.version; }),
      ua_platform: uad.platform, ua_mobile: uad.mobile,
      history_length: history.length,
      /* An honest self-report. Automation usually leaves it set, and a dashboard that cannot tell
         a person from a link preview is counting an audience that was never there. */
      webdriver: !!n.webdriver
    };
  }
  function send(useBeacon) {
    tickVisible();
    maxScroll = Math.max(maxScroll, scrollPct());
    var body = JSON.stringify(facts());
    sent++;
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon(url, new Blob([body], {type: 'application/json'}));
      return;
    }
    fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: body, keepalive: true}).catch(function () {});
  }

  document.addEventListener('visibilitychange', tickVisible);
  window.addEventListener('scroll', function () {
    maxScroll = Math.max(maxScroll, scrollPct());
  }, {passive: true});
  /* First one immediately, so a visitor who closes the tab in three seconds is still a recorded
     reading with a browser and a screen attached to it. */
  setTimeout(function () { send(false); }, 900);
  setInterval(function () { send(false); }, 15000);
  window.addEventListener('pagehide', function () { send(true); });
})();


/* ---------------------------------------------------------------------------
   THE WHOLE FILE, on any reference card.

   The search corpus knows what a document SAYS. It does not know what has happened to it: whether
   it was granted, opposed, abandoned, whether a renewal fee is due, what the examiner posted, or
   whether a 1.290 window is still open. That lives in the registers, and the lookup engine at
   window.LOOKUP_BASE already owns the adapters for all four of them (USPTO ODP, EPO OPS, Google
   Patents, DPMAregister) plus a pacer for OPS's burst detection and a document store.

   So this does not fetch anything itself. On card open it asks the engine ONE cheap question —
   is this file already held? — and shows the file if so, or a button if not. A deep fetch costs
   register calls and tens of megabytes, so it never starts unasked.

   The engine authenticates on this app's own session cookie (same domain, cookie path "/"), which
   is why these are plain same-origin fetches with no token anywhere.
--------------------------------------------------------------------------- */
(function () {
  var LB = window.LOOKUP_BASE || '/patentlookup';
  var OPEN = {};      // pub -> true while a panel is open, so a re-render does not stack streams

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; });
  }
  function pretty(d) {
    var m = /^(\d{4})(\d{2})(\d{2})$/.exec(String(d || ''));
    return m ? m[1] + '-' + m[2] + '-' + m[3] : esc(d || '');
  }
  function api(path, opts) {
    opts = opts || {};
    opts.credentials = 'same-origin';
    opts.headers = Object.assign({'Accept': 'application/json'}, opts.headers || {});
    return fetch(LB + path, opts).then(function (r) {
      if (r.status === 401) throw new Error('Session expired — reload and sign in again.');
      if (!r.ok) throw new Error('The lookup service answered ' + r.status + '.');
      return r.json();
    });
  }

  function panelFor(btn) {
    var pub = btn.dataset.pub;
    var id = 'filepanel-' + pub.replace(/[^A-Za-z0-9]/g, '');
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement('div');
      el.id = id;
      el.className = 'filepanel';
      /*  Appended to the card, not to the meta line: this is a block of tables and it must not
          reflow the title row it was launched from. */
      (btn.closest('.rmain') || btn.parentNode).appendChild(el);
    }
    return el;
  }

  function table(title, cols, rows, raw) {
    if (!rows || !rows.length) return '';
    var h = ['<div class="fpsec"><b>' + esc(title) + '</b> <span class="cc">' + rows.length + '</span>',
             '<div style="overflow-x:auto"><table class="tbl fptbl"><thead><tr>'];
    cols.forEach(function (c) { h.push('<th>' + esc(c) + '</th>'); });
    h.push('</tr></thead><tbody>');
    rows.forEach(function (r) {
      h.push('<tr>');
      r.forEach(function (cell) { h.push('<td>' + (raw ? cell : esc(cell)) + '</td>'); });
      h.push('</tr>');
    });
    h.push('</tbody></table></div></div>');
    return h.join('');
  }

  function render(el, rec, pub) {
    var d = (rec && rec.dossier) || {}, c = (rec && rec.confirm) || {};
    var h = ['<div class="fphead"><b>' + esc(d.title || (rec && rec.title) || pub) + '</b>'];
    if (rec && rec.headline) h.push(' <span class="stag t-info">' + esc(rec.headline) + '</span>');
    h.push(' <a class="pn" href="' + window.APP_BASE + '/patentlookup?number='
           + encodeURIComponent(pub) + '" target="_blank" rel="noopener">open in Lookup ↗</a></div>');
    if (c.note) h.push('<p class="small">' + esc(c.note) + '</p>');
    if (d.summary) h.push('<p class="small">' + esc(d.summary) + '</p>');
    var meta = [];
    if ((d.applicants || []).length) meta.push('Applicant: ' + esc(d.applicants.join('; ')));
    if ((d.inventors || []).length) meta.push('Inventors: ' + esc(d.inventors.slice(0, 4).join('; ')));
    if ((d.classifications || []).length) meta.push('CPC: ' + esc(d.classifications.slice(0, 6).join(' · ')));
    if ((c.sources || []).length) meta.push('Sources: ' + esc(c.sources.join(', ')));
    if (meta.length) h.push('<p class="small muted">' + meta.join(' &nbsp;·&nbsp; ') + '</p>');
    if (c.google && c.google.present === false)
      h.push('<p class="small muted">Not on Google Patents'
             + (c.google.reason ? ' (' + esc(c.google.reason) + ')' : '') + '.</p>');

    h.push(table('Legal events', ['Date', 'Code', 'What happened'],
      (d.events || []).map(function (e) { return [pretty(e.date), e.code || '', e.description || '']; })));
    h.push(table('Deadlines and open windows', ['Date', 'What', 'Basis'],
      (d.deadlines || []).map(function (e) { return [pretty(e.date), e.name || '', e.basis || e.law || '']; })));
    h.push(table('Family', ['Member', 'Country', 'Kind', 'Published'],
      (d.members || []).map(function (m) {
        var r = m.ref || {};
        return [m.id || '', r.country || '', r.kind || '', pretty(m.publication_date || '')];
      })));
    var docs = (rec && rec.docs) || [];
    if (docs.length) {
      h.push(table('Documents', ['Document', 'Kind', 'Source', 'Date'], docs.map(function (x) {
        return ['<a href="' + LB + '/api/doc/' + encodeURIComponent(x.id)
                + '" target="_blank" rel="noopener">' + esc(x.title || x.filename || x.id) + '</a>',
                esc(x.category || ''), esc(x.source || ''), pretty(x.doc_date || '')];
      }), true));
    }
    if (!(d.events || []).length && !docs.length)
      h.push('<p class="small muted">The registers returned no events or documents for this '
             + 'publication.</p>');
    el.innerHTML = h.join('');
  }

  function follow(el, lid, pub) {
    var settled = false;
    function finish() {
      if (settled) return;
      settled = true;
      api('/api/lookup/' + encodeURIComponent(lid))
        .then(function (rec) { render(el, rec, pub); })
        .catch(function (e) { el.innerHTML = '<p class="small warn">' + esc(e.message) + '</p>'; });
    }
    var es;
    try { es = new EventSource(LB + '/api/lookup/' + encodeURIComponent(lid) + '/stream'); }
    catch (e) { return finish(); }
    es.onmessage = function (ev) {
      var j; try { j = JSON.parse(ev.data); } catch (e) { return; }
      var m = j.message || j.msg || j.phase || '';
      if (m && !settled) el.innerHTML = '<p class="small muted"><span class="spin" aria-hidden="true"></span> '
        + esc(m) + '</p>';
      if (j.done) { try { es.close(); } catch (e) {} finish(); }
    };
    es.onerror = function () { try { es.close(); } catch (e) {} finish(); };
  }

  function start(el, pub, refresh) {
    el.innerHTML = '<p class="small muted"><span class="spin" aria-hidden="true"></span> '
      + 'Pulling the file from the registers. This takes a minute or two and is kept, so it is '
      + 'instant next time.</p>';
    api('/api/file', {method: 'POST', headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify({number: pub, refresh: !!refresh})})
      .then(function (d) {
        if (d.phase === 'error') {
          el.innerHTML = '<p class="small warn">'
            + esc((d.confirm && (d.confirm.error || d.confirm.note))
                  || 'The registers could not resolve this number.') + '</p>';
          return;
        }
        if (d.reused && !d.running)
          return api('/api/lookup/' + encodeURIComponent(d.id))
            .then(function (rec) { render(el, rec, pub); });
        follow(el, d.id, pub);
      })
      .catch(function (e) { el.innerHTML = '<p class="small warn">' + esc(e.message) + '</p>'; });
  }

  function open(btn) {
    var pub = btn.dataset.pub, el = panelFor(btn);
    if (OPEN[pub]) { el.hidden = !el.hidden; btn.setAttribute('aria-expanded', String(!el.hidden)); return; }
    OPEN[pub] = true;
    el.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    el.innerHTML = '<p class="small muted">Asking the registers whether this file is already held…</p>';
    /*  The cheap question first. A deep fetch costs register calls and tens of megabytes; it does
        not start because somebody opened a panel. */
    api('/api/file?number=' + encodeURIComponent(pub))
      .then(function (d) {
        if (d.found && !d.running)
          return api('/api/lookup/' + encodeURIComponent(d.id))
            .then(function (rec) { render(el, rec, pub); });
        if (d.found && d.running) return follow(el, d.id, pub);
        el.innerHTML = '<p class="small muted">No file held for this publication yet. '
          + 'Pulling it reads USPTO ODP, the EPO register and DPMAregister, takes a minute or two, '
          + 'and is kept afterwards.</p>'
          + '<button type="button" class="btn sm fpgo">Pull the file</button>';
        el.querySelector('.fpgo').addEventListener('click', function () { start(el, pub, false); });
      })
      .catch(function (e) { el.innerHTML = '<p class="small warn">' + esc(e.message) + '</p>'; });
  }

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest && ev.target.closest('.filelink');
    if (!btn) return;
    ev.preventDefault();
    open(btn);
  });
})();


/*  ------------------------------------------------------------------ the (?) mark, anywhere
    A page that explains itself in a paragraph beside every control is a page nobody finishes
    reading. The explanation is still worth having, so it moves one click away: the control says
    what it is, and the (?) beside it says how it works and when it matters.

    Usage is one element and no wiring:

        <button type="button" class="qmark" data-title="Concept search"
                data-help="Breaks the description into…">?</button>

    Two paragraphs: separate with a blank line. `type="button"` matters inside a form, and the
    markup is a <button> rather than a <span> so it is reachable by keyboard without a tabindex.
*/
(function questionMarks() {
  var dlg = null;

  function ensure() {
    if (dlg) { return dlg; }
    dlg = document.createElement('dialog');
    dlg.className = 'dlg qmarkdlg';
    dlg.innerHTML =
      '<form method="dialog" class="stack" style="min-width:min(34rem,92vw)">' +
      '<h3 class="h3" data-t style="margin-top:0"></h3>' +
      '<div class="small" data-b style="line-height:1.6"></div>' +
      '<div style="display:flex;margin-top:.7rem"><span class="grow"></span>' +
      '<button class="btn ghost sm" value="close">Close</button></div></form>';
    document.body.appendChild(dlg);
    return dlg;
  }

  function open(btn) {
    var d = ensure();
    if (!d.showModal) { return; }                 // no <dialog>: leave the title attribute to it
    d.querySelector('[data-t]').textContent = btn.getAttribute('data-title') || 'About this';
    var body = btn.getAttribute('data-help') || '';
    d.querySelector('[data-b]').innerHTML = body.split(/\n\s*\n/).map(function (p) {
      //  The text is authored in the template, never from a user, but escape it anyway: this is
      //  one line and it means a future data-help that IS user text cannot inject.
      var e = document.createElement('div');
      e.textContent = p.trim();
      return '<p>' + e.innerHTML + '</p>';
    }).join('');
    d.showModal();
  }

  /*  THE OTHER CONVENTION, which had no handler at all.

      The 1.290 picker carries four question marks written as `<span class="qhelp"
      data-help="secret">` with the text in a hidden `<div id="helpSecret" class="qhelp-body">`
      beside them. `.qhelp` is a STYLE rule in style.css and nothing was ever bound to it, so all
      four rendered as a question mark that did nothing when clicked. They are the four
      explanations that decide whether a document goes in an envelope filed at the USPTO.

      They keep their markup, because their text is rich (it carries <b> lead-ins) and stuffing
      rich text into a data-help attribute would flatten it: `open()` escapes, on purpose. So
      this resolves the LINKED HIDDEN BODY instead and hands its markup to the same dialog.  */
  function openLinked(b) {
    var key = b.getAttribute('data-help') || '';
    var el = document.getElementById('help' + key.charAt(0).toUpperCase() + key.slice(1));
    if (!el) { return; }
    var d = ensure();
    if (!d.showModal) { return; }
    d.querySelector('[data-t]').textContent =
      b.getAttribute('data-title') || b.getAttribute('aria-label') || 'About this';
    d.querySelector('[data-b]').innerHTML = el.innerHTML;
    d.showModal();
  }

  document.addEventListener('click', function (ev) {
    if (!ev.target.closest) { return; }
    var b = ev.target.closest('.qmark');
    if (b) { ev.preventDefault(); open(b); return; }
    var q = ev.target.closest('.qhelp[data-help]');
    if (q) { ev.preventDefault(); openLinked(q); }
  });

  //  `.qhelp` is a <span role="button" tabindex="0">, so the browser does not press it on Enter
  //  or Space the way it would a real <button>. Without this the four are keyboard-dead as well.
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') { return; }
    if (!ev.target.closest) { return; }
    var q = ev.target.closest('.qhelp[data-help]');
    if (q) { ev.preventDefault(); openLinked(q); }
  });
})();

/*  THE ETA HAS TO FOLLOW BOTH CONTROLS.
    The depth select carries a data-eta measured for the interactive reader. Batched is about four
    times the wall clock for the same coverage (6.7 pairs/s against 26.8, measured), so a page that
    kept showing the interactive figure beside a batched run would be quoting a time it cannot
    meet. The multiplier is applied here, in one place, and the email checkbox is forced on with
    it: a run nobody is watching has to be able to tell you it finished. */
(function readDepthEta() {
  var sel = document.getElementById('readTop');
  var mode = document.getElementById('readBatched');
  var dest = document.getElementById('readThen');
  var out = document.getElementById('readEta');
  if (!sel || !out) return;
  var mail = document.querySelector('.readdepth input[name="notify_email"]');

  function scale(text, factor) {
    /*  The ETA is prose ("about 6 minutes", "8 to 12 minutes"), so every number in it is scaled
        rather than the string being re-derived: that keeps whatever wording the server chose. */
    return text.replace(/\d+(?:\.\d+)?/g, function (n) {
      var v = parseFloat(n) * factor;
      /*  Whole minutes from three up. "about 7.5 to 17 minutes" is false precision on an
          estimate whose own range is a factor of two. */
      return String(v >= 3 ? Math.round(v) : Math.round(v * 10) / 10);
    });
  }

  /*  Each control multiplies the same base, so the line under them is one number rather than
      three that have to be added up by the reader.

      Batched: 4x, measured (6.7 pairs a second against 26.8 per-document).
      Grid:    the per-requirement sweep goes from 3 documents a limitation to 12, and the sweep
               is calls, so the run is about half again as long at the same read depth.
      Packet:  one further model call per document after the reading, which is a minute or two on
               a ten-document packet and is named rather than folded into the number. */
  /*  The estimate for an arbitrary N, since a slider is not a list of seven rungs any more.
      Same shape the server quotes: a fixed cost for retrieval, screening and the external
      fan-out, plus about 2.5 seconds a document read, and a range because a document's length
      is the variance. Kept here rather than round-tripped so the number moves with the thumb. */
  function etaFor(n) {
    var lo = Math.round((90 + n * 2.0) / 60);
    var hi = Math.round((180 + n * 4.5) / 60);
    if (hi <= lo) { hi = lo + 1; }
    return 'about ' + lo + ' to ' + hi + ' minutes';
  }

  function paint() {
    var done = parseInt(sel.dataset.done || '0', 10) || 0;
    var want = parseInt(sel.value, 10) || 0;
    var add = Math.max(0, want - done);
    var base = etaFor(add);
    var count = document.getElementById('readCount');
    if (count) {
      /*  What the number MEANS, which is not the same as the number. On a fresh search it is
          "read 45"; on one that has already read 100 it is "125 in total, 25 more than the 100
          already read", because the 100 are not read again and are not paid for again. */
      count.textContent = done
        ? want + ' in total · ' + add + ' more than the ' + done + ' already read'
        : 'the strongest ' + want;
    }
    var batched = mode && mode.value === '1';
    var want = dest ? dest.value : 'list';
    var factor = (batched ? 4 : 1) * (want === 'grid' ? 1.5 : 1);
    var text = base && factor !== 1 ? scale(base, factor) : base;
    if (text && want === 'packet') { text += ', then the papers'; }
    if (text && batched) { text += ' · emailed'; }
    out.textContent = text;
    out.classList.toggle('rdeta-batched', !!batched);
    if (batched && mail) { mail.checked = true; }
    var go = document.getElementById('startReading');
    if (go) {
      go.textContent = want === 'list' ? 'Start reading'
                     : want === 'grid' ? 'Read and build the grid'
                     : 'Read and build the submission';
    }
  }
  sel.addEventListener('change', paint);
  sel.addEventListener('input', paint);          /* while dragging, not only on release */
  if (mode) mode.addEventListener('change', paint);
  if (dest) dest.addEventListener('change', paint);
  paint();
})();


/*  clamp3 toggle: click a clamped block to open it, click again to close. Delegated, so blocks
    the extract step writes after this script ran are covered too. A textarea cannot be clamped
    this way (it is a form control, not a box of text), so it gets a row cap instead. */
(function clampToggle() {
  document.addEventListener('click', function (ev) {
    var el = ev.target.closest && ev.target.closest('.clamp3');
    if (!el || el.tagName === 'TEXTAREA') { return; }
    el.classList.toggle('open');
  });
  document.addEventListener('focusin', function (ev) {
    var t = ev.target;
    if (t && t.tagName === 'TEXTAREA' && t.classList.contains('clamp3')) {
      t.classList.add('open');
    }
  });
})();

