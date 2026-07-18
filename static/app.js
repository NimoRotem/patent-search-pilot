// Results page interactions: lazy card enrichment, tabs, lightbox, filters/sort, highlighting.
const B = (typeof window!=='undefined' && window.APP_BASE) || '';  // proxy path prefix (M9: rotem.ai/patents-data)

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

// ---- card expand + lazy enrichment ---------------------------------------------------------
async function toggleCard(head){
  const card = head.closest('.refcard');
  card.classList.toggle('open');
  const body = card.querySelector('.refbody');
  if(card.classList.contains('open') && body.dataset.loaded==='0'){
    body.dataset.loaded='1';
    body.innerHTML = '<div class="why load">Loading drawings, PDF & sections…</div>';
    try{
      const r = await fetch(B+'/api/ref/'+encodeURIComponent(card.dataset.pub)+'?slug='+encodeURIComponent(window.SLUG));
      const j = await r.json();
      body.innerHTML = renderBody(j);
      wireTabs(body);
      const mount = body.querySelector('.cgraph');
      if(mount) loadGraph(card.dataset.pub, mount);
    }catch(e){ body.innerHTML = '<div class="nodig">Could not load this reference. '+esc(String(e))+'</div>'; }
  }
}

function renderBody(j){
  const d = j.display||{}, s = j.sections||{}, m = j.matched||{};
  let h = '';
  // AI rationale
  if(j.rationale && j.rationale.why){
    h += '<div class="why"><b>Why relevant:</b> '+esc(j.rationale.why);
    if(j.rationale.reads_on && j.rationale.reads_on.length)
      h += '<div style="margin-top:5px" class="small muted">reads on: '+j.rationale.reads_on.map(esc).join(' · ')+'</div>';
    h += '</div>';
  }
  // drawings
  h += '<div class="draws">';
  if(d.images && d.images.length){
    h += '<div class="g">';
    d.images.forEach((im,i)=>{
      const url=B+'/figures/'+encodeURIComponent(d.pub)+'/'+im.file;
      h += '<figure><img loading="lazy" src="'+url+'" data-pub="'+esc(d.pub)+'" data-i="'+i+'" onclick="openLb(this)">'
        + '<figcaption>fig '+(i+1)+'</figcaption></figure>';
    });
    h += '</div>';
  } else {
    h += '<div class="nodig">🗎 Facsimile not digitized for this document (common for pre-2000 EP/DE/WO). '
      + 'View on <a href="'+esc(d.google_patents||'#')+'" target="_blank">Google Patents</a> · '
      + '<a href="'+esc(d.espacenet||'#')+'" target="_blank">Espacenet</a></div>';
  }
  if(d.pdf_local){ h += '<div style="margin-top:8px"><a class="btn ghost sm" href="'+B+'/pdf/'+encodeURIComponent(d.pub)+'" target="_blank">📄 Open PDF facsimile</a></div>'; }
  h += '</div>';

  // tabs
  const claims = (s.claims&&s.claims.length)?s.claims:[];
  const paras = (s.paragraphs&&s.paragraphs.length)?s.paragraphs:[];
  const figs = (s.figures&&s.figures.length)?s.figures:[];
  const cites = (s.citations&&s.citations.length)?s.citations:[];
  const mc = (m.coord_raw)||{};
  h += '<div class="tabs">';
  const tabs = [['abstract','Abstract'],['claims','Claims ('+claims.length+')'],
    ['paras','Description ('+paras.length+')'],['figs','Figures ('+figs.length+')'],
    ['cites','Citations ('+cites.length+')']];
  tabs.forEach((t,i)=>h+='<div class="tab'+(i==0?' active':'')+'" data-t="'+t[0]+'">'+t[1]+'</div>');
  h += '</div><div class="tabpanes">';

  // abstract
  h += '<div class="tabpane active" data-t="abstract"><div class="abstract">'+(hl(d.abstract)|| '<span class="muted">No abstract.</span>')+'</div></div>';
  // claims
  h += '<div class="tabpane" data-t="claims">';
  if(claims.length){
    claims.forEach(c=>{
      const matched = (mc.claim_no!=null && String(mc.claim_no)===String(c.claim_no));
      h += '<div class="claim'+(c.independent?' indep':'')+(matched?' matched':'')+'">'
        + '<span class="cn">'+ (c.claim_no!=null?c.claim_no+'.':'') +'</span>'
        + (c.independent?'<span class="ind">INDEP</span>':'')
        + (matched?' <span class="ind" style="color:#8a5300">◀ matched</span>':'')
        + '<div style="margin-top:3px">'+hl(c.text)+'</div></div>';
    });
  } else h+='<span class="muted">Claims not ingested for this document — see the PDF / Google Patents.</span>';
  h += '</div>';
  // paragraphs
  h += '<div class="tabpane" data-t="paras">';
  if(paras.length){
    paras.forEach(p=>{
      const matched = (mc.para_no!=null && String(mc.para_no)===String(p.para_no));
      h += '<div class="para'+(matched?' matched':'')+'">'
        + '<span class="coord">'+esc(p.para_no||'')+(p.page_no?(' · p'+p.page_no):'')+'</span>'
        + (p.heading?'<span class="hd">'+esc(p.heading)+'</span> ':'')
        + (matched?'<span class="ind" style="color:#8a5300">◀ matched</span> ':'')
        + hl(p.text)+'</div>';
    });
  } else h+='<span class="muted">Description paragraphs not ingested (expanded-tier / non-US doc). Use the PDF.</span>';
  h += '</div>';
  // figures
  h += '<div class="tabpane" data-t="figs">';
  if(figs.length){ figs.forEach(f=>{h+='<div class="figcap"><span class="fno">FIG '+esc(f.figure_no||'')+'</span> '+hl(f.caption)
      +(f.reference_numbers&&f.reference_numbers.length?' <span class="muted small">refs '+f.reference_numbers.join(', ')+'</span>':'')+'</div>';});}
  else h+='<span class="muted">No figure captions.</span>';
  h += '</div>';
  // citations
  h += '<div class="tabpane" data-t="cites"><div class="cites">';
  if(cites.length){ cites.forEach(c=>{h+='<div class="ci">'+(c.category?'<span class="cat">'+esc(c.category)+'</span>':'')
      +'<a href="https://patents.google.com/patent/'+esc((c.pub||'').replace(/-/g,''))+'/en" target="_blank">'+esc(c.pub)+'</a>'
      +'<span class="muted small">'+esc(c.origin||'')+'</span></div>';});}
  else h+='<span class="muted">No citation edges stored.</span>';
  h += '</div></div>';

  h += '</div>'; // tabpanes
  h += '<div class="cgraph" data-pub="'+esc(d.pub)+'"></div>'; // citation graph mount (lazy)
  return h;
}

