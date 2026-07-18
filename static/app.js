// Results page interactions: scannable card list with sketch thumbnails, closed-by-default
// expandable tabs, a full-document slide-over browser (gallery + citation navigation), triage
// flags, sort/filter, export/compare, lightbox and query-term highlighting.
// Reference text (abstract/claims/description/figures) is server-rendered into hidden panes, so
// tab switching is instant; only drawings-not-yet-downloaded, the AI opinion and citations load
// on demand. Everything shares one /api/ref cache with the slide-over.
const B = (typeof window !== 'undefined' && window.APP_BASE) || '';
const $ = s => document.querySelector(s);
function esc(s){ return String(s ?? '').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function normPub(s){ return (s||'').toUpperCase().replace(/[^A-Z0-9]/g,''); }
function gp(pub){ return 'https://patents.google.com/patent/'+(pub||'').replace(/-/g,'')+'/en'; }

// ---- /api/ref cache (shared by card panes + slide-over) ------------------------------------
const REFCACHE = {};
async function fetchRef(pub){
  if(REFCACHE[pub]) return REFCACHE[pub];
  const j = await (await fetch(B+'/api/ref/'+encodeURIComponent(pub)+'?slug='+encodeURIComponent(window.SLUG))).json();
  REFCACHE[pub] = j; return j;
}

// ---- query-term highlighting ---------------------------------------------------------------
let QTERMS = null;
function queryTerms(){
  if(QTERMS) return QTERMS;
  const q = (window.QUERY||'').toLowerCase();
  const stop = new Set(('the a an and or of to for with without in on at by is are be as that this from '+
    'which said comprising comprises having has have each least one first second means device apparatus '+
    'method system according wherein into such other than more also its their between within').split(' '));
  QTERMS = [...new Set((q.match(/[a-z][a-z\-]{3,}/g)||[]).filter(w=>!stop.has(w)))].slice(0,40);
  return QTERMS;
}
function hlNode(node){
  const terms = queryTerms(); if(!terms.length) return;
  const re = new RegExp('\\b('+terms.map(t=>t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|')+')\\b','gi');
  const walk=document.createTreeWalker(node,NodeFilter.SHOW_TEXT,null);
  const texts=[]; let n; while(n=walk.nextNode()){ if(n.nodeValue.trim() && n.parentNode.nodeName!=='MARK') texts.push(n); }
  texts.forEach(t=>{ if(re.test(t.nodeValue)){ const span=document.createElement('span'); span.innerHTML=esc(t.nodeValue).replace(re,m=>'<mark>'+m+'</mark>'); t.parentNode.replaceChild(span,t);} });
}

// ---- inline expandable tabs ----------------------------------------------------------------
function pane(card){ return card.querySelector('.rpane'); }
function showOnly(card, t){
  const p = pane(card); p.classList.remove('all');
  p.querySelectorAll('.psec').forEach(s=>{ s.hidden = s.dataset.t!==t; });
}
async function rtab(btn){
  const card = btn.closest('.refcard'), t = btn.dataset.t, p = pane(card);
  const wasActive = btn.classList.contains('on');
  card.querySelectorAll('.rtab').forEach(b=>b.classList.remove('on'));
  if(wasActive && p.classList.contains('open') && !p.classList.contains('all')){   // toggle closed
    p.classList.remove('open'); return;
  }
  btn.classList.add('on');
  p.classList.add('open');
  showOnly(card, t);
  if(t==='figs') await ensureImages(card);
  if(t==='why') await ensureWhy(card);
  if(t==='cites') await ensureCites(card);
}
async function expandAll(btn){
  const card = btn.closest('.refcard'), p = pane(card);
  const open = p.classList.contains('open') && p.classList.contains('all');
  card.querySelectorAll('.rtab').forEach(b=>b.classList.remove('on'));
  if(open){ p.classList.remove('open','all'); return; }
  p.classList.add('open','all');
  p.querySelectorAll('.psec').forEach(s=>s.hidden=false);
  await Promise.all([ensureImages(card), ensureWhy(card), ensureCites(card)]);
}

async function ensureImages(card){
  const pane = card.querySelector('.psec[data-t="figs"]');
  if(!pane || card.dataset.imgLoaded==='1' || +card.dataset.nimg>0) return;
  card.dataset.imgLoaded='1';
  const ph = pane.querySelector('.figph');
  try{
    const j = await fetchRef(card.dataset.pub);
    const d = j.display||{};
    if(d.images && d.images.length){
      card.dataset.nimg = d.images.length;
      let h='';
      if(d.figs_from_pdf) h+='<div class="muted small" style="margin-bottom:6px">🗎 Extracted from the PDF facsimile.</div>';
      h+='<div class="g">'+d.images.map((im,i)=>{const u=B+'/figures/'+encodeURIComponent(d.pub)+'/'+im.file;
        return '<figure><img loading="lazy" src="'+u+'" data-pub="'+esc(d.pub)+'" onclick="openLb(this)"><figcaption>'+(d.figs_from_pdf?'p.':'fig ')+(i+1)+'</figcaption></figure>';}).join('')+'</div>';
      if(ph) ph.outerHTML=h; else pane.querySelector('h4').insertAdjacentHTML('afterend',h);
    } else if(ph){
      ph.outerHTML='<div class="nodig">🗎 No drawings digitized for this document. '
        +'View on <a href="'+esc(d.google_patents||gp(card.dataset.pub))+'" target="_blank">Google Patents</a> · '
        +'<a href="'+esc(d.espacenet||'#')+'" target="_blank">Espacenet</a></div>';
    }
  }catch(e){ if(ph) ph.textContent='Drawings unavailable.'; }
}
async function ensureWhy(card){
  const slot = card.querySelector('.psec[data-t="why"] .whyslot');
  if(!slot || card.dataset.whyLoaded==='1') return;
  card.dataset.whyLoaded='1';
  try{ const j = await fetchRef(card.dataset.pub); slot.innerHTML = renderWhy(j.rationale); }
  catch(e){ slot.innerHTML='<span class="muted">Opinion unavailable.</span>'; }
}
async function ensureCites(card){
  const slot = card.querySelector('.psec[data-t="cites"] .citeslot');
  if(!slot || card.dataset.citesLoaded==='1') return;
  card.dataset.citesLoaded='1';
  await loadGraph(card.dataset.pub, slot);
}
function renderWhy(r){
  if(!r || !r.why) return '<span class="muted">No AI opinion generated for this reference.</span>';
  let h = '<div class="why">'+esc(r.why)+'</div>';
  if(r.reads_on && r.reads_on.length)
    h += '<div class="readson"><span class="muted small">reads on</span> '+r.reads_on.map(x=>'<span class="chip el">'+esc(x)+'</span>').join(' ')+'</div>';
  return h;
}

// ---- lazy sketch thumbnails ----------------------------------------------------------------
async function ensureThumb(card){
  const thumb = card.querySelector('.rthumb'); if(!thumb || thumb.dataset.done==='1') return;
  const ph = thumb.querySelector('[data-lazythumb]'); if(!ph) return;
  thumb.dataset.done='1';
  try{
    const j = await fetchRef(card.dataset.pub);
    const d = j.display||{};
    if(d.images && d.images.length){
      card.dataset.nimg = d.images.length;
      const u = B+'/figures/'+encodeURIComponent(d.pub)+'/'+d.images[0].file;
      thumb.classList.remove('pending');
      thumb.querySelector('.zoomhint').insertAdjacentHTML('beforebegin',
        '<img loading="lazy" src="'+u+'" alt="drawing"><span class="figbadge">'+d.images.length+' fig'+(d.images.length!==1?'s':'')+'</span>');
      ph.remove();
    }else{
      thumb.classList.add('empty'); thumb.classList.remove('pending');
      ph.outerHTML='<div class="ph">🗎<br>no drawing</div>';
    }
  }catch(e){ thumb.classList.add('empty'); ph.outerHTML='<div class="ph">—</div>'; }
}

// ---- citation graph (card cites pane + slide-over) -----------------------------------------
async function loadGraph(pub, mount){
  mount.innerHTML = '<span class="ploading"><span class="spin sm"></span> loading…</span>';
  try{
    // graph (cited/citing/similar) is cache-backed and fast → render immediately.
    const g = await fetch(B+'/api/graph/'+encodeURIComponent(pub)).then(r=>r.json());
    mount.innerHTML = graphHTML(g, pub)
      + '<div class="morelike"><h5>⁘ More like this (in-corpus, semantic)</h5>'
      + '<div class="cglist mlslot"><span class="ploading"><span class="spin sm"></span> finding similar…</span></div></div>';
    // more-like-this is a live embedding search → load async so it never blocks the citations.
    fetch(B+'/api/morelike/'+encodeURIComponent(pub)).then(r=>r.json()).then(ml=>{
      const slot = mount.querySelector('.mlslot'); if(!slot) return;
      const items = (ml.results||[]).filter(r=>normPub(r.pub)!==normPub(pub)).slice(0,10);
      slot.innerHTML = items.length ? items.map(r=>'<div class="cgitem"><span class="pnlink" onclick="openDetail(\''+esc(r.pub)+'\')">'+esc(r.pub)+'</span>'
        + '<span class="muted small">'+esc((r.title||'').slice(0,54))+'</span>'
        + (r.score!=null?'<span class="chip" style="margin-left:auto">'+Math.round(r.score*100)+'</span>':'')+'</div>').join('')
        : '<span class="muted small">none</span>';
    }).catch(()=>{ const s=mount.querySelector('.mlslot'); if(s) s.innerHTML='<span class="muted small">unavailable</span>'; });
  }catch(e){ mount.innerHTML = '<span class="muted small">citations unavailable</span>'; }
}
function graphHTML(g, pub){
  const col = (title, items)=>{
    let h = '<div class="cgcol"><h5>'+title+' ('+items.length+')</h5><div class="cglist">';
    if(!items.length) h += '<span class="muted small">none</span>';
    items.forEach(it=>{
      const inc = it.in_corpus;
      h += '<div class="cgitem">'
        + (inc ? '<span class="pnlink" onclick="openDetail(\''+esc(it.pub)+'\')">'+esc(it.pub)+'</span>'
               : '<a href="'+gp(it.pub)+'" target="_blank">'+esc(it.pub)+'</a>')
        + (it.examiner?'<span class="exam" title="examiner-cited">X</span>':'')
        + (inc?'<span class="incorp" onclick="openDetail(\''+esc(it.pub)+'\')">in-corpus</span>':'')+'</div>';
    });
    return h+'</div></div>';
  };
  return '<div class="cgcols">'
    + col('◄ Backward (cites)', g.backward||[])
    + col('► Forward (cited by)', g.forward||[])
    + col('≈ Similar', g.similar||[])
    + '</div>';
}

// ---- jump / element filter -----------------------------------------------------------------
function jumpRef(pub){
  const el = document.getElementById('ref-'+pub);
  if(!el){ alert(pub+' is not in the top ranked list.'); return; }
  el.scrollIntoView({behavior:'smooth',block:'center'});
  el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),1400);
}
function filterByElement(el){
  const sel = document.getElementById('felement'); if(!sel) return;
  const idx = (window.ELEMENTS||[]).findIndex(e=>e===el);
  if(idx>=0){ sel.value = String(idx); applyControls(); document.querySelector('.controls').scrollIntoView({behavior:'smooth',block:'center'}); }
}

