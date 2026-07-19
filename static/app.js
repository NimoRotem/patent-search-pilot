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
const gp = pub => 'https://patents.google.com/patent/' + (pub || '').replace(/-/g, '') + '/en';
const figUrl = (pub, file) => B + '/figures/' + encodeURIComponent(pub) + '/' + encodeURIComponent(file);
const FLAGS = { US: '🇺🇸', EP: '🇪🇺', WO: '🌐', DE: '🇩🇪', GB: '🇬🇧', FR: '🇫🇷', JP: '🇯🇵', CN: '🇨🇳', KR: '🇰🇷' };

function fmtDur(ms){
  const s = Math.floor(ms / 1000);
  return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
}

/* ── /api/ref cache ────────────────────────────────────────────────────────────────────────
   `light=1` skips the grounded opinion, which is a Vertex call for any reference that has none
   cached yet. Panes that only need text ask for light; the opinion and the full detail view ask
   for the real thing. A light response is upgraded in place once a full one arrives. */
const REFCACHE = {};
async function fetchRef(pub, light){
  const hit = REFCACHE[pub];
  if (hit && (!light ? hit._full : true)) return hit;
  const url = B + '/api/ref/' + encodeURIComponent(pub) +
              '?slug=' + encodeURIComponent(window.SLUG || '') + (light ? '&light=1' : '');
  const r = await fetch(url);
  if (!r.ok) throw new Error('ref ' + r.status);
  const j = await r.json();
  j._full = !light;
  if (!hit || j._full) REFCACHE[pub] = j;
  return REFCACHE[pub];
}

/* ── query-term highlighting ─────────────────────────────────────────────────────────────── */
let QTERMS = null;
function queryTerms(){
  if (QTERMS) return QTERMS;
  const stop = new Set(('the a an and or of to for with without in on at by is are be as that this from ' +
    'which said comprising comprises having has have each least one first second means device apparatus ' +
    'method system according wherein into such other than more also its their between within').split(' '));
  QTERMS = [...new Set(((window.QUERY || '').toLowerCase().match(/[a-z][a-z-]{3,}/g) || [])
    .filter(w => !stop.has(w)))].slice(0, 40);
  return QTERMS;
}
function hlNode(node){
  const terms = queryTerms(); if (!terms.length || !node) return;
  const re = new RegExp('\\b(' + terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')\\b', 'gi');
  const walk = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, null);
  const texts = []; let n;
  while ((n = walk.nextNode())) if (n.nodeValue.trim() && n.parentNode.nodeName !== 'MARK') texts.push(n);
  texts.forEach(t => {
    if (!re.test(t.nodeValue)) return;
    re.lastIndex = 0;
    const span = document.createElement('span');
    span.innerHTML = esc(t.nodeValue).replace(re, m => '<mark>' + m + '</mark>');
    t.parentNode.replaceChild(span, t);
  });
}

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

/* States: loading (manifest in flight) → queued (manifest known, off-screen) → ok | none | error.
   'queued' is a STATIC placeholder, never a spinner: a card the user has not scrolled to has not
   failed at anything, and an animation there would be a lie about work in progress. */
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
  if (THUMB_IO) THUMB_IO.observe(thumb); else loadThumb(thumb);
}

/* Deliberately NOT loading="lazy" on a detached Image: Chrome will not start the fetch for a lazy
   image that is not in the document, so the old preload never completed and the card sat on a
   spinner. Laziness is provided by the observer below instead — explicit, and always terminal. */
