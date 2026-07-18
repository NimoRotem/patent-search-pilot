// Results page interactions: inline tabbed cards (all data on the card), lazy per-card
// enrichment, lightbox, filters/sort, query highlighting.
const B = (typeof window!=='undefined' && window.APP_BASE) || '';  // proxy path prefix (rotem.ai/patents-data)

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
function hl(text){
  if(!text) return '';
  let out = esc(text);
  const terms = queryTerms();
  if(terms.length){
    const re = new RegExp('\\b('+terms.map(t=>t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|')+')\\b','gi');
    out = out.replace(re, m=>'<mark>'+m+'</mark>');
  }
  return out;
}

// ---- inline tabbed cards: switch tabs, lazy-load the reference's data once per card ----------
function pane(card, t){ return card.querySelector('.ppane[data-t="'+t+'"]'); }

async function ptab(btn){
  const card = btn.closest('.refcard');
  const t = btn.dataset.t;
  card.querySelectorAll('.ptab').forEach(b=>b.classList.toggle('active', b===btn));
  card.querySelectorAll('.ppane').forEach(p=>p.classList.toggle('active', p.dataset.t===t));
  if(t !== 'abstract') await ensureLoaded(card, t);
  if(t === 'cites'){ const g = card.querySelector('.cgraph'); if(g && g.dataset.loaded!=='1'){ g.dataset.loaded='1'; loadGraph(card.dataset.pub, g); } }
}

async function ensureLoaded(card, focusTab){
  const panel = card.querySelector('.ppanel');
  if(panel.dataset.loaded === '1' || panel.dataset.loaded === 'loading') return;
  panel.dataset.loaded = 'loading';
  const ft = pane(card, focusTab || 'why');
  if(ft && !ft.innerHTML.trim()) ft.innerHTML = '<span class="muted">Loading claims, description, figures & AI opinion…</span>';
  try{
    const j = await (await fetch(B+'/api/ref/'+encodeURIComponent(card.dataset.pub)+'?slug='+encodeURIComponent(window.SLUG))).json();
    fillPanes(card, j);
    panel.dataset.loaded = '1';
    card.querySelectorAll('.ppane').forEach(p=>p.classList.toggle('active', p.dataset.t===focusTab));
  }catch(e){ panel.dataset.loaded='0'; if(ft) ft.innerHTML = '<span class="nodig">Could not load this reference. '+esc(String(e))+'</span>'; }
}

function setCount(card, t, n){ const b = card.querySelector('.ptab[data-t="'+t+'"]'); if(b && n) b.dataset.n = n; }

function fillPanes(card, j){
  const d = j.display||{}, s = j.sections||{}, m = j.matched||{}, mc = (m && m.coord_raw)||{};
  pane(card,'why').innerHTML = renderWhy(j.rationale);
  const claims = s.claims||[];       setCount(card,'claims', claims.length);
  pane(card,'claims').innerHTML = renderClaims(claims, mc);
  const paras = s.paragraphs||[];    setCount(card,'paras', paras.length);
  pane(card,'paras').innerHTML = renderParas(paras, mc);
  const figs = s.figures||[];        setCount(card,'figs', (d.images||[]).length || figs.length);
  pane(card,'figs').innerHTML = renderFigs(d, figs);
  const cites = s.citations||[];     setCount(card,'cites', cites.length);
  pane(card,'cites').innerHTML = renderCites(cites) + '<div class="cgraph" data-pub="'+esc(d.pub)+'"></div>';
  if(d.abstract){ const ap = pane(card,'abstract'); if(ap) ap.innerHTML = hl(d.abstract); }
}

function renderWhy(r){
  if(!r || !r.why) return '<span class="muted">No AI opinion generated for this reference yet.</span>';
  let h = '<div class="why">'+esc(r.why)+'</div>';
  if(r.reads_on && r.reads_on.length)
    h += '<div class="readson"><span class="muted small">reads on</span> '+r.reads_on.map(x=>'<span class="chip el">'+esc(x)+'</span>').join(' ')+'</div>';
  return h;
}
function renderClaims(claims, mc){
  if(!claims.length) return '<span class="muted">Claims not ingested for this document — see the PDF / Google Patents.</span>';
  return claims.map(c=>{
    const matched = (mc.claim_no!=null && String(mc.claim_no)===String(c.claim_no));
    return '<div class="claim'+(c.independent?' indep':'')+(matched?' matched':'')+'">'
      + '<span class="cn">'+(c.claim_no!=null?c.claim_no+'.':'')+'</span>'
      + (c.independent?'<span class="ind">INDEP</span>':'')
      + (matched?' <span class="ind mm">◀ best match</span>':'')
      + '<div class="ctext">'+hl(c.resolved_text||c.text)+'</div></div>';
  }).join('');
}
function renderParas(paras, mc){
  if(!paras.length) return '<span class="muted">Description paragraphs not ingested (non-US / expanded-tier doc). Use the PDF.</span>';
  return paras.map(p=>{
    const matched = (mc.para_no!=null && String(mc.para_no)===String(p.para_no));
    return '<div class="para'+(matched?' matched':'')+'"><span class="coord">'+esc(p.para_no||'')+(p.page_no?(' · p'+p.page_no):'')+'</span>'
      + (p.heading?'<span class="hd">'+esc(p.heading)+'</span> ':'')
      + (matched?'<span class="ind mm">◀ best match</span> ':'') + hl(p.text)+'</div>';
  }).join('');
}
function renderFigs(d, figs){
  let h='';
  if(d.images && d.images.length){
    h += '<div class="g">';
    d.images.forEach((im,i)=>{ const url = B+'/figures/'+encodeURIComponent(d.pub)+'/'+im.file;
      h += '<figure><img loading="lazy" src="'+url+'" data-pub="'+esc(d.pub)+'" data-i="'+i+'" onclick="openLb(this)"><figcaption>fig '+(i+1)+'</figcaption></figure>'; });
    h += '</div>';
  } else {
    h += '<div class="nodig">🗎 Facsimile not digitized for this document (common for pre-2000 EP/DE/WO). '
      + 'View on <a href="'+esc(d.google_patents||'#')+'" target="_blank">Google Patents</a> · '
      + '<a href="'+esc(d.espacenet||'#')+'" target="_blank">Espacenet</a></div>';
  }
  if(figs && figs.length){
    h += '<div class="figcaps">'+figs.map(f=>'<div class="figcap"><span class="fno">FIG '+esc(f.figure_no||'')+'</span> '+hl(f.caption)
      + (f.reference_numbers&&f.reference_numbers.length?' <span class="muted small">refs '+f.reference_numbers.join(', ')+'</span>':'')+'</div>').join('')+'</div>';
  }
  return h || '<span class="muted">No figures.</span>';
}
function renderCites(cites){
  if(!cites.length) return '<span class="muted">No citation edges stored.</span>';
  return '<div class="cites">'+cites.map(c=>'<div class="ci">'+(c.category?'<span class="cat">'+esc(c.category)+'</span>':'')
    + '<a href="https://patents.google.com/patent/'+esc((c.pub||'').replace(/-/g,''))+'/en" target="_blank">'+esc(c.pub)+'</a>'
    + '<span class="muted small">'+esc(c.origin||'')+'</span></div>').join('')+'</div>';
}

function toggleExpand(btn){
  const panel = btn.parentNode.querySelector('.ppanel');
  const open = panel.classList.toggle('expanded');
  btn.textContent = open ? 'Show less ▲' : 'Show all ▾';
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
  showLb();
  document.getElementById('lb').classList.add('open');
}
function showLb(){
  const im=LB.imgs[LB.i]; if(!im) return;
  document.getElementById('lbimg').src=im.src;
  document.getElementById('lbcap').textContent='Figure '+(LB.i+1)+' / '+LB.imgs.length+' · '+im.dataset.pub;
}
function lbNav(d){ LB.i=(LB.i+d+LB.imgs.length)%LB.imgs.length; showLb(); }
function closeLb(){ document.getElementById('lb').classList.remove('open'); }
document.addEventListener('keydown',e=>{
  if(!document.getElementById('lb')||!document.getElementById('lb').classList.contains('open'))return;
  if(e.key==='Escape')closeLb(); if(e.key==='ArrowLeft')lbNav(-1); if(e.key==='ArrowRight')lbNav(1);
});

// ---- sort + filter -------------------------------------------------------------------------
function applyControls(){
  const cont=document.getElementById('cards'); if(!cont) return;
  const cards=[...cont.querySelectorAll('.refcard')];
  const sortby=document.getElementById('sortby').value;
  const fprior=document.getElementById('fprior').checked;
  const frel=document.getElementById('frelevant') && document.getElementById('frelevant').checked;
  const fj=document.getElementById('fjuris').value;
  const felIdx=document.getElementById('felement').value;
  const fch=document.getElementById('fchannel').value;
  const elName = felIdx!=='' ? (window.ELEMENTS||[])[+felIdx] : null;

  let shown=0;
  cards.forEach(c=>{
    let ok=true;
    if(fprior && !(c.dataset.basis==='public_prior_art'||c.dataset.basis==='secret_prior_art')) ok=false;
    if(frel && c.dataset.flag!=='relevant') ok=false;
    if(fj && c.dataset.juris!==fj) ok=false;
    if(elName){ const cov=(c.dataset.covers||'').split('||'); if(!cov.includes(elName)) ok=false; }
    if(fch && !(c.dataset.channels||'').split(',').includes(fch)) ok=false;
    c.classList.toggle('hide',!ok); if(ok)shown++;
  });
  const key={
    rank:c=>+c.dataset.rank, score:c=>-c.dataset.rel, date:c=>-(Date.parse(c.dataset.date)||0),
    datea:c=>(Date.parse(c.dataset.date)||9e15), juris:c=>c.dataset.juris, covers:c=>-c.dataset.ncovers
  }[sortby]||(c=>+c.dataset.rank);
  cards.sort((a,b)=>{const ka=key(a),kb=key(b);return ka<kb?-1:ka>kb?1:0;});
  cards.forEach(c=>cont.appendChild(c));
  document.getElementById('shown').textContent = shown+' / '+cards.length+' shown';
}

// ---- triage: flags + notes -----------------------------------------------------------------
async function setFlag(pub, flag, el){
  const group = el.parentNode;
  const already = el.classList.contains('on');
  group.querySelectorAll('.fp').forEach(x=>x.classList.remove('on'));
  const val = already ? '' : flag;
  if(!already) el.classList.add('on');
  document.getElementById('ref-'+pub).dataset.flag = val;
  try{ await fetch(B+'/api/flags/'+encodeURIComponent(window.SLUG),{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pub, flag:val})}); }catch(e){}
  applyControls();
}
async function saveNote(pub, note){
  try{ await fetch(B+'/api/flags/'+encodeURIComponent(window.SLUG),{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pub, note})}); }catch(e){}
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