// ---- lightbox ------------------------------------------------------------------------------
let LB={imgs:[],i:0};
function openLb(img){
  const p = img.closest('.psec') || img.closest('.gallery') || img.closest('.refcard');
  LB.imgs=[...p.querySelectorAll('.g img, #gMain')]; LB.i=Math.max(0,LB.imgs.indexOf(img));
  if(img.id==='gMain'){ LB.imgs=[img]; LB.i=0; }
  showLb(); document.getElementById('lb').classList.add('open');
}
function showLb(){
  const im=LB.imgs[LB.i]; if(!im) return;
  document.getElementById('lbimg').src=im.src;
  document.getElementById('lbcap').textContent='Figure '+(LB.i+1)+' / '+LB.imgs.length+(im.dataset.pub?' · '+im.dataset.pub:'');
}
function lbNav(d){ if(!LB.imgs.length)return; LB.i=(LB.i+d+LB.imgs.length)%LB.imgs.length; showLb(); }
function closeLb(){ document.getElementById('lb').classList.remove('open'); }
document.addEventListener('keydown',e=>{
  const lb=document.getElementById('lb'); if(!lb||!lb.classList.contains('open'))return;
  if(e.key==='Escape')closeLb(); if(e.key==='ArrowLeft')lbNav(-1); if(e.key==='ArrowRight')lbNav(1);
});