function loadThumb(thumb){
  if (thumb.dataset.state !== 'queued') return;
  const pub = thumb.dataset.pub, images = thumb._figs || [];
  if (!images.length){ thumbNone(thumb); return; }
  thumb.dataset.state = 'fetching';
  const img = new Image();
  img.decoding = 'async';
  img.alt = (images[0].from_pdf ? 'Page' : 'Figure') + ' 1 of ' + pub;
  // Commit the <img> only once the bytes actually decode, so a 404 or a corrupt file becomes an
  // explicit error state instead of a broken-image icon or a blank box.
  img.onload = () => {
    if (thumb.dataset.state !== 'fetching' || thumb.dataset.pub !== pub) return;
    thumb.dataset.state = 'ok';
    thumb.textContent = '';
    thumb.appendChild(img);
    thumb.insertAdjacentHTML('beforeend',
      '<span class="figbadge">' + images.length + ' fig' + (images.length !== 1 ? 's' : '') + '</span>' +
      '<span class="zoomhint" aria-hidden="true">🔍</span>');
  };
  img.onerror = () => {
    if (thumb.dataset.state !== 'fetching' || thumb.dataset.pub !== pub) return;
    thumbFail(thumb);
  };
  img.src = figUrl(pub, images[0].file);
}

const THUMB_IO = ('IntersectionObserver' in window)
  ? new IntersectionObserver((ents, io) => {
      ents.forEach(e => { if (e.isIntersecting){ io.unobserve(e.target); loadThumb(e.target); } });
    }, { rootMargin: '600px' })
  : null;

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
   CARD PANES — lazily hydrated, one pane visible at a time
   ══════════════════════════════════════════════════════════════════════════════════════════ */
function paneOf(card){ return card.querySelector('.rpane'); }