// ---- citation graph (lazy, into the Citations pane) ----------------------------------------
async function loadGraph(pub, mount){
  mount.innerHTML = '<h5>Citation graph & more-like-this</h5><span class="muted small">loading…</span>';
  try{
    const [g, ml] = await Promise.all([
      fetch(B+'/api/graph/'+encodeURIComponent(pub)).then(r=>r.json()),
      fetch(B+'/api/morelike/'+encodeURIComponent(pub)).then(r=>r.json())
    ]);
    const col = (title, items, kind)=>{
      let h = '<div class="cgcol"><h5>'+title+' ('+items.length+')</h5><div class="cglist">';
      if(!items.length) h += '<span class="muted small">none</span>';
      items.forEach(it=>{
        const gp = 'https://patents.google.com/patent/'+(it.pub||'').replace(/-/g,'')+'/en';
        h += '<div class="cgitem"><a href="'+gp+'" target="_blank">'+esc(it.pub)+'</a>'
          + (it.examiner?'<span class="exam">X</span>':'')
          + (it.in_corpus?'<span class="incorp" title="in corpus" onclick="jumpRef(\''+esc(it.pub)+'\')" style="cursor:pointer">in-corpus</span>':'')
          + '</div>';
      });
      return h+'</div></div>';
    };
    let h = '<h5>Citation graph & more-like-this</h5><div class="cgcols">';
    h += col('◄ Backward (cites)', g.backward||[], 'b');
    h += col('► Forward (cited by)', g.forward||[], 'f');
    h += col('≈ Similar', g.similar||[], 's');
    h += '</div>';
    const mlitems = (ml.results||[]).filter(r=>r.pub.replace(/-/g,'')!==pub.replace(/-/g,'')).slice(0,8);
    h += '<div class="morelike"><h5>More like this (query-by-example, in-corpus)</h5><div class="cglist">';
    mlitems.forEach(r=>{ h += '<div class="cgitem"><a href="#" onclick="jumpRef(\''+esc(r.pub)+'\');return false">'+esc(r.pub)+'</a>'
      + '<span class="muted small">'+esc((r.title||'').slice(0,44))+'</span><span class="muted small">'+r.score+'</span></div>'; });
    h += '</div></div>';
    mount.innerHTML = h;
  }catch(e){ mount.innerHTML = '<h5>Citation graph & more-like-this</h5><span class="muted small">unavailable</span>'; }
}

document.addEventListener('DOMContentLoaded',()=>{
  if(!document.getElementById('cards')) return;
  // highlight the server-rendered abstract panes (plain text -> query-term highlight)
  document.querySelectorAll('.ppane[data-t="abstract"]').forEach(p=>{ if(!p.querySelector('*')) p.innerHTML = hl(p.textContent); });
  applyControls();
});