// ---- sort + filter -------------------------------------------------------------------------
function applyControls(){
  const cont=document.getElementById('cards'); if(!cont) return;
  const cards=[...cont.querySelectorAll('.refcard')];
  const sortby=document.getElementById('sortby').value;
  const fprior=document.getElementById('fprior') && document.getElementById('fprior').checked;
  const fdraw=document.getElementById('fdraw') && document.getElementById('fdraw').checked;
  const fj=document.getElementById('fjuris').value;
  const felIdx=document.getElementById('felement').value;
  const fflag=document.getElementById('fflag') ? document.getElementById('fflag').value : '';
  const elName = felIdx!=='' ? (window.ELEMENTS||[])[+felIdx] : null;
  let shown=0;
  cards.forEach(c=>{
    let ok=true;
    if(fprior && !(c.dataset.basis==='public_prior_art'||c.dataset.basis==='secret_prior_art')) ok=false;
    if(fdraw && !(+c.dataset.nimg>0)) ok=false;
    if(fj && c.dataset.juris!==fj) ok=false;
    if(elName){ const cov=(c.dataset.covers||'').split('||'); if(!cov.includes(elName)) ok=false; }
    if(fflag){ const fl=c.dataset.flag||''; if(fflag==='unflagged'? !!fl : fl!==fflag) ok=false; }
    c.classList.toggle('hide',!ok); if(ok)shown++;
  });
  const key={
    rank:c=>+c.dataset.rank, score:c=>-c.dataset.rel, covers:c=>-c.dataset.ncovers,
    date:c=>-(Date.parse(c.dataset.date)||0), datea:c=>(Date.parse(c.dataset.date)||9e15),
    juris:c=>c.dataset.juris
  }[sortby]||(c=>+c.dataset.rank);
  cards.sort((a,b)=>{const ka=key(a),kb=key(b);return ka<kb?-1:ka>kb?1:0;});
  cards.forEach(c=>cont.appendChild(c));
  const sh=document.getElementById('shown'); if(sh) sh.textContent = shown+' / '+cards.length+' shown';
}

