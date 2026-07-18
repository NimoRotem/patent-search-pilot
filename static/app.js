// Results page: reference data (abstract/claims/description/figures) is rendered server-side into
// every card, so tabs just switch panes. Only the AI opinion, citations, and any not-yet-downloaded
// drawings load on demand. Plus lightbox, filters/sort, query-term highlighting, export/compare.
const B = (typeof window!=='undefined' && window.APP_BASE) || '';

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

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

// ---- tabs: switch panes; lazy-load why/cites + drawings ------------------------------------
async function ptab(btn){
  const card = btn.closest('.refcard'), t = btn.dataset.t;
  card.querySelectorAll('.ptab').forEach(b=>b.classList.toggle('active', b===btn));
  card.querySelectorAll('.ppane').forEach(p=>p.classList.toggle('active', p.dataset.t===t));
  if(t==='figs') await ensureImages(card);
  if(t==='why') await ensureRef(card);
  if(t==='cites') await ensureCites(card);
}

async function ensureRef(card){
  const pane = card.querySelector('.ppane[data-t="why"]');
  if(card.dataset.refLoaded==='1' || pane.dataset.loading==='1') return;
  pane.dataset.loading='1';
  try{
    const j = await (await fetch(B+'/api/ref/'+encodeURIComponent(card.dataset.pub)+'?slug='+encodeURIComponent(window.SLUG))).json();
    pane.innerHTML = renderWhy(j.rationale);
    card.dataset.refLoaded='1';
  }catch(e){ pane.innerHTML='<span class="muted">Opinion unavailable.</span>'; }
}

async function ensureImages(card){
  if(card.dataset.imgLoaded==='1' || +card.dataset.nimg>0) return;   // already have images
  card.dataset.imgLoaded='1';
  const pane = card.querySelector('.ppane[data-t="figs"]');
  const ph = pane.querySelector('.figph');
  try{
    const j = await (await fetch(B+'/api/ref/'+encodeURIComponent(card.dataset.pub)+'?slug='+encodeURIComponent(window.SLUG))).json();
    const d = j.display||{};
    if(d.images && d.images.length){
      let h='';
      if(d.figs_from_pdf) h+='<div class="muted small" style="margin-bottom:6px">🗎 Extracted from the PDF facsimile.</div>';
      h+='<div class="g">'+d.images.map((im,i)=>{const u=B+'/figures/'+encodeURIComponent(d.pub)+'/'+im.file;
        return '<figure><img loading="lazy" src="'+u+'" data-pub="'+esc(d.pub)+'" onclick="openLb(this)"><figcaption>'+(d.figs_from_pdf?'p.':'fig ')+(i+1)+'</figcaption></figure>';}).join('')+'</div>';
      if(ph) ph.outerHTML=h; else pane.insertAdjacentHTML('afterbegin',h);
    } else if(ph){
      ph.outerHTML='<div class="nodig">🗎 No drawings digitized for this document. '
        +'View on <a href="'+esc(d.google_patents||'#')+'" target="_blank">Google Patents</a> · '
        +'<a href="'+esc(d.espacenet||'#')+'" target="_blank">Espacenet</a></div>';
    }
  }catch(e){ if(ph) ph.textContent='Drawings unavailable.'; }
}

async function ensureCites(card){
  const pane = card.querySelector('.ppane[data-t="cites"]');
  if(card.dataset.citesLoaded==='1' || pane.dataset.loading==='1') return;
  pane.dataset.loading='1';
  await loadGraph(card.dataset.pub, pane);
  card.dataset.citesLoaded='1';
}

function renderWhy(r){
  if(!r || !r.why) return '<span class="muted">No AI opinion generated for this reference.</span>';
  let h = '<div class="why">'+esc(r.why)+'</div>';
  if(r.reads_on && r.reads_on.length)
    h += '<div class="readson"><span class="muted small">reads on</span> '+r.reads_on.map(x=>'<span class="chip el">'+esc(x)+'</span>').join(' ')+'</div>';
  return h;
}

function toggleExpand(btn){
  const panel = btn.parentNode.querySelector('.ppanel');
  const open = panel.classList.toggle('expanded');
  btn.textContent = open ? 'Show less ▲' : 'Show all ▾';
}
function toggleFamily(btn){
  const list = btn.parentNode.querySelector('.famlist');
  const open = list.hasAttribute('hidden');
  if(open) list.removeAttribute('hidden'); else list.setAttribute('hidden','');
  btn.classList.toggle('open', open);
}

function jumpRef(pub){
  const el = document.getElementById('ref-'+pub);
  if(!el){ alert(pub+' is not in the top ranked list.'); return; }
  el.scrollIntoView({behavior:'smooth',block:'center'});
  el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),1400);
}