function wireTabs(scope){
  scope.querySelectorAll('.tab').forEach(t=>{
    t.onclick=()=>{
      const pane=t.dataset.t, panes=scope.querySelector('.tabpanes');
      scope.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===t));
      panes.querySelectorAll('.tabpane').forEach(p=>p.classList.toggle('active',p.dataset.t===pane));
    };
  });
}

function jumpRef(pub){
  const el=document.getElementById('ref-'+pub);
  if(!el){ alert(pub+' is not in the top ranked list.'); return; }
  el.scrollIntoView({behavior:'smooth',block:'center'});
  if(!el.classList.contains('open')) toggleCard(el.querySelector('.refhead'));
  el.style.transition='box-shadow .3s'; el.style.boxShadow='0 0 0 3px #2a6cf0';
  setTimeout(()=>el.style.boxShadow='',1400);
}

// ---- lightbox ------------------------------------------------------------------------------
let LB={imgs:[],i:0};
function openLb(img){
  const card=img.closest('.refbody');
  LB.imgs=[...card.querySelectorAll('.draws img')]; LB.i=LB.imgs.indexOf(img);
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
    rank:c=>+c.dataset.rank, score:c=>-c.dataset.score,
    date:c=>-(Date.parse(c.dataset.date)||0), datea:c=>(Date.parse(c.dataset.date)||9e15),
    juris:c=>c.dataset.juris, covers:c=>-c.dataset.ncovers
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

// ---- citation graph (lazy, appended to card body) ------------------------------------------
async function loadGraph(pub, mount){
  mount.innerHTML = '<h5>Citations & similar</h5><span class="muted small">loading…</span>';
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
  }catch(e){ mount.innerHTML = '<h5>Citations & similar</h5><span class="muted small">unavailable</span>'; }
}

document.addEventListener('DOMContentLoaded',()=>{ if(document.getElementById('cards')) applyControls(); });