// ---- selection + export bar ----------------------------------------------------------------
function selectedPubs(){ return [...document.querySelectorAll('.selbox:checked')].map(b=>b.value); }
function updateBar(){
  const sel = selectedPubs(); const bar = document.getElementById('exportbar');
  document.getElementById('selcount').textContent = sel.length+' selected';
  bar.classList.toggle('show', sel.length>0);
}
function clearSel(){ document.querySelectorAll('.selbox:checked').forEach(b=>b.checked=false); updateBar(); }
function doExport(fmt){
  const sel = selectedPubs();
  if(!sel.length){ alert('Select at least one reference (checkbox on the left of a card).'); return; }
  document.getElementById('exportpubs').value = sel.join(',');
  document.getElementById('exportfmt').value = fmt;
  document.getElementById('exportform').submit();
}
function openCompare(){
  const sel = selectedPubs();
  if(sel.length<2 || sel.length>3){ alert('Select 2 or 3 references to compare.'); return; }
  window.open(B+'/compare?slug='+encodeURIComponent(window.SLUG)+'&pubs='+encodeURIComponent(sel.join(',')),'_blank');
}

// ---- triage flags (persist via /api/flags) -------------------------------------------------
async function loadFlags(){
  try{
    const flags = await (await fetch(B+'/api/flags/'+encodeURIComponent(window.SLUG))).json();
    Object.entries(flags||{}).forEach(([pub,e])=>{
      const card=document.getElementById('ref-'+pub); if(!card||!e.flag)return;
      card.dataset.flag=e.flag;
      const p=card.querySelector('.fp-'+e.flag); if(p)p.classList.add('on');
    });
  }catch(e){}
}
async function setFlag(btn){
  const card=btn.closest('.refcard'), pub=card.dataset.pub, want=btn.dataset.flag;
  const cur=card.dataset.flag||'';
  const next = cur===want ? '' : want;
  card.querySelectorAll('.fp').forEach(b=>b.classList.remove('on'));
  if(next){ card.dataset.flag=next; btn.classList.add('on'); } else { delete card.dataset.flag; card.removeAttribute('data-flag'); }
  try{ await fetch(B+'/api/flags/'+encodeURIComponent(window.SLUG),{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({pub,flag:next})}); }catch(e){}
}