async function rtab(btn){
  const card = btn.closest('.refcard'), t = btn.dataset.t, p = paneOf(card);
  const wasOpen = btn.getAttribute('aria-expanded') === 'true';
  card.querySelectorAll('.rtab[data-t]').forEach(b => b.setAttribute('aria-expanded', 'false'));
  if (wasOpen){ p.classList.remove('open'); p.hidden = true; return; }
  btn.setAttribute('aria-expanded', 'true');
  p.classList.add('open'); p.hidden = false;
  p.innerHTML = '<span class="ploading"><span class="spin sm" aria-hidden="true"></span> loading…</span>';
  try{
    if (t === 'why')    p.innerHTML = await paneWhy(card);
    if (t === 'figs')   p.innerHTML = await paneFigs(card);
    if (t === 'text')   p.innerHTML = await paneText(card);
    hlNode(p);
  }catch(e){
    p.innerHTML = '<div class="pempty">Couldn\'t load this section. ' +
      '<a href="' + gp(card.dataset.pub) + '" target="_blank" rel="noopener">Open on Google Patents</a>.</div>';
  }
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
  let h = '<div class="why">' + esc(r.why) + '</div>';
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
      'View it on <a href="' + esc(d.google_patents || gp(pub)) + '" target="_blank" rel="noopener">Google Patents</a>' +
      (d.espacenet ? ' or <a href="' + esc(d.espacenet) + '" target="_blank" rel="noopener">Espacenet</a>' : '') + '.</div>';
  const kind = d.figs_from_pdf ? 'Page ' : 'Figure ';
  return '<h4>Drawings (' + imgs.length + ')</h4>' +
    (d.figs_from_pdf ? '<p class="small muted" style="margin:-4px 0 9px">Extracted from the PDF facsimile.</p>' : '') +
    '<div class="g">' + imgs.map((im, i) =>
      '<figure><img loading="lazy" decoding="async" src="' + figUrl(pub, im.file) + '" ' +
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
let LB = { imgs: [], i: 0 };
function openLb(img){
  const scope = img.closest('.rpane') || img.closest('.gallery') || img.closest('.cmpgrid') || document;
  LB.imgs = [...scope.querySelectorAll('.g img, #gMain, .cmpimg')];
  LB.i = Math.max(0, LB.imgs.indexOf(img));
  if (img.id === 'gMain'){ LB.imgs = [img]; LB.i = 0; }
  showLb();
  const lb = document.getElementById('lb');
  lb.classList.add('open');
  document.getElementById('lbclose').focus();
}
function showLb(){
  const im = LB.imgs[LB.i]; if (!im) return;
  const stage = document.getElementById('lbstage');
  const el = document.createElement('img');
  el.src = im.src;
  el.alt = im.alt || ('Figure ' + (LB.i + 1) + (im.dataset.pub ? ' of ' + im.dataset.pub : ''));
  stage.replaceChildren(el);
  document.getElementById('lbcap').textContent =
    'Figure ' + (LB.i + 1) + ' of ' + LB.imgs.length + (im.dataset.pub ? ' · ' + im.dataset.pub : '');
}
function lbNav(d){ if (!LB.imgs.length) return; LB.i = (LB.i + d + LB.imgs.length) % LB.imgs.length; showLb(); }
function closeLb(){ document.getElementById('lb').classList.remove('open'); }

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
  h += '<div class="so-sub"><a class="pn" href="' + esc(d.google_patents || gp(pn)) + '" target="_blank" rel="noopener">' +
    esc(pn) + ' ↗</a>' +
    (d.assignees && d.assignees.length ? ' · ' + esc(d.assignees.join('; ')) : '') +
    (d.publication_date ? ' · published ' + esc(d.publication_date) : '') +
    (d.inventors && d.inventors.length ? ' · ' + esc(d.inventors.slice(0, 4).join(', ')) : '') + '</div>';

  h += '<div class="so-chips">';
  if (d.pdf_url) h += '<a class="chip ch" href="' + B + '/pdf/' + encodeURIComponent(pn) + '" target="_blank" rel="noopener">PDF ⭳</a>';
  h += '<a class="chip" href="' + esc(d.espacenet || '#') + '" target="_blank" rel="noopener">Espacenet ↗</a>';
  h += '<a class="chip" href="' + esc(d.google_patents || gp(pn)) + '" target="_blank" rel="noopener">Google Patents ↗</a>';
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

  const imgs = (d.images || []).map(im => figUrl(d.pub || pn, im.file));
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
   The agent emits elements / partial / seeded / round / reranking / federating / done. Between
   'partial' and 'reranking' there can be minutes where only SSE keep-alives arrive, and the old
   UI simply froze on one sentence. So the stage list is a state machine that never moves
   backwards, each stage keeps the numbers it learned, and the active stage shows how long it has
   been running — a silent server still reads as visible, honest progress. */
const STAGES = [
  { key: 'decompose', rank: 0, name: 'Reading the disclosure',
    note: 'Decomposing the invention into independent technical elements.' },
  { key: 'search',    rank: 1, name: 'Searching the corpus',
    note: 'Eight retrieval channels across 107,795 publications — dense, sparse, CPC, citation, cross-lingual.' },
  { key: 'expand',    rank: 2, name: 'Expanding the candidate set',
    note: 'Following citations, patent families and EN/DE equivalents outward from the seed hits.' },
  { key: 'rounds',    rank: 3, name: 'Refinement rounds',
    note: 'Re-querying on the elements that are still uncovered, until new families stop appearing.' },
  { key: 'rerank',    rank: 4, name: 'Reranking and grounding',
    note: 'A bge-reranker cross-encoder rescores every candidate, then each claim-chart cell is grounded in real text. This is the slowest step.' },
  { key: 'federate',  rank: 5, name: 'Wider search — external APIs',
    note: 'Querying SerpApi, BigQuery, PQAI and OpenAlex, then fusing by reciprocal rank.' },
  { key: 'done',      rank: 6, name: 'Report ready', note: '' }
];
const KIND_RANK = { elements: 1, seeded: 2, partial: 2, round: 3, reranking: 4, federating: 5, done: 6 };

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
    fetch(B + '/status/' + encodeURIComponent(slug)).then(r => r.json())
      .then(j => { if (!onEvent(j)) setTimeout(poll, 2000); else done(); })
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
  resolveThumbs();

  const m = (location.hash || '').match(/patent=([^&]+)/);
  if (m){ try{ openDetail(decodeURIComponent(m[1])); }catch(e){} }
});