// ---- lightbox ------------------------------------------------------------------------------
let LB={imgs:[],i:0};
function openLb(img){
  const p = img.closest('.ppane') || img.closest('.refcard');
  LB.imgs=[...p.querySelectorAll('.g img')]; LB.i=LB.imgs.indexOf(img);
  showLb(); document.getElementById('lb').classList.add('open');
}
function showLb(){
  const im=LB.imgs[LB.i]; if(!im) return;
  document.getElementById('lbimg').src=im.src;
  document.getElementById('lbcap').textContent='Figure '+(LB.i+1)+' / '+LB.imgs.length+' · '+im.dataset.pub;
}
function lbNav(d){ LB.i=(LB.i+d+LB.imgs.length)%LB.imgs.length; showLb(); }
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
  const fj=document.getElementById('fjuris').value;
  const felIdx=document.getElementById('felement').value;
  const elName = felIdx!=='' ? (window.ELEMENTS||[])[+felIdx] : null;

  let shown=0;
  cards.forEach(c=>{
    let ok=true;
    if(fprior && !(c.dataset.basis==='public_prior_art'||c.dataset.basis==='secret_prior_art')) ok=false;
    if(fj && c.dataset.juris!==fj) ok=false;
    if(elName){ const cov=(c.dataset.covers||'').split('||'); if(!cov.includes(elName)) ok=false; }
    c.classList.toggle('hide',!ok); if(ok)shown++;
  });
  const key={
    rank:c=>+c.dataset.rank, score:c=>-c.dataset.rel, date:c=>-(Date.parse(c.dataset.date)||0),
    datea:c=>(Date.parse(c.dataset.date)||9e15), juris:c=>c.dataset.juris, covers:c=>-c.dataset.ncovers
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
  if(!sel.length){ alert('Select at least one reference.'); return; }
  document.getElementById('exportpubs').value = sel.join(',');
  document.getElementById('exportfmt').value = fmt;
  document.getElementById('exportform').submit();
}
function openCompare(){
  const sel = selectedPubs();
  if(sel.length<2 || sel.length>3){ alert('Select 2 or 3 references to compare.'); return; }
  window.open(B+'/compare?slug='+encodeURIComponent(window.SLUG)+'&pubs='+encodeURIComponent(sel.join(',')),'_blank');
}

// ---- citation graph (into the Citations pane) ----------------------------------------------
async function loadGraph(pub, mount){
  mount.innerHTML = '<span class="muted small">loading…</span>';
  try{
    const [g, ml] = await Promise.all([
      fetch(B+'/api/graph/'+encodeURIComponent(pub)).then(r=>r.json()),
      fetch(B+'/api/morelike/'+encodeURIComponent(pub)).then(r=>r.json())
    ]);
    const col = (title, items)=>{
      let h = '<div class="cgcol"><h5>'+title+' ('+items.length+')</h5><div class="cglist">';
      if(!items.length) h += '<span class="muted small">none</span>';
      items.forEach(it=>{ const gp = 'https://patents.google.com/patent/'+(it.pub||'').replace(/-/g,'')+'/en';
        h += '<div class="cgitem"><a href="'+gp+'" target="_blank">'+esc(it.pub)+'</a>'
          + (it.examiner?'<span class="exam">X</span>':'')
          + (it.in_corpus?'<span class="incorp" onclick="jumpRef(\''+esc(it.pub)+'\')" style="cursor:pointer">in-corpus</span>':'')+'</div>'; });
      return h+'</div></div>';
    };
    let h = '<div class="cgcols">';
    h += col('◄ Backward (cites)', g.backward||[]);
    h += col('► Forward (cited by)', g.forward||[]);
    h += col('≈ Similar', g.similar||[]);
    h += '</div>';
    const mlitems = (ml.results||[]).filter(r=>r.pub.replace(/-/g,'')!==pub.replace(/-/g,'')).slice(0,8);
    h += '<div class="morelike"><h5>More like this (in-corpus)</h5><div class="cglist">';
    mlitems.forEach(r=>{ h += '<div class="cgitem"><a href="#" onclick="jumpRef(\''+esc(r.pub)+'\');return false">'+esc(r.pub)+'</a>'
      + '<span class="muted small">'+esc((r.title||'').slice(0,44))+'</span></div>'; });
    h += '</div></div>';
    mount.innerHTML = h;
  }catch(e){ mount.innerHTML = '<span class="muted small">citations unavailable</span>'; }
}

document.addEventListener('DOMContentLoaded',()=>{
  if(!document.getElementById('cards')) return;
  // highlight the query terms across the server-rendered abstract / claims / description
  document.querySelectorAll('.refcard .abstract, .refcard .claim .ctext, .refcard .para, .refcard .figcap')
    .forEach(hlNode);
  applyControls();
  // pre-fetch drawings for cards that have none cached, as they scroll into view
  if('IntersectionObserver' in window){
    const io=new IntersectionObserver((ents)=>{ ents.forEach(e=>{ if(e.isIntersecting){ ensureImages(e.target); io.unobserve(e.target);} }); },{rootMargin:'400px'});
    document.querySelectorAll('.refcard').forEach(c=>{ if(+c.dataset.nimg===0) io.observe(c); });
  }
});