// ============================================================================================
//  SLIDE-OVER: full-document browser (gallery + bibliographic + sections + citation navigation)
// ============================================================================================
const detailStack = [];
let GAL = {imgs:[], i:0};
const overlay = () => document.getElementById('soOverlay');

function galSet(i){
  if(!GAL.imgs.length) return;
  GAL.i = (i + GAL.imgs.length) % GAL.imgs.length;
  const main = document.getElementById('gMain'); if(main) main.src = GAL.imgs[GAL.i];
  const c = document.getElementById('gCount'); if(c) c.textContent = (GAL.i+1)+' / '+GAL.imgs.length;
  document.querySelectorAll('.gthumbs img').forEach((t,idx)=>t.classList.toggle('sel', idx===GAL.i));
}
function galPrev(){ galSet(GAL.i-1); } function galNext(){ galSet(GAL.i+1); }

function setHash(pn){ try{ history.replaceState(null,'',B+'/report/'+window.SLUG+'#patent='+encodeURIComponent(pn)); }catch(e){} }
function clearHash(){ try{ history.replaceState(null,'',B+'/report/'+window.SLUG); }catch(e){} }

async function openDetail(pn, push=true){
  if(!pn) return;
  if(push && detailStack[detailStack.length-1] !== pn) detailStack.push(pn);
  document.getElementById('soBack').style.display = detailStack.length>1 ? '' : 'none';
  document.getElementById('soBreadcrumb').textContent = detailStack.join('  ›  ');
  setHash(pn);
  overlay().classList.add('open'); document.body.style.overflow='hidden';
  const body = document.getElementById('soBody');
  body.innerHTML = '<div class="so-loading"><span class="spin"></span><div>Loading '+esc(pn)+' — drawings, claims, citations…</div></div>';
  try{
    const j = await fetchRef(pn);
    renderDetail(pn, j);
  }catch(e){ body.innerHTML = '<div class="so-loading">Couldn\'t load '+esc(pn)+'.</div>'; }
}
function closeDetail(){ overlay().classList.remove('open'); detailStack.length=0; document.body.style.overflow=''; clearHash(); }

function renderDetail(pn, j){
  const d = j.display||{}, sec = j.sections||{}, matched = j.matched, rat = j.rationale;
  const flag = (d.country && FLAGS[d.country]) || '';
  let h = '';
  h += '<div class="so-title">'+(flag?flag+' ':'')+esc(d.title||pn)+'</div>';
  h += '<div class="so-sub"><a class="pn" href="'+esc(d.google_patents||gp(pn))+'" target="_blank">'+esc(pn)+' ↗</a>'
     + (d.assignees&&d.assignees.length?' · '+esc(d.assignees.join('; ')):'')
     + (d.publication_date?' · pub '+esc(d.publication_date):'')
     + (d.inventors&&d.inventors.length?' · 👤 '+esc(d.inventors.slice(0,4).join(', ')):'')+'</div>';
  // action chips
  h += '<div class="so-chips">';
  if(d.pdf_url) h += '<a class="chip ch" href="'+B+'/pdf/'+encodeURIComponent(pn)+'" target="_blank">⭳ PDF</a>';
  h += '<a class="chip" href="'+esc(d.espacenet||'#')+'" target="_blank">Espacenet ↗</a>';
  h += '<a class="chip" href="'+esc(d.google_patents||gp(pn))+'" target="_blank">Google ↗</a>';
  h += '<button class="chip el" onclick="openSimilar(\''+esc(pn)+'\')">⁘ More like this</button>';
  h += '</div>';

  // why relevant (grounded opinion) + matched passage
  if(rat && rat.why){
    h += '<h2>Why relevant</h2>'+renderWhy(rat);
  }
  if(matched && matched.coord){
    h += '<div class="para matched" style="margin-top:10px"><span class="coord">'+esc((matched.kind||'')+' '+matched.coord)+'</span> best semantic match ('+(matched.score||0).toFixed(3)+' cosine)</div>';
  }

  // drawings gallery
  const imgs = (d.images||[]).map(im=>B+'/figures/'+encodeURIComponent(d.pub)+'/'+im.file);
  if(imgs.length){
    GAL = {imgs, i:0};
    h += '<h2>Drawings <span class="muted" style="font-weight:400">('+imgs.length+(d.figs_from_pdf?' · from PDF':'')+')</span></h2>';
    h += '<div class="gallery"><div class="gmain">'
       + (imgs.length>1?'<button class="gnav l" onclick="galPrev()">‹</button>':'')
       + '<img id="gMain" src="'+imgs[0]+'" data-pub="'+esc(pn)+'" onclick="openLb(this)" alt="figure">'
       + (imgs.length>1?'<button class="gnav r" onclick="galNext()">›</button>':'')
       + '<span class="gcount" id="gCount">1 / '+imgs.length+'</span></div>';
    if(imgs.length>1)
      h += '<div class="gthumbs">'+imgs.map((u,idx)=>'<img src="'+u+'" class="'+(idx===0?'sel':'')+'" onclick="galSet('+idx+')" loading="lazy">').join('')+'</div>';
    h += '</div>';
  }

  // bibliographic
  h += '<h2>Bibliographic</h2><div class="biblio">';
  const bib = [['Inventors',(d.inventors||[]).join(', ')],['Assignee',(d.assignees||[]).join('; ')],
    ['Priority',d.priority_date],['Filing',d.filing_date],['Published',d.publication_date],
    ['Country',d.country],['Family',d.family_id]];
  bib.forEach(([k,val])=>{ if(val) h+='<div class="k">'+k+'</div><div>'+esc(val)+'</div>'; });
  if((d.classifications||[]).length){ h+='<div class="k">CPC</div><div class="rchips" style="margin:0">'
    + d.classifications.slice(0,14).map(c=>'<span class="chip cpc" title="'+esc(c.description||'')+'">'+esc(c.code)+'</span>').join('')+'</div>'; }
  h += '</div>';

  // sections (claims / description) — collapsible, claims from Postgres for precision
  const claims = (sec.claims&&sec.claims.length)?sec.claims:null;
  const paras = (sec.paragraphs&&sec.paragraphs.length)?sec.paragraphs:null;
  h += '<h2>Full text</h2>';
  if(d.abstract) h += '<details class="sec2" open><summary>Abstract</summary><div class="secbody">'+esc(d.abstract)+'</div></details>';
  if(claims){
    h += '<details class="sec2"><summary>Claims ('+claims.length+')</summary><div class="secbody">'
      + claims.map(cl=>'<div class="claim'+(cl.independent?' indep':'')+(matched&&matched.coord_raw&&matched.coord_raw.claim_no===cl.claim_no?' matched':'')+'">'
        + '<span class="cn">'+cl.claim_no+'.</span>'+(cl.independent?'<span class="ind">INDEP</span>':'')
        + '<div class="ctext">'+esc(cl.resolved_text||cl.text)+'</div></div>').join('')+'</div></details>';
  }
  if(paras){
    h += '<details class="sec2"><summary>Description ('+paras.length+' paragraphs)</summary><div class="secbody">'
      + paras.map(p=>'<div class="para'+(matched&&matched.coord_raw&&matched.coord_raw.para_no===p.para_no?' matched':'')+'">'
        + '<span class="coord">'+esc(p.para_no||'')+'</span>'+(p.heading?'<span class="hd">'+esc(p.heading)+'</span> ':'')+esc(p.text)+'</div>').join('')+'</div></details>';
  }
  if(!d.abstract && !claims && !paras) h += '<div class="nodig">Full text not in the local corpus for this publication — open the PDF or Google Patents above.</div>';

  // PDF
  if(d.pdf_url){
    h += '<h2>PDF <a href="'+B+'/pdf/'+encodeURIComponent(pn)+'" target="_blank" style="font-weight:400;font-size:12px">open ↗</a></h2>';
    h += '<div class="pdfwrap"><iframe src="'+B+'/pdf/'+encodeURIComponent(pn)+'" title="patent pdf" loading="lazy"></iframe></div>';
  }

  // citations (lazy)
  h += '<h2>Citations &amp; similar</h2><div id="soCites"><span class="ploading"><span class="spin sm"></span> loading citations…</span></div>';

  const body = document.getElementById('soBody');
  body.innerHTML = h; body.scrollTop = 0;
  // highlight query terms in the shown text
  body.querySelectorAll('.secbody, .why, .abstract').forEach(hlNode);
  loadGraph(pn, document.getElementById('soCites'));
}

async function openSimilar(pn){
  detailStack.push(pn+' · similar');
  document.getElementById('soBack').style.display = detailStack.length>1 ? '' : 'none';
  document.getElementById('soBreadcrumb').textContent = detailStack.join('  ›  ');
  overlay().classList.add('open'); document.body.style.overflow='hidden';
  const body = document.getElementById('soBody');
  body.innerHTML = '<div class="so-loading"><span class="spin"></span><div>Finding patents similar to '+esc(pn)+'…</div></div>';
  try{
    const ml = await (await fetch(B+'/api/morelike/'+encodeURIComponent(pn))).json();
    const list = (ml.results||[]).filter(r=>normPub(r.pub)!==normPub(pn));
    let h = '<div class="so-title">Similar to '+esc(pn)+'</div><div class="so-sub">ranked by embedding cosine · in-corpus</div>';
    if(!list.length){ h += '<div class="nodig" style="margin-top:12px">No similar in-corpus patents found.</div>'; }
    else h += list.map(r=>'<div class="simrow" onclick="openDetail(\''+esc(r.pub)+'\')"><div class="simscore">'+Math.round((r.score||0)*100)+'</div>'
      + '<div style="min-width:0"><b class="pn" style="font-family:ui-monospace,Menlo,monospace">'+esc(r.pub)+'</b>'
      + (r.country?' <span class="chip">'+esc(r.country)+'</span>':'')
      + '<div class="muted small" style="margin-top:2px">'+esc(r.title||'(untitled)')+'</div></div></div>').join('');
    body.innerHTML = h; body.scrollTop = 0;
  }catch(e){ body.innerHTML='<div class="so-loading">Error finding similar patents.</div>'; }
}

const FLAGS = {US:'🇺🇸',EP:'🇪🇺',WO:'🌐',DE:'🇩🇪',GB:'🇬🇧',FR:'🇫🇷',JP:'🇯🇵',CN:'🇨🇳',KR:'🇰🇷'};

// ---- init ----------------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded',()=>{
  if(!document.getElementById('cards')) return;
  // slide-over controls
  document.getElementById('soClose').addEventListener('click', closeDetail);
  document.getElementById('soBack').addEventListener('click', ()=>{
    detailStack.pop(); const prev=detailStack[detailStack.length-1];
    if(prev){ const pn = prev.replace(/ · similar$/,''); openDetail(pn,false); } else closeDetail();
  });
  overlay().addEventListener('click', e=>{ if(e.target===overlay()) closeDetail(); });
  document.addEventListener('keydown', e=>{ if(e.key==='Escape' && overlay().classList.contains('open') && !document.getElementById('lb').classList.contains('open')) closeDetail(); });
  document.getElementById('soLink').addEventListener('click', ()=>{
    const pn = (detailStack[detailStack.length-1]||'').replace(/ · similar$/,''); if(!pn) return;
    const url = location.origin + B + '/report/' + window.SLUG + '#patent=' + encodeURIComponent(pn);
    const btn = document.getElementById('soLink'); const t=btn.textContent;
    const done = ()=>{ btn.textContent='✓ Copied'; setTimeout(()=>btn.textContent=t,1400); };
    if(navigator.clipboard) navigator.clipboard.writeText(url).then(done,done); else { prompt('Copy link:', url); }
  });
  // triage flag pills
  document.querySelectorAll('.fp').forEach(b=>b.addEventListener('click', e=>{ e.stopPropagation(); setFlag(b); }));
  loadFlags();
  // highlight query terms across the card-face snippets
  document.querySelectorAll('.refcard .rsnip').forEach(hlNode);
  applyControls();
  // lazy-load sketches + prefetch missing drawings as cards scroll into view
  if('IntersectionObserver' in window){
    const io=new IntersectionObserver((ents)=>{ ents.forEach(e=>{ if(e.isIntersecting){ ensureThumb(e.target); io.unobserve(e.target);} }); },{rootMargin:'500px'});
    document.querySelectorAll('.refcard').forEach(c=>{ if(+c.dataset.nimg===0) io.observe(c); });
  }
  // deep-link: open a shared patent (#patent=US...)
  const m=(location.hash||'').match(/patent=([^&]+)/);
  if(m){ try{ openDetail(decodeURIComponent(m[1])); }catch(e){} }
});
