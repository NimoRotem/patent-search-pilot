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
  let polling = null;
  let reviewing = false;
  let refreshSerial = Promise.resolve();

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

  /* How a review's verdict is said, in the masthead, in Review and against every version in
     History. It lived at the top of the conversation panel, which the terminal replaced. */
  const VERDICT = {
    pass: ['good', 'consistent'], warn: ['warn', 'points to settle'],
    fail: ['bad', 'not consistent yet'], unknown: ['muted', 'not reviewed'],
  };

  // ── the drafting agent's terminal ──────────────────────────────────────────
  /* The agent is a real Claude Code session with this draft as its working directory, and this is
     that session's screen. It is the operators' own terminal, ported: the same append-only line
     renderer, the same clean view, the same live indicator lifted out of the transcript so a
     ticking counter never repaints the page. What is NOT here is everything that belongs to
     running a fleet - auto-push, idle nudge, skills, profiles, a session list, and the clean-view
     switch itself. There is one session, it is this application's, and clean is simply how it is
     drawn.

     THE RENDERER IS APPEND-ONLY AND THAT IS A HARD INVARIANT. It paints one <div class="tl"> per
     line and diffs by common prefix and suffix, so untouched lines keep their DOM node - which is
     what stops the view jumping and what lets a selection survive an update. It only holds if
     everything upstream is append-only too: if a function's output for line i can change when
     line i+1 arrives, it will move the page under the reader. Two plausible-looking things broke
     that before (a global de-indent, and a look-ahead window in the wrap estimate) and both were
     found by measuring rather than by reading. */

  //  One session, so one state object rather than the dashboard's map of them.
  //  `frozen`/`frozenAtLines` hold the PAINT while a selection is being made; polling and
  //  `fullText` carry on regardless. `painted` is the HTML of every line on screen, which is what
  //  the diff runs against. `_bodyText` is the transcript with the live counter already stripped,
  //  so a repaint of the counter alone is a no-op. `live` is what that counter said.
  const TERM = {
    polling: false, timer: null, knownLines: 0, userScrolledUp: false, visibleHash: '',
    firstLoad: true, fullText: '', paneWidth: 132, frozen: false, frozenAtLines: 0,
    painted: null, _bodyText: null, _flowMode: null, live: null, _pendingRender: false,
  };
  function getRawState() { return TERM; }

  let agentState = { status: 'unknown', detail: '', running: false, available: true, reason: '' };
  function termBusy() { return agentState.status === 'busy'; }

  const _LEADING_BULLET_RE=/^[\s]*[●⏺•·]/;
  // Output markers: Claude's ⎿, Codex's └. `│` is a wrapped *command* row. The
  // box-drawing ones must be indented — Codex's start-up banner draws its frame
  // with │ at column 0 and we do not want to eat that.
  const _OUTPUT_MARKER_RE=/^[\s]*⎿|^\s+[└╰]\s/;
  const _CALL_CONT_RE=/^\s+│/;
  // A rendered markdown TABLE is drawn with the same `│` Codex uses for a wrapped
  // command row, and its rules start with the same `└`/`├` used for tool output:
  //
  //   ┌──────────────┬──────────┐
  //   │   channel    │ visitors │     <- matched _CALL_CONT_RE -> mode='call'
  //   ├──────────────┼──────────┤
  //
  // so clean view used to swallow the whole table from its second line on, and
  // every indented line after it (the prose that followed the table) with it. The
  // discriminator is the column count: a wrapped command row carries exactly one
  // leading `│`, a table row has one per column boundary. Rule rows are pure
  // box-drawing. ASCII tables must open AND close with `|` — a bare `|` mid-line
  // is a shell pipe in a wrapped command, not a column.
  const _BOX_RULE_RE=/^[─│┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬╭╮╯╰━┃┏┓┗┛┣┫┳┻╋\s]+$/;
  function _isTableRow(line){
    const s=(line||'').trim();
    if(!s)return false;
    if(s.charAt(0)==='│')return (s.match(/│/g)||[]).length>=2;
    if('┌├└╭╰┏┣┗┬┼┴═╔╠╚'.indexOf(s.charAt(0))>=0)return _BOX_RULE_RE.test(s)&&s.length>=3;
    if(s.charAt(0)==='|'&&s.charAt(s.length-1)==='|')return (s.match(/\|/g)||[]).length>=2;
    return false;
  }
  const _ANY_DECORATION_RE=/^[\s●⏺•·■□▶▸→↳⎼└├│>*\-​]+/;
  // Claude-style `ToolName(args)`. The "(" is required so ordinary prose starting
  // with a word like "Read"/"Write"/"Update"/"Add"/"Task" is never swallowed.
  const _TOOL_PAREN_RE=/^(?:(?:Bash|BashOutput|Fetch|WebFetch|Read|Edit|MultiEdit|Write|NotebookEdit|Update|Grep|Glob|Task|Search|WebSearch|TodoWrite|Kill|Add|Agent|Artifact|Skill|Workflow|ToolSearch)\s*\(|mcp__[^(\s]+\s*\()/i;
  // Codex verbs that are only ever emitted as a tool header, never as prose.
  const _CODEX_VERB_RE=/^(?:Ran|Explored|Called|Searched|Listed|Viewed|Applied patch|Proposed patch|Reviewed|Running|Exploring|Reading|Editing|Writing)\b/;
  // Codex file ops carry a diff stat: "Edited app.py (+12 -3)", "Added x.md (+40)".
  // The stat is what tells them apart from prose ("Added swap and a monitor").
  const _CODEX_FILEOP_RE=/^(?:Edited|Added|Created|Wrote|Updated|Deleted|Removed|Renamed|Moved|Read|Patched)\b.*\([+-]?\d+(?:\s+[+-]\d+)?\)\s*$/;
  function _isToolHeader(line,followerIsMarker){
    const stripped=line.replace(_ANY_DECORATION_RE,'');
    if(_TOOL_PAREN_RE.test(stripped))return true;
    if(_CODEX_VERB_RE.test(stripped))return true;
    if(_CODEX_FILEOP_RE.test(stripped))return true;
    // Structural fallback: a bullet whose next non-empty row is a `│`/`└`/`⎿`
    // marker is a tool block whatever the verb is. Prose bullets never are.
    return !!followerIsMarker;
  }
  // Kept for compatibility with the old two-toggle helper name.
  function _isBashFetchHeader(line){return _isToolHeader(line,false)}
  // Package/self-update chatter, plus the per-turn bookkeeping and footer hint
  // bars both CLIs print. These wrap, so a match opens a suppression block.
  const _NOISE_RES=[
    /^checking for updates?/i,
    /^installing\b.*\b(claude|codex|update|npm|node|package|version|v?\d)/i,
    /^downloading\b.*\b(claude|codex|update|npm|version|package|v?\d)/i,
    /\b(update (installed|complete|available)|successfully updated|already up to date|(claude code|codex) v?\d[\d.]* installed)\b/i,
    /^npm\b/i,
    /\bnpm (warn|notice|info|err|http|verb|sill|deprecated|audit|fund)\b/i,
    /^(added|changed|removed|audited)\s+\d+\s+packages?\b/i,
    /\bpackages?\b[^\n]*\blooking for funding\b/i,
    /^found \d+ vulnerabilit/i,
    /\bnpm audit\b/i,
    /\bto (apply|finish|complete) the update\b/i,
    /^restart (claude|codex)\b/i,
    /^[\[(][#=>\-.\s]{3,}[\])]/,
    /^token usage:\s/i,
    /^to continue this session, run (codex|claude) resume\b/i,
    /^…\s*\+\d+\s+lines?\b/,
    /^\+\d+\s+lines?\b/,
    /^tip:\s/i,
    /^context (left|remaining|window):/i,
    /^esc to interrupt\b/i,
    /^shell cwd was reset\b/i,
    /\(ctrl ?\+ ?[to] to (view transcript|expand)\)/i,
    /^made \d+\s+\S+.*\b(edit|change)/i,
    /^⏵/,
    /\bnew task\?\s*\/clear to save\b/i,
    /^shift\+tab to cycle\b/i,
  ];
  function _isNoise(line){
    if(!line)return false;
    const s=line.replace(/^[\s⎿●⏺•·│└├>*\-]+/,'').trim();
    if(!s)return false;
    for(let i=0;i<_NOISE_RES.length;i++){if(_NOISE_RES[i].test(s))return true;}
    return false;
  }
  // Kept for compatibility with the old two-toggle helper name.
  function _isUpdateNoise(line){return _isNoise(line)}
  // Structural furniture the pane draws at column 0: the `›`/`❯` composer prompt,
  // the start-up banner box, and the horizontal rules between turns. These always
  // end a hidden block.
  const _PANE_STRUCTURE_RE=/^[›❯>]\s|^[╭╰╮╯│├┤─━═]/;
  // A shell prompt line ("nimo@host:~/dir$ codex --yolo") and the `export DASH_…`
  // / `cd -- …` plumbing the dashboard types to start a session. Only stripped in
  // panes that are actually running an agent — a plain shell session would
  // otherwise render as an empty page.
  const _SHELL_PROMPT_RE=/^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^\s]*\s*[$#]\s?/;
  function _looksLikeAgentPane(text){
    return /^[\s]*[●⏺•]/m.test(text)||text.indexOf('⎿')>=0||
           text.indexOf('OpenAI Codex')>=0||text.indexOf('Claude Code')>=0;
  }

  // ── The live status block, and the chrome around it ─────────────────────────
  // Both CLIs keep a status area pinned to the foot of the pane and repaint it
  // several times a second:
  //
  //   · Gitifying… (1m 36s · ↓ 2.9k tokens · thought for 1s)
  //   ────────────────────────────────────────────────────────
  //   ❯
  //   ────────────────────────────────────────────────────────
  //     ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
  //
  // None of it is transcript. The counter alone changed the buffer every second,
  // which forced a repaint every second, which is why the page moved under anyone
  // trying to read or copy. The spinner row is cut out of the text in BOTH views
  // and re-drawn by us outside the scroll area (see updateLiveBar); the rules, the
  // composer and the hint bar are cut in clean view.
  //
  // The glyph set deliberately excludes ●, ⏺ and • — those are the agent's real
  // prose/tool bullets. `·` doubles as a spinner frame, so a match also has to
  // carry evidence: a clock, a token tally, or the interrupt hint.
  const _LIVE_HEAD_RE=/^ {0,6}([✻✽✢✳✴✱✲✵✶✷✸✹✺✧✦·⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒▌▐])[ \t]+(\S.*)$/;
  // AND THE PLAIN ASTERISK. Measured on Claude Code 2.1.250 by capturing a live pane
  // frame by frame: the spinner cycles · ✻ ✽ ✢ ✶ and, one frame in five, a bare
  // ASCII `*`. That one frame did not match, so the status row survived into the
  // transcript on every fifth poll and the whole view jumped a row down and back,
  // twice a second, which is exactly what a reader sees as "the text is bouncing".
  // It gets its own pattern rather than a place in the class above, because `*` also
  // opens a markdown bullet: this one additionally requires the spinner's own shape,
  // a single capitalised word ending in an ellipsis.
  const _LIVE_ASCII_HEAD_RE=/^ {0,6}([*∗])[ \t]+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’-]*…\s*\(.*)$/;
  const _LIVE_EVID_RE=/\(\s*\d+\s*[hms]\b|\bfor\s+\d+\s*[hms]\b|\besc to interrupt\b|\btokens?\b|\bthought for\b|…\s*\(/i;
  const _LIVE_VERB_RE=/^([A-Za-z][A-Za-zÀ-ÿ'’-]{1,24})/;   // accents count: "Sautéing…" is one word
  const _LIVE_TIME_RE=/(?:^|[\s(·•])(?:(\d+)\s*h\s*)?(?:(\d+)\s*m\s*)?(\d+)\s*s\b/;
  const _LIVE_TOK_RE=/([↑↓⇡⇣])?\s*([\d.]+\s*[kKmM]?)\s*tokens?/;
  function _liveStatusParts(line){
    const m=_LIVE_HEAD_RE.exec(line||'')||_LIVE_ASCII_HEAD_RE.exec(line||'');
    return m&&_LIVE_EVID_RE.test(m[2])?m:null;
  }
  function _isLiveStatusLine(line){
    return !!_liveStatusParts(line);
  }
  function _readLiveStatus(line,live){
    const m=_liveStatusParts(line);
    if(!m)return;
    const rest=m[2];
    const v=_LIVE_VERB_RE.exec(rest);
    if(v)live.verb=v[1];
    const t=_LIVE_TIME_RE.exec(rest);
    if(t)live.sec=(parseInt(t[1]||'0',10)*3600)+(parseInt(t[2]||'0',10)*60)+parseInt(t[3]||'0',10);
    const k=_LIVE_TOK_RE.exec(rest);
    if(k){live.dir=k[1]||'';live.tok=k[2].replace(/\s+/g,'')}
    live.esc=/esc to interrupt/i.test(rest);
    live.seen=true;
  }
  // Full-width horizontal rule. Box-drawing only, and long: an agent printing a
  // markdown `---` or a table border must survive (a table border opens with
  // ┌ ├ └, never with a bare ─).
  const _CHROME_RULE_RE=/^\s*[─━═]{18,}\s*$/;
  // The composer, and it has to be an EMPTY one: Claude Code echoes every message
  // you sent back with the same `❯` prefix, and those are the transcript, not
  // chrome. `>` is left out entirely — it starts a quoted line in prose.
  const _CHROME_COMPOSER_RE=/^\s*[❯›»][\s █▌│]*$/;
  // The hint bar under it, and the badges the CLIs park beside it.
  const _CHROME_HINT_RE=/^\s*(?:⏵|\?\s*for shortcuts|shift\+tab\b|ctrl\+[a-z0-9]+ to\b|esc to (?:interrupt|undo|clear)\b|⧉\s*In\b|↑\s*to (?:edit|recall)\b|⌥|bypassing permissions\b|\d+%\s+context left\b|←\s*for agents\b)/i;
  function _isPaneChrome(line){
    return _CHROME_RULE_RE.test(line)||_CHROME_COMPOSER_RE.test(line)||_CHROME_HINT_RE.test(line);
  }
  // Pull the live status out of the buffer and read it. Runs in both views, and
  // runs before anything else, so a repaint of the counter alone never reaches the
  // renderer. Returns the body with those rows (and the trailing blank rows the
  // pane pads itself with) removed.
  function splitLiveTail(lines){
    const live={verb:'',sec:null,tok:'',dir:'',esc:false,seen:false};
    const body=[];
    for(let i=0;i<lines.length;i++){
      if(_isLiveStatusLine(lines[i])){_readLiveStatus(lines[i],live);continue}
      body.push(lines[i]);
    }
    while(body.length&&body[body.length-1].trim()==='')body.pop();
    return {body:body,live:live};
  }
  // The dashboard starts a session by typing a shell line into it:
  //
  //   nimrod_rotem@instance-3:~/tmux-dashboard-original$ export DASH_USER=Nimo …
  //   DASH_PROJECT_DIR=/home/… GIT_AUTHOR_NAME=… GIT_COMMITTER_EMAIL=…
  //
  // which the terminal then wraps over half a dozen rows. It is the first thing
  // anyone opening the session reads and it means nothing to them. Cut everything
  // from the top down to the first row that is actually the agent talking. Scoped
  // to the head of the buffer and to panes that did start an agent, so a plain
  // shell session is never blanked.
  const _ENV_ASSIGN_RE=/^[A-Za-z_][A-Za-z0-9_]*=/;
  // The launch line itself, for the case where the prompt that carried it has
  // already scrolled out of tmux's history and only the command is left.
  const _LAUNCH_CMD_RE=/^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:claude|codex)\b|--dangerously-skip-permissions|CLAUDE_CODE_OAUTH_TOKEN=|^(?:source|\.)\s+\S*\/\.tmux-dashboard\/launch\//;
  function _stripStartupPreamble(lines){
    let i=0,sawShell=false;
    const limit=Math.min(lines.length,120);
    while(i<limit){
      const l=lines[i],t=l.trim();
      if(t===''){i++;continue}
      if(_SHELL_PROMPT_RE.test(l)||_LAUNCH_CMD_RE.test(t)){sawShell=true;i++;continue}
      if(!sawShell)break;
      // Wrapped tail of that command: bare VAR=value pairs, `export`/`cd`/`&&`
      // fragments, and the CLI's own "starting…" chatter.
      if(_ENV_ASSIGN_RE.test(t)||/^(export|cd|source|env|exec|nohup|&&|\|\|)\b/.test(t)||
         /^--?[a-z]/.test(t)||/^(claude|codex)\b/.test(t)){i++;continue}
      // The compact start-up banner, which is the CLI's block-glyph logo with the
      // version, the model and the cwd set beside it. Not drawn as a box, so
      // _stripStartupBanner cannot see it; three or more logo glyphs on a row is
      // the tell.
      if((l.match(/[▐▛▜▌▝▘█▄▀]/g)||[]).length>=3){i++;continue}
      break;
    }
    return sawShell?lines.slice(i):lines;
  }
  // The welcome box each CLI draws on start-up ("✻ Welcome to Claude Code", cwd,
  // model, /help hint). It is a box so _isTableRow keeps it; drop it by content.
  const _BANNER_HINT_RE=/welcome (to|back)\b|(claude code|openai codex)\s+v?\d|\/help for help|\/init to create|release-notes|tips for getting|what's new|cwd:|workdir:|model:\s|approval:|sandbox:|press enter to continue/i;
  function _stripStartupBanner(lines){
    const out=[];
    let box=null;
    for(let i=0;i<lines.length;i++){
      const t=lines[i].trim();
      const opens=/^[╭┌]/.test(t),closes=/^[╰└]/.test(t);
      if(box===null&&opens){box={rows:[lines[i]],hit:false};continue}
      if(box!==null){
        box.rows.push(lines[i]);
        if(_BANNER_HINT_RE.test(t))box.hit=true;
        if(closes||box.rows.length>14){
          if(!box.hit)for(const r of box.rows)out.push(r);
          box=null;
        }
        continue;
      }
      out.push(lines[i]);
    }
    if(box!==null&&!box.hit)for(const r of box.rows)out.push(r);
    return out;
  }
  function applyRawFilter(text){
    if(!true)return text;
    if(!text)return text;
    let lines=text.split('\n');
    // Decided on the ORIGINAL text: the start-up banner is one of the tells, and
    // we are about to delete it.
    const agentPane=_looksLikeAgentPane(text);
    if(agentPane){
      lines=_stripStartupPreamble(lines);
      lines=_stripStartupBanner(lines);
    }
    lines=lines.filter(l=>!_isPaneChrome(l));
    // Index of the next non-empty line, for the structural tool-block test and
    // for deciding whether a blank row ends a hidden block or sits inside one.
    const nextNonEmpty=new Array(lines.length).fill(-1);
    for(let i=lines.length-2;i>=0;i--){
      nextNonEmpty[i]=lines[i+1].trim()===''?nextNonEmpty[i+1]:i+1;
    }
    const out=[];
    // One suppression state machine:
    //   'call'   — a hidden tool-call header and its wrapped command rows.
    //   'output' — a hidden ⎿/└ result block and its indented continuation rows.
    //   'noise'  — a hidden bookkeeping/update line and its wrapped rows.
    //   'shell'  — a hidden shell command and its wrapped rows.
    // Suppression ends at a bullet, at pane structure, or at a blank row that is
    // followed by something starting at column 0 — so the agent's spoken text and
    // its paragraph breaks always survive.
    let mode='';
    for(let i=0;i<lines.length;i++){
      const line=lines[i];
      // Table rows are content, and they end any block that was being hidden —
      // tested before the marker rules, which would otherwise claim them. Two
      // blocks still win: a `⎿`/`└` result (a table printed BY a command is tool
      // output, and breaking out would leak the rest of the block) and the
      // launch-command block (whose tail is Claude Code's own `╭…│…╰` banner).
      if(mode!=='output'&&mode!=='shell'&&_isTableRow(line)){mode='';out.push(line);continue;}
      if(_isNoise(line)){mode='noise';continue;}
      if(_CALL_CONT_RE.test(line)){mode='call';continue;}
      if(_OUTPUT_MARKER_RE.test(line)){mode='output';continue;}
      // A tool call that is still RUNNING is drawn without its bullet — Claude
      // only stamps the ● once the call returns. So `  Bash(cd …` sits there at an
      // indent with nothing to mark it, and the bullet branch below never sees it.
      // `Name(` at the start of a line is never prose, so match it on its own.
      if(_TOOL_PAREN_RE.test(line.replace(_ANY_DECORATION_RE,''))){mode='call';continue;}
      if(_LEADING_BULLET_RE.test(line)){
        const nx=nextNonEmpty[i];
        const followerIsMarker=nx>=0&&(_OUTPUT_MARKER_RE.test(lines[nx])||_CALL_CONT_RE.test(lines[nx]));
        if(_isToolHeader(line,followerIsMarker)){mode='call';continue;}
        mode='';out.push(line);continue;   // prose bullet
      }
      if(agentPane&&_SHELL_PROMPT_RE.test(line)){mode='shell';continue;}
      if(line.trim()===''){
        // A blank row inside a hidden block (command output routinely has them)
        // must not end the suppression, or the tail of the block leaks out. It
        // ends when the block does — when the next real row is a bullet or
        // starts at column 0.
        const nx=nextNonEmpty[i];
        if(mode&&nx>=0&&/^\s/.test(lines[nx])&&!_LEADING_BULLET_RE.test(lines[nx]))continue;
        mode='';out.push(line);continue;
      }
      if(/^\S/.test(line)){
        // Column-0 non-space. Inside an agent pane every spoken word sits inside
        // a `•` bullet at an indent, so a column-0 row reached while a block is
        // being hidden is that block's wrapped tail, not prose — drop it. Pane
        // structure (rules, banner, `›` prompt) still breaks out.
        if(mode&&agentPane&&!_PANE_STRUCTURE_RE.test(line))continue;
        if(mode==='shell')continue;
        mode='';out.push(line);continue;
      }
      // Indented continuation row — dropped if it belongs to a hidden block.
      if(mode)continue;
      out.push(line);
    }
    // Removing whole blocks leaves ragged runs of blank rows behind. Collapse
    // them to a single separator so the conversation reads as prose.
    const tidy=[];
    for(const l of out){
      if(l.trim()===''&&(!tidy.length||tidy[tidy.length-1].trim()===''))continue;
      tidy.push(l);
    }
    return tidy.join('\n');
  }
  function _escTermHtml(s){
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  // Linkify http(s):// URLs and absolute file paths in raw terminal output.
  // URL handling:
  //  (1) URL on one logical line — escape, trim trailing punctuation, wrap in <a>.
  //  (2) URL split across multiple rows by Claude Code's alt-screen TUI — when a
  //      whitespace/newline appears at column ≥ paneWidth-4 followed by row
  //      padding and a URL-valid char on the next row, treat as a soft wrap:
  //      strip padding+newline from href but emit per-chunk <a> tags so the
  //      visual layout matches the terminal and every chunk is clickable.
  // URLs only. The operators' dashboard also turns an absolute path into a link to
  // its file viewer; the studio has no file viewer, and a patent draft is not a
  // place to browse the server from, so a path here stays text.
  function _findNextLinkable(text,from){
    const urlIdx=text.slice(from).search(/https?:\/\//);
    return urlIdx>=0?{kind:'url',start:from+urlIdx}:null;
  }
  function _renderUrlSpan(text,start,paneWidth){
    const pw=Math.max(20,paneWidth||80);
    const wrapCol=pw-4;
    const MAX_WRAP_LINES=20;
    const MAX_URL_LEN=4096;
    let j=start;
    let crossedNewlines=0;
    while(j<text.length&&(j-start)<MAX_URL_LEN){
      const ch=text[j];
      if(ch==='<'||ch==='>'||ch==='"'||ch==="'"||ch==='`')break;
      if(/\s/.test(ch)){
        let ls=j;
        while(ls>0&&text[ls-1]!=='\n')ls--;
        const col=j-ls;
        if(col<wrapCol)break;
        let k=j;
        while(k<text.length&&(text[k]===' '||text[k]==='\t'))k++;
        if(k>=text.length||text[k]!=='\n')break;
        if(crossedNewlines>=MAX_WRAP_LINES)break;
        const next=text[k+1];
        if(!next||/\s/.test(next))break;
        if(next==='<'||next==='>'||next==='"'||next==="'"||next==='`')break;
        const nextSlice=text.slice(k+1,k+9);
        if(nextSlice.startsWith('http://')||nextSlice.startsWith('https://'))break;
        crossedNewlines++;
        j=k+1;
        continue;
      }
      j++;
    }
    const urlRaw=text.slice(start,j);
    const hasNewlines=urlRaw.indexOf('\n')>=0;
    let href=urlRaw.replace(/[ \t]*\n[ \t]*/g,'');
    let trailText='';
    if(!hasNewlines){
      while(href.length>0&&_RAW_URL_TRAIL_RE.test(href[href.length-1])){
        trailText=href[href.length-1]+trailText;
        href=href.slice(0,-1);
      }
    }
    let html;
    if(href.length===0){
      html=_escTermHtml(urlRaw);
    }else if(!hasNewlines){
      const dispText=urlRaw.slice(0,urlRaw.length-trailText.length);
      html='<a href="'+_escTermHtml(href)+'" target="_blank" rel="noopener noreferrer" class="raw-link">'+_escTermHtml(dispText)+'</a>'+_escTermHtml(trailText);
    }else{
      const parts=urlRaw.split(/(\s+)/);
      const hrefEsc=_escTermHtml(href);
      html='';
      for(const part of parts){
        if(!part)continue;
        if(/^\s+$/.test(part)){
          html+=_escTermHtml(part);
        }else{
          html+='<a href="'+hrefEsc+'" target="_blank" rel="noopener noreferrer" class="raw-link">'+_escTermHtml(part)+'</a>';
        }
      }
    }
    return {html:html,end:j};
  }
  function _linkifyTerminalText(text,paneWidth){
    if(!text)return '(empty)';
    let out='';
    let i=0;
    while(i<text.length){
      const hit=_findNextLinkable(text,i);
      if(!hit){out+=_escTermHtml(text.slice(i));break;}
      out+=_escTermHtml(text.slice(i,hit.start));
      const rendered=_renderUrlSpan(text,hit.start,paneWidth);
      out+=rendered.html;
      i=rendered.end;
      if(i<=hit.start)i=hit.start+1;
    }
    return out;
  }
  // Which rendered line is this node in? Used to decide whether a paint would
  // actually disturb the reader's selection, rather than assuming it would.
  function _lineIndexOf(el,node){
    let x=node;
    while(x&&x.parentNode!==el)x=x.parentNode;
    if(!x)return -1;
    return Array.prototype.indexOf.call(el.children,x);
  }
  // A paint only destroys a selection if it rewrites a line the selection is in.
  // The agent appends at the tail, so a reader highlighting something further up
  // is not affected and there is no reason to hold the update back.
  function _selectionBelow(el,fromIdx){
    const sel=window.getSelection?window.getSelection():null;
    if(!sel||sel.isCollapsed||sel.rangeCount===0)return false;
    for(let i=0;i<sel.rangeCount;i++){
      const r=sel.getRangeAt(i);
      if(!(el.contains(r.startContainer)||el.contains(r.endContainer)))continue;
      const a=_lineIndexOf(el,r.startContainer),b=_lineIndexOf(el,r.endContainer);
      if(a<0||b<0)return true;                 // can't place it — play safe
      if(Math.max(a,b)>=fromIdx)return true;
    }
    return false;
  }
  // Once the user's selection moves off the lines a paint wanted to rewrite, flush
  // it. renderRawText re-tests the overlap itself, so this can just ask again.
  function _flushDeferredRawRenders(){
    const st=getRawState();
    if(st._pendingRender&&!st.frozen&&$('termOut'))renderRawText();
  }
  document.addEventListener('selectionchange',_flushDeferredRawRenders);
  // A selection that does not exist yet cannot be detected by selectionchange: for
  // the first pixels of a drag the range is still collapsed, so a re-render landing
  // in that window rips the nodes out from under the mouse and the highlight never
  // starts. Hold every terminal paint while a button is down inside one, and flush
  // on release. Delegated at the document level on purpose — the terminal element
  // is rebuilt on every detail re-render, so per-element listeners would leak.
  let _rawDragEl=null;
  document.addEventListener('mousedown',function(e){
    _rawDragEl=(e.target&&e.target.closest)?e.target.closest('.raw-output'):null;
  },true);
  document.addEventListener('mouseup',function(){
    if(!_rawDragEl)return;
    _rawDragEl=null;
    _flushDeferredRawRenders();
  },true);
  // ── Undoing the terminal's own line breaks ──────────────────────────────────
  // Claude Code and Codex hard-wrap their output to the pane width before tmux
  // ever sees it, so a paragraph arrives as N rows of ~78 columns and a long URL
  // arrives cut in half. That is the whole of two complaints at once: the text is
  // stuck at 80 columns (too wide for a phone, half the screen wasted on a
  // desktop) and a link split across two rows is not a link any more.
  //
  // A row continues the one above it only if the first word of the row would NOT
  // have fitted on it — the exact inverse of the greedy wrap that produced them.
  // "Did the row reach the right margin" is not enough on its own: two short lines
  // that happen to be long-ish get glued together, and a two-column list turns
  // into porridge. The word test never merges those, because the next line's first
  // word plainly would have fitted.
  const _BLOCK_START_RE=/^\s*(?:[●⏺•✱✲✳✴✵✶✷✸✹✺✧✦⎿└├┌┐┘╭╮╯╰│┤┬┴┼─━═║╔╗╚╝▌▐❯›»>]|[-*+]\s|\d+[.)]\s|#{1,6}\s|[✓✔✗✘◻☐☑▢]\s)/;
  // A token too long to finish the row is cut at the very last column and picked
  // up at column 0 on the next one, whatever the paragraph's indent was — which is
  // exactly how a URL comes through:
  //
  //     (https://docs.google.com/spreadsheets/d/1I5Lii…UNfAUe8oObwbE/edit    <- col 80
  //   ?gid=1321488188).                                                     <- col 0
  //
  // So a column-0 row is still a continuation when the row above filled the pane
  // and the two halves join into one token. Without this the link stays in two
  // pieces and neither half is clickable.
  function _isTokenCut(prev,row,limit){
    return prev.length>=limit&&_wrapGlue(prev,row,limit)==='';
  }
  function _isWrapContinuation(prev,row,limit){
    if(!prev||!row)return false;
    if(!prev.trim()||!row.trim())return false;
    if(_isTableRow(prev)||_isTableRow(row))return false;
    if(_BLOCK_START_RE.test(row))return false;
    const pIndent=prev.length-prev.replace(/^\s+/,'').length;
    const rIndent=row.length-row.replace(/^\s+/,'').length;
    // back at column 0 — structure, not a wrap, unless a token was cut there
    if(rIndent===0&&pIndent>0&&!_isTokenCut(prev,row,limit))return false;
    if(rIndent>pIndent+6)return false;        // a deeper block of its own
    const word=row.replace(/^\s+/,'').split(/\s/)[0]||'';
    if(!word)return false;
    // The row above has to have been nearly full for a wrap to be plausible at
    // all, and the word below has to be one that would not have finished it. The
    // 2-column slack absorbs the fact that `limit` is an estimate: the widest row
    // we can see is a lower bound on where the CLI actually broke.
    if(prev.length<limit-10)return false;
    return (prev.length+1+word.length)>limit-2;
  }
  function _wrapGlue(prev,row,limit){
    // A word wrap ate the space that was between the two halves; a token too long
    // for one row was cut with no space at all. Only the second may be rejoined
    // tight, and it is exactly the case that used to leave a URL split down the
    // middle and therefore dead.
    // A token is only ever cut when it fills the row to the very last column. One
    // column short of that and the break was at a space, so the space goes back.
    if(prev.length<limit)return ' ';
    const tail=prev.slice(prev.lastIndexOf(' ')+1);
    const head=row.replace(/^\s+/,'');
    const CH=/[A-Za-z0-9\/._~:?#\[\]@!$&'()*+,;=%-]/;
    if(!CH.test(tail.slice(-1))||!CH.test(head.charAt(0)))return ' ';
    // Anything that ends in punctuation is a finished word, whatever else it looks
    // like. "…cost me a cycle:" ends a clause; it is not half of a URL, and gluing
    // it gave "cycle:the".
    if(/[.,;:!?)\]}'"]$/.test(tail))return ' ';
    // A path that already carries its extension, or ends at a directory slash, is
    // finished too — what follows is the next word, not the rest of the token.
    if((/\.[A-Za-z]{2,5}$/.test(tail)||tail.slice(-1)==='/')&&/^[A-Za-z]/.test(head))return ' ';
    const looksCut=/:\/\//.test(tail)         // carries a scheme
                || /^[~.]?\//.test(tail)      // is a path
                || tail.indexOf('/')>=0       // has a path segment in it
                || /@[A-Za-z0-9-]+$/.test(tail)
                || tail.length>=22;           // one long unbroken token
    return looksCut?'':' ';
  }
  // The wrap column is what the agent actually drew to, not what tmux says the
  // pane is — and it is not one number for the whole buffer. Inside a bordered box
  // (a quoted user message, a plan) the CLI wraps several columns narrower than it
  // does in open prose, so a single buffer-wide maximum makes every paragraph in
  // the box look "not full" and none of it gets rejoined. Estimate it locally.
  //
  // The window looks BACKWARDS only, and that is the whole point. A window that
  // also peeked ahead meant one new row at the bottom could change the estimate
  // for rows above it, re-join them differently, and shift the text under a reader
  // who was nowhere near the bottom. Looking only at what is already settled makes
  // the result append-only: everything above the new row is decided for good.
  const _WRAP_WINDOW=16;
  function _localWrapLimits(rs,paneWidth){
    const n=rs.length,pw=Math.max(40,paneWidth||80),out=new Array(n);
    for(let i=0;i<n;i++){
      let w=0;
      const lo=Math.max(0,i-_WRAP_WINDOW);
      for(let j=lo;j<=i;j++)if(rs[j].length>w)w=rs[j].length;
      out[i]=Math.min(Math.max(w,40),pw);
    }
    return out;
  }
  function _unwrapRows(lines,paneWidth){
    const n=lines.length,rs=new Array(n);
    for(let i=0;i<n;i++)rs[i]=lines[i].replace(/\s+$/,'');
    const limits=_localWrapLimits(rs,paneWidth);
    const out=[];
    let cur=null,lastRow='',lastIdx=0;
    for(let i=0;i<n;i++){
      const row=rs[i],limit=limits[lastIdx];
      if(cur!==null&&_isWrapContinuation(lastRow,row,limit)){
        cur+=_wrapGlue(lastRow,row,limit)+row.replace(/^\s+/,'');
        lastRow=row;lastIdx=i;
        continue;
      }
      if(cur!==null)out.push(cur);
      cur=row;lastRow=row;lastIdx=i;
    }
    if(cur!==null)out.push(cur);
    return out;
  }
  // There is deliberately no global de-indent here. Stripping the pane's common
  // left margin looks tidier, but the common margin is a property of the WHOLE
  // buffer: one new line at column 0 arriving at the bottom re-indents every line
  // above it and the page jumps under the reader. The margin is two columns and
  // the CSS hanging indent handles the rest, so it stays.

  // ── Fitting the grid to the screen ──────────────────────────────────────────
  // Exact mode has to keep the terminal's character grid, so the only free
  // variable is the type size. Fit pane_width columns to the column that is
  // actually there: a phone stops needing to scroll sideways, and a wide desktop
  // stops rendering an 80-column ribbon down the middle of a 1400px window.
  let _termCharRatio=0;
  function _measureCharRatio(el){
    if(_termCharRatio)return _termCharRatio;
    const probe=document.createElement('span');
    probe.style.cssText='position:absolute;visibility:hidden;white-space:pre;left:-9999px;top:0';
    probe.textContent='0123456789'.repeat(10);
    el.appendChild(probe);
    const w=probe.getBoundingClientRect().width;
    const fs=parseFloat(getComputedStyle(probe).fontSize)||13;
    el.removeChild(probe);
    if(w>0&&fs>0)_termCharRatio=(w/100)/fs;
    return _termCharRatio||0.6;
  }
  function fitTerminalFont(el,paneWidth,flow){
    if(flow){el.style.fontSize='';return}
    const ratio=_measureCharRatio(el);
    const cs=getComputedStyle(el);
    const avail=el.clientWidth-(parseFloat(cs.paddingLeft)||0)-(parseFloat(cs.paddingRight)||0)-2;
    if(avail<=0)return;
    const cols=Math.max(20,paneWidth||80);
    const size=Math.max(9,Math.min(15,avail/(cols*ratio)));
    el.style.fontSize=size.toFixed(2)+'px';
  }

  // ── Painting: replace only the lines that changed ───────────────────────────
  // One <div class="tl"> per line, diffed by common prefix and common suffix. In
  // the normal case the agent appends at the tail, the prefix covers everything
  // above it, and not one node the reader is looking at is touched — so nothing
  // shifts, and a highlight in that region survives the update intact.
  function _lineDiff(prev,next){
    const n=next.length,m=prev.length;
    let p=0;
    while(p<n&&p<m&&next[p]===prev[p])p++;
    let s=0;
    while(s<(n-p)&&s<(m-p)&&next[n-1-s]===prev[m-1-s])s++;
    return {from:p,remove:(m-s)-p,insert:(n-s)-p};
  }
  function _applyLineDiff(el,d,next){
    const kids=el.children;
    const reuse=Math.min(d.remove,d.insert);
    for(let k=0;k<reuse;k++){
      const c=kids[d.from+k];
      if(c)c.innerHTML=next[d.from+k]||'';
    }
    if(d.remove>d.insert){
      for(let k=d.remove-1;k>=reuse;k--){
        const c=kids[d.from+k];
        if(c)el.removeChild(c);
      }
    }else if(d.insert>d.remove){
      const before=kids[d.from+reuse]||null;
      const frag=document.createDocumentFragment();
      for(let k=reuse;k<d.insert;k++){
        const div=document.createElement('div');
        div.className='tl';
        div.innerHTML=next[d.from+k]||'';
        frag.appendChild(div);
      }
      el.insertBefore(frag,before);
    }
  }
  function _setRawScroll(el,target){
    const max=Math.max(0,el.scrollHeight-el.clientHeight);
    target=Math.max(0,Math.min(target,max));
    if(Math.abs(el.scrollTop-target)<1)return;
    el.scrollTop=target;
  }
  // A very long scrollback is bounded, with hysteresis so the window does not
  // crawl forward a line at a time (which would repaint everything, every poll).
  const RAW_MAX_LINES=5000;

  function renderRawText(force){
    const st=getRawState();
    const rawEl=$('termOut');
    if(!rawEl)return;
    // The live counter comes off FIRST, in both views. It changes every second,
    // and everything below this line would otherwise re-run every second.
    const split=splitLiveTail((st.fullText||'').split('\n'));
    if(split.live.seen){
      split.live.at=_nowMs();
      st.live=split.live;
    }
    updateLiveBar();
    // Frozen: keep buffering into st.fullText and leave the screen alone. The
    // agent is untouched — only the paint waits, and unfreezing shows the lot.
    if(!force&&st.frozen){st._pendingRender=true;updateFreezeUi();return;}

    const flow=true;
    // A fresh element — switching session or tab rebuilds the whole detail panel —
    // has none of our line divs in it, so whatever we last painted is gone with
    // the old node. Drop the memo BEFORE the no-change short-circuit below, or the
    // new element stays stuck on "Loading Claude Code…" until the text changes.
    if(rawEl._lineMode!==true){rawEl.textContent='';rawEl._lineMode=true;st.painted=null;st._bodyText=null}
    const bodyText=split.body.join('\n');
    if(!force&&bodyText===st._bodyText&&flow===st._flowMode&&st.painted){st._pendingRender=false;return;}

    let body=applyRawFilter(bodyText);
    let rows=body?body.split('\n'):[];
    if(flow){
      rows=_unwrapRows(rows,st.paneWidth);
      while(rows.length&&rows[rows.length-1].trim()==='')rows.pop();
    }else{
      // tmux pads every row out to the pane width. Those trailing spaces are
      // invisible, but they count towards the line box and would wrap a row that
      // otherwise fits exactly.
      rows=rows.map(l=>l.replace(/\s+$/,''));
    }
    if(rows.length>RAW_MAX_LINES+400)rows=rows.slice(rows.length-RAW_MAX_LINES);
    let htmlLines;
    if(!rows.length||!rows.join('').trim()){
      htmlLines=(st.fullText||'').trim()
        ? ['<span style="color:#6e7681">Nothing on this screen but the agent&rsquo;s own chrome. It is waiting for you.</span>']
        : [];
    }else{
      // Linkified as one string so the exact-mode rejoining of URLs and paths
      // split across rows still works; neither renderer ever emits a tag that
      // straddles a newline, so splitting the result back per line is safe.
      htmlLines=_linkifyTerminalText(rows.join('\n'),flow?1000000:st.paneWidth).split('\n');
    }

    const prev=st.painted||[];
    const d=_lineDiff(prev,htmlLines);
    if(!d.remove&&!d.insert){
      st.painted=htmlLines;st._bodyText=bodyText;st._flowMode=flow;st._pendingRender=false;
      return;
    }
    // Hold the paint only if it would actually rip out what the reader is holding:
    // a drag in progress here, or a selection that reaches into the lines about to
    // be rewritten. A highlight further up is left alone and the update goes ahead.
    if(!force&&(_rawDragEl===rawEl||_selectionBelow(rawEl,d.from))){st._pendingRender=true;return;}

    rawEl.classList.toggle('flow',flow);
    rawEl.classList.toggle('exact',!flow);
    if(st._flowMode!==flow||rawEl._fitW!==rawEl.clientWidth||rawEl._fitPw!==st.paneWidth){
      fitTerminalFont(rawEl,st.paneWidth,flow);
      rawEl._fitW=rawEl.clientWidth;rawEl._fitPw=st.paneWidth;
    }

    const atBottom=!st.userScrolledUp;
    const beforeH=rawEl.scrollHeight,beforeTop=rawEl.scrollTop;
    const anchor=rawEl.children[d.from];
    const anchorTop=anchor?anchor.offsetTop:beforeH;
    _applyLineDiff(rawEl,d,htmlLines);
    st.painted=htmlLines;st._bodyText=bodyText;st._flowMode=flow;st._pendingRender=false;
    if(atBottom){
      _setRawScroll(rawEl,rawEl.scrollHeight);
    }else if(anchorTop<beforeTop){
      // Something above the fold moved (a resync, or the scrollback cap trimming
      // the head). Give the reader back the line they were on.
      const delta=rawEl.scrollHeight-beforeH;
      if(delta)_setRawScroll(rawEl,beforeTop+delta);
    }
    // Scrolled up and the change was at or below the top of the viewport: nothing
    // the reader can see has moved, so scrollTop is not touched at all.
    updateFreezeUi();
  }
  // The exact-mode type size is a function of the container width, so it has to be
  // recomputed when the window changes shape (rotating a phone, most of all).
  let _refitTimer=null;
  window.addEventListener('resize',function(){
    clearTimeout(_refitTimer);
    _refitTimer=setTimeout(function(){
    
      const el=$('termOut');
      if(!el)return;
      const st=getRawState();
      fitTerminalFont(el,st.paneWidth,true);
      el._fitW=el.clientWidth;el._fitPw=st.paneWidth;
      if(!st.userScrolledUp)_setRawScroll(el,el.scrollHeight);
    },180);
  });

  // ── Our own live indicator ──────────────────────────────────────────────────
  // Everything the CLIs animate into the pane is stripped upstream; this is what
  // replaces it. It sits outside the scroll area and updates by writing text into
  // three spans, so a second passing moves nothing in the transcript. The clock
  // runs locally off the last value we parsed, which is why it stays smooth even
  // though we no longer care how often the terminal redraws it.
  function _nowMs(){return (window.performance&&performance.now)?performance.now():new Date().getTime()}
  function _fmtElapsed(sec){
    sec=Math.max(0,Math.floor(sec||0));
    const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;
    if(h)return h+'h '+String(m).padStart(2,'0')+'m';
    if(m)return m+'m '+String(s).padStart(2,'0')+'s';
    return s+'s';
  }
  function _liveSeconds(st,busy){
    const live=st.live||{};
    if(live.sec===null||live.sec===undefined)return null;
    if(!busy)return live.sec;
    return live.sec+Math.max(0,(_nowMs()-(live.at||_nowMs()))/1000);
  }
  let _liveTicker=null;
  function startLiveTicker(){
    if(_liveTicker)return;
    _liveTicker=setInterval(function(){
      const st=getRawState();
      if(!st.live||!st.live.seen||!termBusy())return;
      const e=$('termTime');
      const secs=_liveSeconds(st,true);
      if(e&&secs!==null)e.textContent=_fmtElapsed(secs);
    },1000);
  }
  function updateLiveBar(){
    const bar=$('termLive');
    if(!bar)return;
    startLiveTicker();
    const st=getRawState();
    const live=st.live||{};
    const busy=termBusy();
    const show=busy||!!live.seen;
    bar.classList.toggle('on',show);
    if(!show)return;
    bar.classList.toggle('idle',!busy);
    const set=function(id,txt){
      const e=$(id);
      if(e&&e.textContent!==txt)e.textContent=txt;
    };
    // The verb is the CLI's whimsical spinner word ("Sketching", "Cogitated") and
    // it only means anything while the turn is running. Once it is over the
    // useful fact is how long the turn took, not what it was called.
    const verb=(live.verb||'').replace(/[.…]+$/,'');
    set('termVerb',busy?(verb||'Working'):'Idle');
    const secs=_liveSeconds(st,busy);
    set('termTime',secs===null?'':(busy?'':'last turn ')+_fmtElapsed(secs));
    /*  THE DRAFT'S TOTAL, NOT THE SESSION'S. The number the CLI paints into the pane counts
        the conversation it is in, so restarting the agent takes it back to zero while the
        application has in fact cost everything it cost so far. That reading is worse than no
        reading: it says the work was free. The durable figure comes from the usage ledger,
        which counts every agent this draft has ever had. */
    const total=USAGE&&USAGE.tokens_total
      ?compactTokens(USAGE.tokens_total)+' tokens · '+money(USAGE.usd)
      :(live.tok?((live.dir?live.dir+' ':'')+live.tok+' tokens'):'');
    set('termTok',total);
    const note=$('termNote');
    if(note){
      const txt=busy&&live.esc?'Stop to interrupt':'';
      if(note.textContent!==txt)note.textContent=txt;
    }
  }

  // ── Holding the paint still ─────────────────────────────────────────────────
  // The operators' dashboard has a Freeze button on this row. There is no button
  // here - the simplified terminal has three - but the mechanism it drives is
  // still what stops a repaint tearing a selection out from under the reader, so
  // `st.frozen` and `_pendingRender` stay and this is what renderRawText calls.
  function _frozenNewLines(st){
    if(!st.frozen)return 0;
    return Math.max(0,(st.fullText||'').split('\n').length-(st.frozenAtLines||0));
  }
  function updateFreezeUi(){
    const rawEl=$('termOut');
    if(rawEl)rawEl.classList.toggle('frozen',!!getRawState().frozen);
  }


  // ── reading the pane ────────────────────────────────────────────────────────
  /* A delta poll, not a full capture. The server answers with a full screen, a tail plus a small
     overlap the client splices on, or nothing at all. The overlap is what makes the splice safe:
     if the rows we already hold do not match the rows the tail repeats, the buffer has drifted
     and we resync from a full capture rather than glue two unrelated screens together. */
  function ensureTermScrollTracking(el) {
    if (el._scrollTracked) return;
    el._scrollTracked = true;
    //  Follow-the-tail is decided by WHERE the pane is, never by who scrolled it. A flag that
    //  tried to tell our own scrolls from the reader's threw away any wheel event that landed
    //  between a paint and its scroll event, so a flick during an update did nothing and the
    //  view snapped back to the bottom. Position cannot lie.
    el.addEventListener('scroll', () => {
      TERM.userScrolledUp = (el.scrollHeight - el.scrollTop - el.clientHeight) > 24;
    }, { passive: true });
  }

  async function pollTermDelta() {
    const el = $('termOut');
    if (!el) return;
    ensureTermScrollTracking(el);
    try {
      const query = `?known_lines=${TERM.knownLines}&last_hash=${encodeURIComponent(TERM.visibleHash || '')}`;
      const response = await fetch(`${BASE}/api/drafts/${PID}/terminal/tail${query}`,
        { credentials: 'same-origin', cache: 'no-store' });
      const data = await response.json();
      if (data.usage) paintUsage(data.usage);
      if (typeof data.visible_hash === 'string') TERM.visibleHash = data.visible_hash;
      if (typeof data.pane_width === 'number' && data.pane_width > 0) TERM.paneWidth = data.pane_width;
      if (data.exists === false) {
        if (!TERM.fullText) el.textContent = agentState.available
          ? 'No drafting agent is running on this draft. Press Start above, or just send a '
            + 'message below and one opens.'
          : agentState.reason || 'The drafting agent is not available on this server.';
        return;
      }
      if (data.mode === 'full') {
        TERM.fullText = data.raw || '';
        TERM.knownLines = data.pane_total;
        TERM.firstLoad = false;
        renderRawText();
      } else if (data.mode === 'delta' && data.raw) {
        const arriving = data.raw.split('\n');
        const held = (TERM.fullText || '').split('\n');
        let matched = false;
        if (data.overlap && held.length >= data.overlap) {
          matched = held.slice(-data.overlap).join('\n') === arriving.slice(0, data.overlap).join('\n');
        }
        if (matched) {
          const appended = arriving.slice(data.overlap).join('\n');
          if (appended) TERM.fullText = (TERM.fullText ? TERM.fullText + '\n' : '') + appended;
          TERM.knownLines = data.pane_total;
          renderRawText();
        } else {
          TERM.knownLines = 0;
          const full = await (await fetch(
            `${BASE}/api/drafts/${PID}/terminal/tail?known_lines=0`,
            { credentials: 'same-origin', cache: 'no-store' })).json();
          if (full.mode === 'full') {
            TERM.fullText = full.raw || '';
            TERM.knownLines = full.pane_total;
            if (full.pane_width > 0) TERM.paneWidth = full.pane_width;
            renderRawText();
          }
        }
      }
    } catch (error) { /* a dropped poll is the next poll's problem */ }
  }

  function startTermPolling() {
    if (TERM.polling) return;
    TERM.polling = true;
    pollTermDelta();
    //  Fast for the first six seconds while the CLI's TUI is booting, then once a second. A
    //  drafting turn is minutes long; a 300 ms poll for all of it is a request every three
    //  frames for output that arrives in paragraphs.
    let ticks = 0;
    TERM.timer = setInterval(() => {
      pollTermDelta();
      if (++ticks === 20) {
        clearInterval(TERM.timer);
        TERM.timer = setInterval(pollTermDelta, 1000);
      }
    }, 300);
  }

  async function reloadTerm() {
    Object.assign(TERM, {
      knownLines: 0, userScrolledUp: false, visibleHash: '', firstLoad: true, fullText: '',
      painted: null, _bodyText: null, live: null, frozen: false, frozenAtLines: 0,
    });
    const el = $('termOut');
    if (el) { el.textContent = 'Reading the terminal…'; el._lineMode = false; }
    await pollTermDelta();
  }

  // ── what the agent is doing ─────────────────────────────────────────────────
  const AGENT_LABEL = { busy: 'working', idle: 'ready', stopped: 'stopped', unknown: 'starting' };

  function renderAgentStatus() {
    const pill = $('termStatus');
    if (!pill) return;
    const status = agentState.status || 'unknown';
    pill.className = 'status-pill ' + status;
    pill.innerHTML = '<span class="status-dot"></span><span class="status-label">' +
      esc(AGENT_LABEL[status] || status) + '</span>' +
      (agentState.detail && status !== 'busy'
        ? '<span class="statusdetail"> · ' + esc(agentState.detail) + '</span>' : '');
    const stop = $('termStop');
    if (stop) stop.classList.toggle('visible', status === 'busy');
    //  The same button, named for what it will do. "Restart" over a dead pane is a question
    //  about a session that is not there.
    const restart = $('termRestart');
    if (restart && !restart.disabled) {
      restart.textContent = status === 'stopped' ? 'Start' : 'Restart';
      restart.title = status === 'stopped'
        ? 'Open a drafting agent on this application'
        : 'Kill this agent and start a new one on the published draft';
    }
    updateLiveBar();
  }

  function applyAgentState(next) {
    agentState = Object.assign({}, agentState, next || {});
    renderAgentStatus();
  }

  async function loadAgentState() {
    try {
      applyAgentState(await api(`/api/drafts/${PID}/terminal`));
    } catch (error) { /* the pill keeps whatever it last knew */ }
  }

  // ── typing into it ──────────────────────────────────────────────────────────
  async function sendToAgent(text) {
    const body = String(text || '').trim();
    if (!body) return;
    const input = $('termInput');
    const send = $('termSend');
    if (send) send.disabled = true;
    if (input) { input.value = ''; sizeComposer(); }
    //  Say so straight away. Starting a cold agent takes a few seconds and an unacknowledged
    //  send reads as a lost message, which is what makes people send it twice.
    applyAgentState({ status: 'busy', detail: 'Working' });
    try {
      await api(`/drafts/${PID}/studio/message`, {
        method: 'POST', body: JSON.stringify({ message: body }),
      });
    } catch (error) {
      if (input) input.value = body;
      const hint = $('termHint');
      if (hint) { hint.textContent = error.message; hint.className = 'small bad'; }
      applyAgentState({ status: 'idle', detail: '' });
    } finally {
      if (send) send.disabled = false;
      sizeComposer();
      startTermPolling();
    }
  }

  function sizeComposer() {
    const input = $('termInput');
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = Math.min(180, Math.max(38, input.scrollHeight)) + 'px';
  }

  // ── the three buttons, and the two chips beside them ────────────────────────
  function openChipMenu(anchor, options, current, choose) {
    document.querySelectorAll('.tmenu').forEach((node) => node.remove());
    const menu = document.createElement('div');
    menu.className = 'tmenu';
    menu.innerHTML = options.map((option) =>
      `<button type="button" class="tmenuitem${option.id === current ? ' on' : ''}"
        data-id="${esc(option.id)}">${esc(option.label)}</button>`).join('');
    anchor.parentNode.insertBefore(menu, anchor.nextSibling);
    menu.querySelectorAll('.tmenuitem').forEach((button) => button.addEventListener('click', () => {
      menu.remove();
      choose(button.dataset.id);
    }));
    const away = (event) => {
      if (menu.contains(event.target) || event.target === anchor) return;
      menu.remove();
      document.removeEventListener('mousedown', away);
    };
    setTimeout(() => document.addEventListener('mousedown', away), 0);
  }

  function labelFor(list, id, fallback) {
    const found = (list || []).find((item) => item.id === id);
    return found ? found.label : fallback;
  }

  function renderAgentChips() {
    const model = $('termModel');
    const effort = $('termEffort');
    if (model) {
      model.innerHTML = esc(labelFor(agentState.models, currentModel(), 'Model')) +
        ' <span class="caret">&#9662;</span>';
    }
    if (effort) {
      effort.innerHTML = esc(labelFor(agentState.efforts, currentEffort(), 'Effort')) +
        ' <span class="caret">&#9662;</span>';
    }
  }

  function currentModel() {
    return chosen.model || agentState.model || S.project.draft_model ||
      agentState.default_model || '';
  }
  function currentEffort() {
    return chosen.effort || agentState.effort || agentState.default_effort || '';
  }
  const chosen = { model: '', effort: '' };

  async function switchModel(id) {
    chosen.model = id;
    renderAgentChips();
    try {
      const data = await api(`/drafts/${PID}/terminal/model`, {
        method: 'POST', body: JSON.stringify({ model: id }),
      });
      chosen.model = data.model || id;
      S.project.draft_model = chosen.model;
    } catch (error) {
      chosen.model = '';
      const hint = $('termHint');
      if (hint) { hint.textContent = error.message; hint.className = 'small bad'; }
    }
    renderAgentChips();
  }

  async function switchEffort(id) {
    chosen.effort = id;
    renderAgentChips();
    try {
      await api(`/drafts/${PID}/terminal/effort`, {
        method: 'POST', body: JSON.stringify({ effort: id }),
      });
    } catch (error) {
      chosen.effort = '';
      const hint = $('termHint');
      if (hint) { hint.textContent = error.message; hint.className = 'small bad'; }
    }
    renderAgentChips();
  }

  async function restartAgent() {
    const button = $('termRestart');
    //  Nothing to confirm when there is nothing running: this is "start it", and asking whether
    //  the user is sure they want to begin is a dialog that only ever gets one answer.
    if (agentState.status !== 'stopped' &&
        !window.confirm('Start a new drafting agent on this application?\n\n' +
          'The current one is stopped and everything it has not published is lost. The new agent ' +
          'starts from the published draft with no memory of the old conversation.')) return;
    if (button) { button.disabled = true; button.textContent = 'Starting…'; }
    try {
      applyAgentState(await api(`/drafts/${PID}/terminal/start`, {
        method: 'POST', body: JSON.stringify({ fresh: true }),
      }));
      await reloadTerm();
    } catch (error) {
      const hint = $('termHint');
      if (hint) { hint.textContent = error.message; hint.className = 'small bad'; }
    } finally {
      if (button) { button.disabled = false; renderAgentStatus(); }
    }
  }

  //  A drag on the handle under the terminal, remembered per browser. The studio is a two-column
  //  page and the right height for this pane depends on the screen it is being read on.
  const TERM_HEIGHT_KEY = 'iptorch.termheight';
  function applyTermHeight() {
    const el = $('termOut');
    if (!el) return;
    let stored = 0;
    try { stored = parseInt(localStorage.getItem(TERM_HEIGHT_KEY) || '0', 10); } catch (e) { stored = 0; }
    if (stored >= 140 && stored <= 1400) el.style.height = stored + 'px';
  }
  function startTermResize(event) {
    const el = $('termOut');
    if (!el) return;
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = el.getBoundingClientRect().height;
    const move = (moved) => {
      const height = Math.max(140, Math.min(1400, startHeight + (moved.clientY - startY)));
      el.style.height = height + 'px';
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      try { localStorage.setItem(TERM_HEIGHT_KEY, String(Math.round(
        el.getBoundingClientRect().height))); } catch (e) { /* private mode */ }
      if (!TERM.userScrolledUp) el.scrollTop = el.scrollHeight;
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }

  function wireTerminal() {
    applyTermHeight();
    const form = $('termForm');
    if (form) form.addEventListener('submit', (event) => {
      event.preventDefault();
      sendToAgent($('termInput').value);
    });
    const input = $('termInput');
    if (input) {
      input.addEventListener('input', sizeComposer);
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          sendToAgent(input.value);
        }
      });
    }
    const mic = $('termMic');
    if (mic) mic.addEventListener('click', () => dictateInto(input, mic));
    const stop = $('termStop');
    if (stop) stop.addEventListener('click', async () => {
      try {
        await api(`/drafts/${PID}/terminal/interrupt`, { method: 'POST' });
      } catch (error) { /* the pane shows what happened */ }
      startTermPolling();
    });
    const reload = $('termReload');
    if (reload) reload.addEventListener('click', () => { reloadTerm(); loadAgentState(); });
    const restart = $('termRestart');
    if (restart) restart.addEventListener('click', restartAgent);
    const model = $('termModel');
    if (model) model.addEventListener('click', () => openChipMenu(
      model, agentState.models || [], currentModel(), switchModel));
    const effort = $('termEffort');
    if (effort) effort.addEventListener('click', () => openChipMenu(
      effort, agentState.efforts || [], currentEffort(), switchEffort));
    const handle = $('termResize');
    if (handle) handle.addEventListener('mousedown', startTermResize);
    document.querySelectorAll('.termkeys .chip[data-key]').forEach((button) =>
      button.addEventListener('click', async () => {
        try {
          await api(`/drafts/${PID}/terminal/keys`, {
            method: 'POST', body: JSON.stringify({ keys: [button.dataset.key] }),
          });
        } catch (error) { /* the pane shows what happened */ }
        startTermPolling();
      }));
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

  /* One dictation implementation for the terminal's microphone and for every per-section ask box.
     Describing a change to a claim out loud is faster than typing it, and the text lands in the
     box rather than being sent, so it can be corrected before it goes. */
  function dictateInto(area, button, after) {
    if (!SPEECH || !area || !button) return;
    if (dictateInto.active) { dictateInto.active.stop(); return; }
    const recognition = new SPEECH();
    recognition.lang = document.documentElement.lang || 'en-US';
    recognition.interimResults = true;
    recognition.continuous = true;
    const before = area.value;
    button.classList.add('on');
    recognition.onresult = (event) => {
      let heard = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        heard += event.results[i][0].transcript;
      }
      area.value = (before ? before.replace(/\s*$/, ' ') : '') + heard;
      if (after) after();
    };
    const done = () => {
      button.classList.remove('on');
      dictateInto.active = null;
      if (after) after();
    };
    recognition.onerror = done;
    recognition.onend = done;
    dictateInto.active = recognition;
    recognition.start();
  }

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
     so it can be corrected before it goes. Same implementation as the composer's. */
  function dictate(button) {
    const key = button.dataset.key;
    const area = $('draftBody').querySelector(`.dsecaskinput[data-key="${key}"]`);
    if (!area) return;
    dictateInto(area, button, () => { sectionUI.ask[key] = area.value; grow(area); });
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

  /*  WHICH CLAIMS STAND ALONE, and what the set costs. Marked from the server's reading
      (S.claims), never parsed again here: the review and the fee worksheet bill off that same
      reading, and a second parser in the browser would disagree with them the first time
      somebody writes "A method of using the device of claim 1" - which the Office counts as
      independent even though it names another claim. */
  function claimsHtml(text) {
    const map = (S.claims && S.claims.claims) ? S.claims : null;
    const byNumber = {};
    if (map) map.claims.forEach((c) => { byNumber[c.number] = c; });
    const blocks = String(text || '').split(/\n(?=\s*\d{1,3}\s*[.)]\s)/)
      .filter((c) => c.trim())
      .map((claim) => {
        const body = claim.trim();
        const number = parseInt((body.match(/^\s*(\d{1,3})\s*[.)]/) || [])[1], 10);
        const info = byNumber[number];
        let tag = '';
        if (info && info.independent) {
          tag = `<span class="ctag ind" title="${info.new_subject
            ? 'Names its own subject, so the Office counts it as independent even though it '
              + 'refers to another claim'
            : 'Stands on its own'}">independent${info.new_subject ? ' *' : ''}</span>`;
        } else if (info) {
          tag = `<span class="ctag dep"${info.multiple_dependent
            ? ' title="A multiple dependent claim is counted as the number of claims it refers'
              + ' to (37 CFR 1.75(c)) and carries a surcharge"' : ''}>${
            info.multiple_dependent ? 'multiple dependent' : 'dependent'} on ${
            info.depends_on.join(', ')}</span>`;
        }
        return `<div class="claim${info && info.independent ? ' cind' : ''}">${
          tag}${esc(body).replace(/\n/g, '<br>')}</div>`;
      }).join('');
    return claimsCountsHtml() + blocks;
  }

  function claimsCountsHtml() {
    const m = S.claims;
    if (!m || !m.total) return '';
    const spare = m.free_independent_left;
    const note = m.excess_independent
      ? `<span class="warn">${m.excess_independent} over the ${m.included_independent} included;
          37 CFR 1.16(h) charges for each</span>`
      : spare
        ? `<span class="good">${spare} more independent claim${spare === 1 ? '' : 's'} cost
            nothing: ${m.included_independent} are included in the basic filing fee</span>`
        : `<span class="muted">using all ${m.included_independent} included in the basic filing
            fee</span>`;
    const excess = m.excess_total
      ? ` · <span class="warn">${m.excess_total} claim(s) over the ${m.included_total}
          included</span>` : '';
    return `<div class="claimcounts">
      <span><b>${m.independent}</b> independent</span>
      <span><b>${m.dependent}</b> dependent</span>
      ${m.multiple_dependent ? `<span><b>${m.multiple_dependent}</b> multiple dependent</span>`
        : ''}
      <span class="muted">${m.billable} billed of ${m.total}</span>
      <span class="grow"></span>${note}${excess}</div>`;
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
    const [tone, label] = VERDICT[(qa || {}).verdict] || VERDICT.unknown;
    const checks = (qa || {}).checks || [];
    const findings = (qa || {}).findings || [];
    const order = { fail: 0, warn: 1, pass: 2 };
    checks.sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));
    const count = (status) => checks.filter((c) => c.status === status).length;
    const failed = count('fail');
    const warned = count('warn');
    const outstanding = failed + warned + findings.length;
    /*  Whether the INDEPENDENT reading actually happened. A mechanical-only pass writes
        "the deterministic checks" as its model, and rendering "the reviewer raised nothing"
        under that is a lie by omission: nothing was raised because nothing read it. */
    const readOnlyByCode = !(qa || {}).model_name ||
      /deterministic/i.test((qa || {}).model_name || '');
    body.innerHTML = `
      <div class="rvhead">
        <span class="verdict big ${tone}">${label}</span>
        <div><b>${esc((qa || {}).summary || 'Checked.')}</b>
          <div class="small muted">version ${(qa || {}).version_no || 'current'} · reviewed by
            ${esc((qa || {}).model_name || 'the deterministic checks')}${(qa || {}).last_error ?
              ' · reviewer error: ' + esc(qa.last_error) : ''}</div></div>
        <span class="grow"></span>
        ${outstanding ? `<button type="button" class="btn sm" id="rvFix">Send all
          ${outstanding} to the agent to fix</button>` : ''}
        <button type="button" class="btn ghost sm" id="rvRerun">Re-run the review</button>
      </div>
      <div class="rvcounts">
        <span class="${failed ? 'bad' : 'muted'}"><b>${failed}</b> failed</span>
        <span class="${warned ? 'warn' : 'muted'}"><b>${warned}</b> warned</span>
        <span class="muted"><b>${count('pass')}</b> passed</span>
        <span class="muted"><b>${findings.length}</b> reviewer finding${
          findings.length === 1 ? '' : 's'}</span>
        <span class="grow"></span>
        <span class="small muted" id="rvFixMsg" role="status"></span>
      </div>
      <h4 class="rvsub">Mechanical checks <span class="small muted">decided in code, the same way
        every time. ${checks.length} of them, everything that did not pass is open</span></h4>
      <div class="checklist">${checks.map(checkRow).join('')}</div>
      <h4 class="rvsub">Reviewer findings <span class="small muted">judgements, each with the text
        it is about</span></h4>
      ${findings.length ? findings.map(findingRow).join('') : readOnlyByCode
        ? `<p class="muted small">The independent reviewer has not read this version. Only the
             mechanical checks above ran, so this is not a clean bill: it is silence.
             <b>Re-run the review</b> for the reading.</p>`
        : '<p class="muted small">The reviewer read this version and raised nothing.</p>'}`;
    const rerun = $('rvRerun');
    if (rerun) rerun.addEventListener('click', rerunReview);
    const fix = $('rvFix');
    if (fix) fix.addEventListener('click', sendReviewToAgent);
    //  A finding with a repair chip hands the wording straight to the agent's composer rather
    //  than sending it: the person decides whether that is the change they want.
    body.querySelectorAll('.fixchip').forEach((button) => button.addEventListener('click', () => {
      const input = $('termInput');
      if (!input) return;
      input.value = button.dataset.q || '';
      sizeComposer();
      input.focus();
    }));
    body.querySelectorAll('.openfigrepair').forEach((button) =>
      button.addEventListener('click', () => showPane('figures')));
  }

  /*  Hand the whole review to the agent in one go. Built on the SERVER from the stored report
      rather than scraped out of this page: the text runs to thousands of characters, the items
      are truncated for display, and a fix list assembled from what happens to be on screen is a
      fix list missing whatever was collapsed. */
  async function sendReviewToAgent() {
    const button = $('rvFix');
    const msg = $('rvFixMsg');
    if (button) { button.disabled = true; button.textContent = 'Sending…'; }
    try {
      const out = await api(`/drafts/${PID}/studio/review/fix`, { method: 'POST', body: '{}' });
      if (msg) {
        msg.textContent = `Sent ${out.items} item(s) to the agent.`;
        msg.className = 'small good';
      }
      startTermPolling();
    } catch (error) {
      if (msg) { msg.textContent = error.message; msg.className = 'small bad'; }
      if (button) { button.disabled = false; button.textContent = 'Send to the agent to fix'; }
    }
  }

  function checkRow(check) {
    const tone = { pass: 'good', warn: 'warn', fail: 'bad' }[check.status] || 'muted';
    const advisory = check.severity === 'advisory'
      ? '<span class="chip tiny">heuristic</span>' : '';
    const numeralMismatch = (check.status === 'fail' && (
      check.name === 'Every drawing numeral appears in the specification' ||
      check.name === 'Every specification numeral appears in a drawing'));
    const repair = numeralMismatch ? `<div class="ffix">
      <button type="button" class="chip openfigrepair">Fix a drawing</button>
      <button type="button" class="chip fixchip" data-q="${esc(
        `Resolve ${check.name.toLowerCase()}: ${(check.items || []).join(', ')}. ` +
        'Change the text only when the inventor disclosure supports the visible part; otherwise change the drawing.')}">
        Ask the agent to fix text</button></div>` : '';
    return `<details class="chk ${tone}"${check.status === 'pass' ? '' : ' open'}>
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
  /* NOTHING HERE DRAWS. This product used to generate its sheets, inspect their pixels and gate a
     version on the result; it does not any more, and the difference is not cosmetic. A drawing is
     now a file the applicant made and uploaded, so what this pane owes them is: which sheets the
     specification asks for, which of those they have supplied, and a way to supply the rest.

     The drafting agent still owns the drawing TEXT - the Brief Description, each figure's brief,
     and the numeral table - and it can open every sheet uploaded here, because each one is
     written into its workspace as a PNG. */
  /* A drawing brief used to be a sentence. Now that the agent writes it for a person to draw
     from, it is several hundred words of structured markdown - view type, what the sheet shows,
     the numerals on it, the section indicators - and putting that through `esc` into one muted
     span produced a wall with literal ** in it. Paragraphs, with the bold the agent meant, and
     folded away by default so the pane is a list of sheets rather than six essays. */
  function briefHtml(text) {
    return para(text).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  }

  function figureCard(figure) {
    const src = figure.figure_id
      ? `${BASE}/drafts/${PID}/figures/${figure.figure_id}.png?version=${figure.active_version}`
      : '';
    const caption = String(figure.caption || '').trim();
    return `<article class="figblock${figure.orphan ? ' orphan' : ''}">
      <div class="fighead"><b>${esc(figure.label)}</b></div>
      ${caption ? `<details class="figbrief"><summary>What this sheet must show</summary>
        <div class="figbriefbody">${briefHtml(caption)}</div></details>` : ''}
      ${figure.orphan ? `<div class="small warn">This sheet is no longer described in the
        specification. Either the agent should describe it again, or it should be deleted.</div>` : ''}
      ${src ? `<img class="figimg" loading="lazy" alt="${esc(figure.label)}" src="${src}">`
            : `<div class="fignone">No sheet has been uploaded for this figure yet.</div>`}
      ${(figure.expected_numerals || []).length ?
        `<div class="fignums" title="Numerals this figure's brief says appear on the sheet">${
          figure.expected_numerals.map((n) => `<span class="chip tiny">${esc(n)}</span>`).join('')}</div>` : ''}
      ${figure.n_versions > 1 ? `<div class="figversions"><span class="small muted">Versions</span>${
        (figure.versions || []).map((version) => `<button type="button" class="chip tiny figversion"
          data-figure="${figure.figure_id}" data-version="${version.version_no}"
          ${Number(version.version_no) === Number(figure.active_version) ? 'disabled' : ''}>v${
            version.version_no}${Number(version.version_no) === Number(figure.active_version)
              ? ' current' : ''}</button>`).join('')}
        </div>` : ''}
      <div class="figrow2">
        <label class="btn ghost sm figupload">
          ${figure.uploaded ? 'Replace this sheet' : 'Upload this sheet'}
          <input type="file" accept="image/png,image/jpeg,image/webp" hidden
            data-label="${esc(figure.label)}"
            ${figure.figure_id ? `data-figure="${figure.figure_id}"` : ''}>
        </label>
        ${figure.figure_id ? `<button type="button" class="chip figdel"
          data-figure="${figure.figure_id}">Delete</button>` : ''}
        <span class="small figmsg" role="status"></span>
      </div></article>`;
  }

  function renderFigures() {
    const figures = S.figures || [];
    const missing = figures.filter((figure) => !figure.uploaded).length;
    $('figuresBody').innerHTML = `
      <div class="figintro">
        <div><b>Upload your drawings</b>
          <div class="small muted">PNG, JPEG or WebP, one file per sheet. Each one is stored with
            the draft, offered in the filing package, and put in the drafting agent's workspace so
            it can read the sheet while it writes the Brief Description of the Drawings.</div></div>
        <label class="btn sm figupload">Add a drawing
          <input type="file" id="figAddFile" accept="image/png,image/jpeg,image/webp" hidden></label>
        <span class="small" id="figAddMsg" role="status"></span>
      </div>
      ${figures.length ? `<p class="small muted">${
        missing ? `${missing} of ${figures.length} described sheet(s) still have no drawing.`
                : 'Every sheet the specification describes has a drawing.'}</p>` : ''}
      ${figures.length ? figures.map(figureCard).join('') :
        `<div class="emptypane"><h3>No drawings yet</h3><p>Once the draft describes its figures
         they are listed here, each with a place to upload the sheet. You can also add a drawing
         before the text mentions it, and ask the agent to describe it.</p></div>`}`;
    document.querySelectorAll('.figupload input[type=file]').forEach((input) =>
      input.addEventListener('change', () => uploadFigure(input)));
    document.querySelectorAll('.figdel').forEach((button) =>
      button.addEventListener('click', () => deleteFigure(Number(button.dataset.figure))));
    document.querySelectorAll('.figversion').forEach((button) =>
      button.addEventListener('click', () => activateFigureVersion(button)));
  }

  async function uploadFigure(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    const card = input.closest('.figblock');
    const message = card ? card.querySelector('.figmsg') : $('figAddMsg');
    if (message) { message.className = 'small muted'; message.textContent = 'Uploading…'; }
    const form = new FormData();
    form.append('image', file);
    if (input.dataset.label) form.append('label', input.dataset.label);
    if (input.dataset.figure) form.append('figure_id', input.dataset.figure);
    try {
      const response = await fetch(`${BASE}/drafts/${PID}/studio/figure/upload`, {
        method: 'POST', credentials: 'same-origin', body: form,
        headers: { 'X-CSRF-Token': window.CSRF_TOKEN || '' },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(data.error || 'That drawing could not be stored.');
      await refresh();
    } catch (error) {
      if (message) { message.className = 'small bad'; message.textContent = error.message; }
    } finally {
      input.value = '';
    }
  }

  async function deleteFigure(figureId) {
    if (!window.confirm('Delete this drawing and every version of it?')) return;
    try {
      await api(`/drafts/${PID}/studio/figure/${figureId}/delete`, { method: 'POST' });
      await refresh();
    } catch (error) {
      const message = $('figAddMsg');
      if (message) { message.className = 'small bad'; message.textContent = error.message; }
    }
  }

  async function activateFigureVersion(button) {
    button.disabled = true;
    try {
      await api(`/drafts/${PID}/figures/${button.dataset.figure}/activate`, {
        method: 'POST', body: JSON.stringify({ version_no: Number(button.dataset.version) }),
      });
      await refresh();
    } catch (error) {
      const card = button.closest('.figblock');
      const message = card && card.querySelector('.figmsg');
      if (message) { message.className = 'small bad'; message.textContent = error.message; }
      button.disabled = false;
    }
  }

  // ── research ───────────────────────────────────────────────────────────────
  /* ONE CONTROL WITH AN EFFORT SETTING, and results drawn as the search results they are.

     Three separate buttons used to live in the Sources tab and they read as three names for one
     thing. Two of them could be running at once on the same draft with neither able to see the
     other, both reported in their own vocabulary, and when a result did land nothing on the page
     said what to do with it. This is the product's own search started from the draft: the run has
     a slug, it is in the user's history, it opens as a full report, and the cards below are the
     report's own cards, fetched already rendered from this app's own cards route, so a
     reference in the studio is byte for byte the reference on a report page.

     The one thing this panel must never do is imply a level measured something it did not. Only
     the deepest level charts the claims; the rest rank text. Each run says which it was. */
  /*  WHERE THIS PAGE'S REFERENCE DRAWINGS COME FROM. app.js builds /refdrawing/<pub>/<file>
      off the app base, and on this domain that path belongs to the search app at the root, which
      keeps a different figure directory: every drawing on a studio card was a 404 from an app
      that had never seen the file. This points app.js at the route this app serves. */
  window.REFDRAW_BASE = `${BASE}/api/drafts/${PID}`;

  let RS = null;                 // the /api/drafts/<id>/research payload
  let researchPoll = null;
  let openRun = null;            // the slug whose results are on screen
  let cardPoll = null;
  const RESEARCH_LEVEL_KEY = 'iptorch.researchlevel';

  function researchLevels() { return (RS && RS.levels) || []; }

  function chosenLevel() {
    const levels = researchLevels();
    if (!levels.length) return null;
    const slider = document.getElementById('rsEffort');
    if (slider) return levels[Math.min(levels.length - 1, Math.max(0, +slider.value))];
    let remembered = (RS && RS.default) || '';
    try { remembered = window.localStorage.getItem(RESEARCH_LEVEL_KEY) || remembered; }
    catch (error) { /* private mode */ }
    return levels.find((item) => item.id === remembered) || levels[0];
  }

  async function loadResearch(force) {
    if (RS !== null && !force) return;
    try {
      RS = await api(`/api/drafts/${PID}/research`);
    } catch (error) {
      RS = { levels: [], runs: [], running: false, default: 'find' };
    }
    renderResearch();
    if (RS.running) startResearchPoll();
  }

  function runRow(run) {
    const busy = run.status === 'running';
    const on = openRun === run.slug;
    return `<button type="button" class="rsrun${on ? ' on' : ''}${busy ? ' busy' : ''}"
        data-slug="${esc(run.slug)}" aria-pressed="${on}">
      <span class="rslvl">${esc(run.label)}</span>
      <span class="statuspill status-${esc(run.status)}">${esc(run.status)}</span>
      <span class="small muted rsmsg">${esc(
        busy ? (run.msg || 'searching…') : (run.query_note || ''))}</span>
      <span class="grow"></span>
      ${run.imported_count ? `<span class="small good">${run.imported_count} attached</span>` : ''}
      ${run.redrafted_turn_id ? '<span class="small good">handed to the agent</span>' : ''}
      <span class="small faint">${esc(String(run.created_at).slice(0, 16).replace('T', ' '))}</span>
      <code class="rsid" title="This search's id, saved with your account">${esc(run.slug)}</code>
    </button>`;
  }

  function renderResearch() {
    const box = document.getElementById('researchPanel');
    if (!box) return;
    const levels = researchLevels();
    const runs = (RS && RS.runs) || [];
    const current = chosenLevel();
    const index = current ? levels.findIndex((item) => item.id === current.id) : 0;
    const busy = runs.some((run) => run.status === 'running');
    if (!levels.length) {
      box.innerHTML = '<div class="small muted">Research is not available on this server.</div>';
      return;
    }
    box.innerHTML = `
      <div class="rshead">
        <div class="rstitle">
          <b>Research</b>
          <div class="small muted">Searches the corpus from this draft. Every level is the same
            search the front page runs and each one is saved to your account with its own id,
            listed below newest first.</div>
        </div>
        <div class="rseffort">
          <label class="small muted" for="rsEffort">Effort</label>
          <input type="range" id="rsEffort" min="0" max="${levels.length - 1}" step="1"
                 value="${Math.max(0, index)}" list="rsTicks"
                 aria-label="How hard to look">
          <datalist id="rsTicks">${levels.map((item, n) =>
            `<option value="${n}" label="${esc(item.label)}"></option>`).join('')}</datalist>
          <div class="rsticks">${levels.map((item, n) =>
            `<span class="${n === index ? 'on' : ''}">${esc(item.label)}</span>`).join('')}</div>
        </div>
        <button type="button" class="btn" id="rsRun" ${busy ? 'disabled' : ''}>${
          busy ? 'Searching…' : 'Research'}</button>
      </div>
      <p class="rswhat small" id="rsWhat"><b>${esc(current.label)}</b>
        <span class="muted">${esc(current.eta)}</span> ${esc(current.what)}</p>
      <div class="small bad" id="rsMsg" role="status"></div>
      ${runs.length ? `<div class="rsruns">${runs.map(runRow).join('')}</div>` : `
        <p class="small muted">No research yet on this draft. Pick an effort and press Research;
          you can keep working while it runs.</p>`}
      <div class="rsresults" id="rsResults"></div>`;

    const slider = document.getElementById('rsEffort');
    if (slider) slider.addEventListener('input', () => {
      const picked = levels[Math.min(levels.length - 1, Math.max(0, +slider.value))];
      const what = document.getElementById('rsWhat');
      if (what && picked) {
        what.innerHTML = `<b>${esc(picked.label)}</b> <span class="muted">${esc(picked.eta)}</span>
          ${esc(picked.what)}`;
      }
      document.querySelectorAll('.rsticks span').forEach((node, n) =>
        node.classList.toggle('on', n === +slider.value));
      try { window.localStorage.setItem(RESEARCH_LEVEL_KEY, picked.id); }
      catch (error) { /* private mode */ }
    });
    const run = document.getElementById('rsRun');
    if (run) run.addEventListener('click', startResearch);
    document.querySelectorAll('.rsrun').forEach((button) =>
      button.addEventListener('click', () => showRun(button.dataset.slug)));

    //  Open the newest run that has anything to show, so a finished search is never a row you
    //  have to know to click. A running one is opened too: its partial cards stream in.
    const target = openRun || (runs[0] && runs[0].slug);
    if (target) showRun(target, true);
  }

  async function startResearch() {
    const button = document.getElementById('rsRun');
    const message = document.getElementById('rsMsg');
    const picked = chosenLevel();
    if (!picked) return;
    button.disabled = true;
    button.textContent = 'Starting…';
    message.textContent = '';
    try {
      const data = await api(`/drafts/${PID}/studio/research`, {
        method: 'POST', body: JSON.stringify({ level: picked.id }),
      });
      openRun = data.slug;
      await loadResearch(true);
      startResearchPoll();
    } catch (error) {
      message.textContent = error.message;
      button.disabled = false;
      button.textContent = 'Research';
    }
  }

  function startResearchPoll() {
    if (researchPoll) return;
    researchPoll = setInterval(async () => {
      try { RS = await api(`/api/drafts/${PID}/research`); } catch (error) { return; }
      renderResearch();
      if (!RS.running) { clearInterval(researchPoll); researchPoll = null; refresh(); }
    }, 5000);
  }

  /* THE RESULTS ARE THE REPORT'S OWN CARDS, rendered by the same Jinja macro the report page
     uses, so figures, the drawing badge, the triage flags and the slide-over all work here
     because they are literally the same markup and the same app.js.
     `offset` means the server only ever sends what this panel does not already hold, which is
     what makes a fifteen-minute search deliver its references as it finds them instead of all at
     the end. */
  async function showRun(slug, quiet) {
    const runs = (RS && RS.runs) || [];
    const run = runs.find((item) => item.slug === slug);
    const host = document.getElementById('rsResults');
    if (!run || !host) return;
    const changed = openRun !== slug;
    openRun = slug;
    if (changed || !host.querySelector('.rscards')) {
      host.innerHTML = `
        <div class="rsresulthead">
          <b>${esc(run.label)}</b>
          <span class="small muted">${esc(run.query_note || '')}</span>
          <span class="small ${run.charts ? 'good' : 'muted'}">${run.charts
            ? 'read in full and charted against your claims'
            : run.reads ? 'read in full, not charted'
            : 'ranked by text, nothing read in full'}</span>
          <span class="grow"></span>
          <button type="button" class="btn sm" id="rsRedraft"
            ${run.status === 'complete' ? '' : 'disabled'}>${
            run.redrafted_turn_id ? 'Send again to redraft' : 'Use to redraft'}</button>
        </div>
        <div class="small" id="rsRedraftMsg" role="status"></div>
        <div class="rscards" id="rsCards"></div>
        <div class="small muted rscardnote" id="rsCardNote"></div>`;
      const redraft = document.getElementById('rsRedraft');
      if (redraft) redraft.addEventListener('click', () => useToRedraft(slug));
    }
    await loadCards(slug, changed);
    if (run.status === 'running') startCardPoll(slug);
  }

  async function loadCards(slug, reset) {
    const host = document.getElementById('rsCards');
    const note = document.getElementById('rsCardNote');
    if (!host) return;
    const have = reset ? 0 : host.querySelectorAll('.refcard').length;
    if (reset) host.innerHTML = '';
    let data = null;
    try {
      //  This app's OWN cards route, not /api/cards. That one is served by the search app at
      //  the root of the domain, which keeps a different reports directory and answers a report
      //  this app generated with somebody else's 401.
      const response = await fetch(
        `${BASE}/api/drafts/${PID}/research/${encodeURIComponent(slug)}/cards?offset=${have}`,
        { credentials: 'same-origin', cache: 'no-store' });
      if (response.ok) data = await response.json();
    } catch (error) { return; }
    if (!data) return;
    if (data.cards) {
      const frag = document.createElement('div');
      frag.innerHTML = data.cards;
      const seen = new Set([...host.querySelectorAll('.refcard')].map((c) => c.dataset.pub));
      [...frag.querySelectorAll('.refcard')].forEach((card) => {
        if (seen.has(card.dataset.pub)) return;
        seen.add(card.dataset.pub);
        host.appendChild(card);
        //  app.js owns the card's behaviour; these are its own entry points for a card that
        //  arrived after first paint. Guarded because the studio must still render if the
        //  search bundle ever stops being loaded on this page.
        if (typeof window.bindStreamedCards === 'function') window.bindStreamedCards(card);
      });
      //  app.js runs these itself on the report page, behind a guard that means "this IS the
      //  report". They are what settles a card's drawing: resolveThumbs asks for the ones the
      //  server could not resolve at render time, and recoverBrokenInitialThumbs catches an
      //  <img> that was rendered but does not load. Without them every card here kept its
      //  loading spinner for ever.
      if (typeof window.resolveThumbs === 'function') window.resolveThumbs();
      if (typeof window.recoverBrokenInitialThumbs === 'function') window.recoverBrokenInitialThumbs();
      if (typeof window.resolvePdfLinks === 'function') window.resolvePdfLinks();
    }
    const n = host.querySelectorAll('.refcard').length;
    if (note) {
      note.textContent = n
        ? `${n} reference${n === 1 ? '' : 's'}${data.partial ? ', still searching' : ''}.`
        : (data.partial ? 'Searching. References appear here as they are found.'
                        : 'This search returned nothing from the corpus.');
    }
  }

  function startCardPoll(slug) {
    if (cardPoll) clearInterval(cardPoll);
    cardPoll = setInterval(async () => {
      if (openRun !== slug) { clearInterval(cardPoll); cardPoll = null; return; }
      const run = ((RS && RS.runs) || []).find((item) => item.slug === slug);
      await loadCards(slug, false);
      if (!run || run.status !== 'running') { clearInterval(cardPoll); cardPoll = null; }
    }, 6000);
  }

  async function useToRedraft(slug) {
    const button = document.getElementById('rsRedraft');
    const message = document.getElementById('rsRedraftMsg');
    if (!button) return;
    button.disabled = true;
    button.textContent = 'Handing it over…';
    message.className = 'small muted';
    message.textContent = '';
    try {
      const data = await api(`/drafts/${PID}/studio/research/${slug}/redraft`,
                             { method: 'POST', body: JSON.stringify({}) });
      message.className = 'small good';
      message.textContent = `${data.imported} reference(s) attached and the drafting agent has `
        + 'been asked to work them into the text and the claims. Watch it in the terminal.';
      button.textContent = 'Sent to the agent';
      await loadResearch(true);
      await refresh();
    } catch (error) {
      message.className = 'small bad';
      message.textContent = error.message;
      button.disabled = false;
      button.textContent = 'Use to redraft';
    }
  }

  // ── sources ────────────────────────────────────────────────────────────────
  /* SOURCES IS ABOUT WHAT THIS DRAFT HOLDS, not about looking for more. The three ways to
     search from a draft that used to live at the top of this tab are one control now, in the
     Research panel under the draft, where the results have the width to be read. */
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
      <div class="srcadd">
        <label for="srcFiles">Upload a document</label>
        <div class="srcrow">
          <input type="file" id="srcFiles" accept=".pdf,.docx,.txt" multiple>
        </div>
        <span class="small muted" id="srcUpMsg">A PDF, Word file or plain text. Its text is
          extracted and put in the drafting agent's workspace, where it can read it and cite it.
          This used to live on the paperclip beside the message box; the message box is a terminal
          now, and a terminal is not a place to hand somebody a file.</span>
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
         alone. Add art below, or run Research under the draft and hand what it finds to the
         agent.</p>`}
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
    $('srcFiles').addEventListener('change', (event) => uploadDocuments(event.target));
    $('srcPub').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); addReference(); }
    });
    document.querySelectorAll('.srcdel').forEach((button) =>
      button.addEventListener('click', () => removeReference(button.dataset.pub)));
    document.querySelectorAll('.docdel').forEach((button) =>
      button.addEventListener('click', () => removeDocument(button.dataset.id)));
  }

  async function uploadDocuments(input) {
    const files = Array.from(input.files || []);
    const message = $('srcUpMsg');
    if (!files.length) return;
    message.className = 'small muted';
    message.textContent = `Reading ${files.length} document(s)…`;
    const form = new FormData();
    files.forEach((file) => form.append('files', file));
    form.append('kind', 'prior_art');
    try {
      const response = await fetch(`${BASE}/drafts/${PID}/studio/upload`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRF-Token': window.CSRF_TOKEN || '' }, body: form,
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'That upload could not be read.');
      await refresh();
    } catch (error) {
      message.className = 'small bad';
      message.textContent = error.message;
    } finally {
      input.value = '';
    }
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
  /* Versions, and only versions. This used to carry a second list of "iterations" - one row per
     queued turn of the headless drafting worker, with its token count and its spend. That worker
     no longer drafts: the agent runs in the terminal, where what it is doing is visible while it
     does it, and every published version is already a row here with the note the agent wrote. A
     list that can never grow again is furniture. */
  const ORIGIN_CHIP = {
    manual: 'edited by hand',
    agent: 'published by the drafting agent',
  };

  function renderHistory() {
    const versions = S.versions || [];
    $('historyBody').innerHTML = `
      <h4 class="rvsub">Versions <span class="small muted">every state this application has been
        in, newest first</span></h4>
      ${versions.length ? versions.map((version) => {
        const [tone, label] = VERDICT[version.verdict] || VERDICT.unknown;
        const chip = ORIGIN_CHIP[version.origin] || '';
        return `<div class="srcitem"><div><b>Version ${version.version_no}</b>
          ${chip ? `<span class="chip tiny">${esc(chip)}</span>` : ''}
          <span class="verdict ${tone} tiny">${label}</span>
          <div class="small muted">${esc(version.change_note || '')}${
            version.created_at ? ' · ' + esc(when(version.created_at)) : ''}</div></div>
          <span class="grow"></span>
          <a class="small" href="${BASE}/drafts/${PID}/download/docx?version=${version.version_no}">Word</a>
        </div>`;
      }).join('') : '<p class="muted small">No versions yet. The drafting agent publishes one by ' +
        'running its publish command, and it appears here.</p>'}`;
  }

  // ── tokens and cost ────────────────────────────────────────────────────────
  /* WHAT THIS COSTS, WHILE IT IS COSTING IT. The number rides the terminal's own poll rather
     than a timer of its own: the drafting agent is what spends, and that is the request already
     asking about it. The dollar figure is a metered EQUIVALENT, because the agent runs on a
     subscription; the panel says so rather than leaving somebody to assume it is a bill. */
  let USAGE = null;

  function compactTokens(value) {
    const number = Number(value || 0);
    const units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'k']];
    for (const [limit, suffix] of units) {
      if (Math.abs(number) >= limit) {
        const trimmed = number / limit;
        return (trimmed < 10 ? trimmed.toFixed(1) : Math.round(trimmed)) + suffix;
      }
    }
    return number.toLocaleString();
  }

  function money(value) {
    const number = Number(value || 0);
    return '$' + number.toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: number < 1 ? 4 : 2 });
  }

  function paintUsage(usage) {
    if (!usage) return;
    USAGE = usage;
    const tokens = $('stUsageTokens');
    const cost = $('stUsageCost');
    if (tokens) tokens.textContent = compactTokens(usage.tokens_total);
    if (cost) cost.textContent = money(usage.usd);
    const panel = $('stUsagePanel');
    if (panel && !panel.hidden) paintUsagePanel();
  }

  function paintUsagePanel() {
    const panel = $('stUsagePanel');
    if (!panel || !USAGE) return;
    const row = (name, item) => `<tr><td>${esc(name)}</td>
      <td class="num">${compactTokens(item.tokens_total)}</td>
      <td class="num">${money(item.usd)}</td>
      <td class="num">${Number(item.calls || 0).toLocaleString()}</td></tr>`;
    panel.innerHTML = `
      <table class="usagetable">
        <thead><tr><th>Where it went</th><th class="num">Tokens</th><th class="num">Cost</th>
          <th class="num">Calls</th></tr></thead>
        <tbody>${(USAGE.by_source || []).map((item) => row(item.label, item)).join('')}</tbody>
        <tfoot><tr><td>Total</td>
          <td class="num">${compactTokens(USAGE.tokens_total)}</td>
          <td class="num">${money(USAGE.usd)}</td>
          <td class="num">${Number(USAGE.calls || 0).toLocaleString()}</td></tr></tfoot>
      </table>
      ${(USAGE.by_model || []).length ? `<table class="usagetable">
        <thead><tr><th>Model</th><th class="num">Tokens</th><th class="num">Cost</th>
          <th class="num">Calls</th></tr></thead>
        <tbody>${(USAGE.by_model || []).map((item) =>
          row(item.model || 'unnamed', item)).join('')}</tbody></table>` : ''}
      <p class="small muted">Input ${compactTokens(USAGE.tokens_input)} ·
        output ${compactTokens(USAGE.tokens_output)} ·
        cache written ${compactTokens(USAGE.tokens_cache_write)} ·
        cache read ${compactTokens(USAGE.tokens_cache_read)}.</p>
      <p class="small muted">${esc(USAGE.basis || '')}</p>`;
  }

  function wireUsage() {
    const button = $('stUsage');
    const panel = $('stUsagePanel');
    if (!button || !panel) return;
    button.addEventListener('click', async () => {
      const open = panel.hidden;
      panel.hidden = !open;
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (!open) return;
      paintUsagePanel();
      try {
        const data = await api(`/api/drafts/${PID}/usage?force=1`);
        paintUsage(data.usage);
      } catch (error) { /* the chip keeps the last figure it had */ }
    });
    //  One read on load, so the number is right before the terminal has polled even once.
    api(`/api/drafts/${PID}/usage`).then((data) => paintUsage(data.usage)).catch(() => {});
  }

  // ── filing ─────────────────────────────────────────────────────────────────
  /* THREE THINGS ON ONE TAB, in the order they have to happen: who is filing, what the package
     says when it is built, and what an independent reader made of it. The build is a button
     rather than something this tab does on load, because it reads every uploaded sheet with a
     vision pass and then audits every file it wrote: doing that on a page load would read as a
     broken tab, and doing it on the three-second poll would do it for ever. */
  let FILING = null;
  let FILING_POLL = null;

  async function renderFiling(force) {
    const body = $('filingBody');
    if (FILING === null || force) {
      body.innerHTML = '<p class="muted small">Checking the draft against the filing ' +
        'requirements…</p>';
    }
    let payload;
    try {
      payload = await api(`/api/drafts/${PID}/filing`);
    } catch (error) {
      body.innerHTML = `<div class="emptypane"><h3>Not yet</h3><p>${esc(error.message)}</p></div>`;
      return;
    }
    FILING = payload;
    paintFiling();
    const state = payload.filing || {};
    const busy = state.building || state.qa_running;
    if (busy && !FILING_POLL) {
      FILING_POLL = setInterval(() => {
        if (currentPane() !== 'filing') { clearInterval(FILING_POLL); FILING_POLL = null; return; }
        renderFiling();
      }, 4000);
    } else if (!busy && FILING_POLL) {
      clearInterval(FILING_POLL); FILING_POLL = null;
    }
  }

  function currentPane() {
    const open = document.querySelector('.spane.on');
    return open ? open.id.replace('pane-', '') : '';
  }

  const SEVERITY = { blocker: 'bad', formality: 'warn', note: '' };

  function findingList(items) {
    const order = { blocker: 0, formality: 1, note: 2 };
    return [...items].sort((a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3))
      .map((item) => `<li><b>${esc(item.title)}</b>
        <span class="pillsm ${SEVERITY[item.severity] || ''}">${esc(item.severity)}</span>
        ${esc(item.detail || '')}
        <code>${esc(item.rule || '')} · ${esc(item.where || '')}</code>
        ${item.fix ? `<code>Fix: ${esc(item.fix)}</code>` : ''}
        ${item.evidence ? `<code>Read: ${esc(item.evidence)}</code>` : ''}</li>`).join('');
  }

  function paintFiling() {
    const body = $('filingBody');
    const report = FILING.readiness || {};
    const state = FILING.filing || {};
    const build = state.build || {};
    const qa = state.qa || {};
    const profile = state.profile || {};
    const gaps = (profile.gaps || []);
    const findings = build.findings || [];
    const counts = ['blocker', 'formality', 'note'].map((s) =>
      findings.filter((f) => f.severity === s).length);
    const block = (title, items, tone) => !items || !items.length ? '' :
      `<div class="rdy ${tone}"><h4>${esc(title)}</h4><ul>${items.map((item) =>
        `<li><b>${esc(item.title)}</b> ${esc(item.detail)}${item.items ?
          `<code>${esc(item.items)}</code>` : ''}</li>`).join('')}</ul></div>`;
    const fees = build.fees || report.fees || {};

    body.innerHTML = `
      ${build.built_at ? `
        <div class="rdyhead ${build.ready ? 'good' : 'bad'}">
          <b>${build.ready ? 'The package passed every mechanical filing check'
                           : counts[0] + ' blocker(s) in the built package'}</b>
          <span class="small">${esc(build.verdict || '')}${state.stale ?
            ' · built from version ' + build.version_no + ', the draft is now on ' +
            state.version_no : ''} · ${counts[1]} formality(ies), ${counts[2]} note(s)</span>
        </div>` : `
        <div class="rdyhead">
          <b>No filing package has been built yet</b>
          <span class="small">Building reads every uploaded sheet, reconciles it against the
            specification, writes the specification, drawings, application data sheet,
            declaration, fee worksheet and citation listing, and then audits every file it
            wrote.</span>
        </div>`}

      <div class="rdyactions">
        <button class="btn" id="filingBuild" ${state.building ? 'disabled' : ''}>${
          state.building ? 'Building…' : (build.built_at ? 'Rebuild the package'
                                                         : 'Build the filing package')}</button>
        <button class="btn ghost" id="filingReview" ${
          state.qa_running || state.building || !state.package_available ? 'disabled' : ''}>${
          state.qa_running ? 'The reviewer is reading it…' : 'Run the independent filing review'}
        </button>
        ${state.package_available ?
          `<a class="btn ghost sm" href="${BASE}/drafts/${PID}/download/filing.zip">Download the
             package</a>` : ''}
        <a class="btn ghost sm" href="${esc(state.patent_center_url || '')}" target="_blank"
           rel="noopener">USPTO Patent Center ↗</a>
        <span class="small" id="filingNote"></span>
      </div>
      ${state.qa_available && state.qa_available.ok === false ?
        `<p class="small muted">The independent reviewer cannot run here:
          ${esc(state.qa_available.reason || '')}</p>` : ''}

      ${build.error ? `<div class="rdy bad"><h4>The last build failed</h4>
        <ul><li>${esc(build.error)}</li></ul></div>` : ''}

      ${qa.status === 'complete' ? `
        <div class="rdy ${qa.verdict === 'file_it' ? '' : qa.verdict === 'do_not_file' ?
          'bad' : 'warn'}">
          <h4>Independent filing review: ${esc(qa.verdict || '')}</h4>
          <ul><li><b>${esc(qa.summary || '')}</b>
            <code>${esc(qa.model || '')} · ${Math.round((qa.duration_ms || 0) / 1000)}s</code>
          </li>${findingList(qa.findings || [])}</ul>
          ${(qa.checked || []).length ? `<h4>What it verified and found correct</h4>
            <ul>${(qa.checked || []).map((c) => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}
        </div>` : qa.status === 'failed' ?
        `<div class="rdy warn"><h4>The independent review did not run</h4>
          <ul><li>${esc(qa.error || '')}</li></ul></div>` : ''}

      ${findings.length ? `<div class="rdy ${counts[0] ? 'bad' : 'warn'}">
        <h4>What the mechanical audit found in the package</h4>
        <ul>${findingList(findings)}</ul></div>` : ''}

      ${block('The draft itself', report.blockers, 'bad')}
      ${block('Formalities in the draft', report.formalities, 'warn')}

      <div class="rdy ${gaps.length ? 'bad' : ''}">
        <h4>Who is filing${gaps.length ? `, ${gaps.length} field(s) still empty` : ''}</h4>
        ${gaps.length ? `<ul>${gaps.map((g) =>
          `<li><b>${esc(g.field)}</b> <code>${esc(g.rule)}</code></li>`).join('')}</ul>` :
          '<ul><li>Every field the application data sheet and the declaration need is filled ' +
          'in.</li></ul>'}
        <div class="rdyactions"><button class="btn ghost sm" id="filingParties">${
          gaps.length ? 'Fill them in' : 'Edit the filing details'}</button></div>
        <div id="filingPartiesForm" hidden></div>
      </div>

      ${(build.sheets || []).length ? `<div class="rdy">
        <h4>Drawing sheets in the package</h4>
        <ul>${(build.measurements || []).map((m) => `<li><b>${
          esc(m.label || 'no figure number')}</b> sheet ${esc(m.sheet_number)} ·
          reference characters ${(m.character_cm || 0).toFixed(2)} cm
          <code>37 CFR 1.84(p)(3) sets a floor of 0.32 cm${
            m.character_pixels ? ` · measured at ${m.character_pixels} px on the artwork` :
            ''}</code></li>`).join('')}</ul></div>` : ''}

      <div class="rdy">
        <h4>Claim counts for the fee calculation</h4>
        <ul><li>${fees.total} claims · ${fees.independent} independent ·
          ${fees.multiple_dependent} multiple dependent
          (counted as ${fees.billable} for fees)</li>
          ${(fees.triggered || []).map((t) =>
            `<li>${esc(String(t.quantity))} x ${esc(t.what || t.key || '')}
               <code>fee code ${esc(t.code || '')}</code></li>`).join('')}
          ${(fees.surcharges || []).map((s) => `<li>${esc(s)}</li>`).join('')}
          <li><a href="${esc(state.fee_schedule_url || (fees.fee_schedule_url || ''))}"
             target="_blank" rel="noopener">Current fee schedule ↗</a>. No amounts are printed
             here because they change, and Patent Center totals them from these same counts.</li>
        </ul>
      </div>

      <div class="rdy"><h4>What only a person can do</h4>
        <ul>${(report.remaining || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div>`;

    const buildButton = $('filingBuild');
    if (buildButton) buildButton.addEventListener('click', () => startFilingBuild(false));
    const reviewButton = $('filingReview');
    if (reviewButton) reviewButton.addEventListener('click', startFilingReview);
    const partiesButton = $('filingParties');
    if (partiesButton) partiesButton.addEventListener('click', toggleFilingParties);
  }

  async function startFilingBuild(review) {
    const button = $('filingBuild');
    if (button) { button.disabled = true; button.textContent = 'Building…'; }
    try {
      await api(`/drafts/${PID}/studio/filing/build`, {
        method: 'POST', body: JSON.stringify({ review: !!review }) });
    } catch (error) {
      filingNote(error.message, 'bad');
    }
    renderFiling();
  }

  async function startFilingReview() {
    const button = $('filingReview');
    if (button) { button.disabled = true; button.textContent = 'The reviewer is reading it…'; }
    try {
      await api(`/drafts/${PID}/studio/filing/review`, { method: 'POST', body: '{}' });
    } catch (error) {
      filingNote(error.message, 'bad');
    }
    renderFiling();
  }

  function filingNote(text, tone) {
    const box = document.getElementById('filingNote');
    if (box) { box.textContent = text || ''; box.className = 'small ' + (tone || 'muted'); }
  }

  function toggleFilingParties() {
    const holder = $('filingPartiesForm');
    if (!holder) return;
    if (!holder.hidden) { holder.hidden = true; return; }
    const profile = (FILING.filing || {}).profile || {};
    const values = profile.values || {};
    const field = (key, label, value, required) =>
      `<label class="fset"><span>${esc(label)}${required ? ' *' : ''}</span>
        <input data-filing="${esc(key)}" value="${esc(value || '')}"></label>`;
    holder.innerHTML = `
      <div class="filingform">
        <h5>Inventors (37 CFR 1.76(b)(1), 1.63(b))</h5>
        <div id="filingInventors">${(values.inventors || [{}]).map((row, index) =>
          `<div class="filinginv" data-index="${index}">
            <b>Inventor ${index + 1}</b>
            ${(profile.inventor_fields || []).map((f) =>
              `<label class="fset"><span>${esc(f.label)}${f.required ? ' *' : ''}</span>
                <input data-inv="${index}" data-key="${esc(f.key)}"
                       value="${esc(row[f.key] || '')}"></label>`).join('')}
          </div>`).join('')}</div>
        <button class="btn ghost sm" id="filingAddInventor">Add another inventor</button>
        <h5>Correspondence, applicant and status</h5>
        ${(profile.fields || []).map((f) =>
          field(f.key, f.label, values[f.key], f.required)).join('')}
        <label class="fset"><span>Entity status (37 CFR 1.27, 1.29)</span>
          <select data-filing="entity_status">${(profile.entity_choices || []).map((c) =>
            `<option value="${esc(c.id)}"${c.id === values.entity_status ? ' selected' : ''}>${
              esc(c.label)}</option>`).join('')}</select></label>
        <label class="fset"><span>Application type</span>
          <select data-filing="application_type">${(profile.application_types || []).map((c) =>
            `<option value="${esc(c.id)}"${c.id === values.application_type ? ' selected' : ''}>${
              esc(c.label)}</option>`).join('')}</select></label>
        <div class="rdyactions"><button class="btn" id="filingSaveParties">Save</button>
          <span class="small muted">Every value here is printed on a paper that gets filed.
            Nothing is guessed at: a field left empty is named as missing on the paper that
            needs it.</span></div>
      </div>`;
    holder.hidden = false;
    $('filingAddInventor').addEventListener('click', () => {
      const values2 = collectFilingProfile();
      values2.inventors.push({});
      saveFilingProfile(values2, true);
    });
    $('filingSaveParties').addEventListener('click', () =>
      saveFilingProfile(collectFilingProfile(), false));
  }

  function collectFilingProfile() {
    const out = { inventors: [] };
    document.querySelectorAll('#filingPartiesForm [data-filing]').forEach((input) => {
      out[input.dataset.filing] = input.value;
    });
    document.querySelectorAll('#filingPartiesForm .filinginv').forEach((holder) => {
      const row = {};
      holder.querySelectorAll('[data-key]').forEach((input) => { row[input.dataset.key] = input.value; });
      out.inventors.push(row);
    });
    return out;
  }

  async function saveFilingProfile(values, reopen) {
    try {
      await api(`/drafts/${PID}/studio/filing/profile`, {
        method: 'POST', body: JSON.stringify(values) });
    } catch (error) {
      filingNote(error.message, 'bad');
      return;
    }
    await renderFiling(true);
    if (reopen) toggleFilingParties();
    filingNote('Filing details saved. Rebuild the package to put them on the papers.', 'good');
  }

  // ── settings ───────────────────────────────────────────────────────────────
  /* EVERY CONTROL HERE DOES SOMETHING. The list is short for that reason: a switch that changes
     nothing is worse than no switch, because it teaches you not to trust the rest of them. Each
     field carries its own explanation rather than a tooltip, because these are decisions with
     costs attached and the cost is the part worth reading. */
  let SETTINGS = null;

  async function loadSettings(force) {
    const box = $('settingsBody');
    if (!box) return;
    if (SETTINGS !== null && !force) { renderSettings(); return; }
    box.innerHTML = '<p class="small muted">Reading this project\u2019s settings…</p>';
    try {
      SETTINGS = await api(`/api/drafts/${PID}/settings`);
    } catch (error) {
      box.innerHTML = `<div class="emptypane"><h3>Settings unavailable</h3>
        <p>${esc(error.message)}</p></div>`;
      return;
    }
    renderSettings();
  }

  function settingsField(field) {
    const id = 'set-' + field.key;
    let control;
    if (field.kind === 'model' || field.kind === 'choice') {
      control = `<select id="${id}" data-key="${field.key}">${
        (field.choices || []).map((choice) => `<option value="${esc(choice.id)}"${
          String(choice.id) === String(field.value) ? ' selected' : ''}>${
          esc(choice.label)}</option>`).join('')}</select>`;
    } else if (field.kind === 'int') {
      control = `<input type="number" id="${id}" data-key="${field.key}"
        min="${field.min}" max="${field.max}" value="${esc(field.value)}">`;
    } else {
      control = `<textarea id="${id}" data-key="${field.key}" rows="4"
        maxlength="${field.max_chars || 4000}"
        placeholder="Nothing extra">${esc(field.value || '')}</textarea>`;
    }
    return `<div class="setfield setfield-${field.kind}">
      <label for="${id}"><b>${esc(field.label)}</b></label>
      <p class="small muted">${esc(field.help)}</p>
      ${control}</div>`;
  }

  function renderSettings() {
    const box = $('settingsBody');
    if (!box || !SETTINGS) return;
    box.innerHTML = `<div class="setintro">
        <h3>Advanced settings</h3>
        <p class="small muted">These apply to this project only, and take effect on its next turn.
          A turn already running keeps the settings it started with.</p>
      </div>
      <div class="setgrid">${(SETTINGS.fields || []).map(settingsField).join('')}</div>
      <div class="setactions">
        <button type="button" class="btn sm" id="setSave">Save settings</button>
        <span class="small" id="setMsg" role="status"></span>
      </div>`;
    $('setSave').addEventListener('click', saveSettings);
  }

  async function saveSettings() {
    const button = $('setSave');
    const message = $('setMsg');
    const body = {};
    $('settingsBody').querySelectorAll('[data-key]').forEach((node) => {
      body[node.dataset.key] = node.value;
    });
    button.disabled = true;
    message.className = 'small muted';
    message.textContent = 'Saving…';
    try {
      SETTINGS = await api(`/drafts/${PID}/studio/settings`, {
        method: 'POST', body: JSON.stringify(body),
      });
      renderSettings();
      const saved = $('setMsg');
      saved.className = 'small good';
      saved.textContent = 'Saved. The next turn uses these.';
      //  The model lives in two places on this page; keep them agreed.
      const select = $('stModel');
      if (select) select.value = (SETTINGS.values || {}).draft_model || '';
      S.project.draft_model = (SETTINGS.values || {}).draft_model || '';
    } catch (error) {
      message.className = 'small bad';
      message.textContent = error.message;
    } finally {
      const again = $('setSave');
      if (again) again.disabled = false;
    }
  }

  // ── panes ──────────────────────────────────────────────────────────────────
  /* Settings is a decision you make ABOUT the draft, so it takes the screen rather than sitting
     in the column where the draft is. Opening it does not move you off whatever you were reading. */
  function openSettings(open) {
    const modal = $('setModal');
    if (!modal) return;
    modal.hidden = !open;
    document.body.classList.toggle('modalopen', !!open);
    if (open) { loadSettings(false); }
  }
  const setClose = $('setClose');
  if (setClose) setClose.addEventListener('click', () => openSettings(false));
  const setModal = $('setModal');
  if (setModal) {
    setModal.addEventListener('click', (event) => {
      if (event.target === setModal) openSettings(false);
    });
  }
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && setModal && !setModal.hidden) openSettings(false);
  });

  function showPane(name, updateHash = true) {
    if (name === 'settings') { openSettings(true); return; }
    document.querySelectorAll('.stab').forEach((tab) =>
      tab.classList.toggle('on', tab.dataset.pane === name));
    document.querySelectorAll('.spane').forEach((pane) =>
      pane.classList.toggle('on', pane.id === 'pane-' + name));
    if (name === 'filing') renderFiling();
    setTimeout(renderJump, 0);
    //  Landing on a folded pane opens the fold, or the page would show a pane with nothing on
    //  screen explaining how you got there. Landing on the draft closes it again: collapsed is
    //  the resting state, which is the whole point of folding it.
    openMore(name !== 'draft');
    const targetHash = '#/' + name;
    if (updateHash && location.hash !== targetHash) location.hash = targetHash;
  }
  document.querySelectorAll('.stab').forEach((tab) =>
    tab.addEventListener('click', () => showPane(tab.dataset.pane)));

  function openMore(open) {
    const more = $('studioMore');
    const button = $('stabMore');
    if (!more || !button) return;
    more.hidden = !open;
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    button.classList.toggle('on', !!open);
  }
  /* Folding the agent away gives the application the whole page, which is what you want the
     moment you are reading rather than instructing. Remembered, because a person who wants the
     page for the draft wants it for more than one reload. */
  //  A NEW KEY, ON PURPOSE. Until today a collapsed panel was two pixels tall with the control
  //  that reopens it clipped out of sight, so nobody who is in that state chose it knowing what
  //  it did, and there was no way back from inside the page. Reading a different key discards
  //  every remembered fold exactly once; from here the rail says what it is, so it can be kept.
  const CHAT_FOLD_KEY = 'iptorch.chatfold2';
  try { window.localStorage.removeItem('iptorch.chatfold'); } catch (error) { /* private mode */ }
  function foldChat(folded) {
    document.querySelector('.studio').classList.toggle('chathidden', !!folded);
    const button = $('chatFold');
    if (button) {
      button.setAttribute('aria-expanded', folded ? 'false' : 'true');
      button.title = folded ? 'Show the drafting agent' : 'Hide the drafting agent';
      button.setAttribute('aria-label', button.title);
    }
    //  The renderer measures the pane to fit its type, so a fold is a resize.
    if (!folded) setTimeout(() => renderRawText(true), 0);
    try { window.localStorage.setItem(CHAT_FOLD_KEY, folded ? '1' : '0'); } catch (error) { /* private mode */ }
  }
  const chatFold = $('chatFold');
  if (chatFold) {
    chatFold.addEventListener('click', (event) => {
      event.stopPropagation();
      foldChat(!document.querySelector('.studio').classList.contains('chathidden'));
    });
    //  While it is collapsed the whole spine opens it, not just the chevron on it. A 44px strip
    //  is a hard target to find on purpose and an impossible one to find by accident, and the
    //  fold outlives the session that set it.
    const spine = document.querySelector('.studioterm');
    if (spine) spine.addEventListener('click', () => {
      if (document.querySelector('.studio').classList.contains('chathidden')) foldChat(false);
    });
    let remembered = '0';
    try { remembered = window.localStorage.getItem(CHAT_FOLD_KEY) || '0'; } catch (error) { /* private mode */ }
    if (remembered === '1') foldChat(true);
  }

  const moreButton = $('stabMore');
  if (moreButton) {
    moreButton.addEventListener('click', () => openMore($('studioMore').hidden));
  }

  /* THE ROW ABOVE THE APPLICATION IS FOR GETTING AROUND THE APPLICATION. A patent is long and the
     part you want is almost never at the top; the tabs that used to sit here were about the draft
     rather than in it, so they fold away and this takes their place. */
  function renderJump() {
    const bar = $('secJump');
    if (!bar) return;
    const sections = S.sections || [];
    const onDraft = document.querySelector('.stab[data-pane="draft"]').classList.contains('on');
    bar.hidden = !onDraft || !sections.length || !S.version;
    if (bar.hidden) { bar.innerHTML = ''; return; }
    //  The two boilerplate headings say "Not applicable." and are folded away in the draft; a
    //  chip that jumps to them spends width on nothing.
    const jumpable = sections.filter((section) => !BOILERPLATE.includes(section.key));
    bar.innerHTML = jumpable.map((section) =>
      `<button type="button" class="jumpto" data-key="${section.key}">${
        esc(shortHeading(section.heading))}</button>`).join('');
    bar.querySelectorAll('.jumpto').forEach((button) => button.addEventListener('click', () => {
      const node = document.getElementById('sec-' + button.dataset.key);
      if (!node) return;
      //  A folded boilerplate section has to be opened before it can be scrolled to, or the click
      //  silently does nothing.
      const fold = node.closest('.dboiler');
      if (fold && !fold.open) { fold.open = true; sectionUI.boilerOpen = true; }
      node.scrollIntoView({ behavior: 'smooth', block: 'start' });
      bar.querySelectorAll('.jumpto').forEach((item) => item.classList.remove('on'));
      button.classList.add('on');
    }));
  }

  //  The filing headings are the right words in the document and far too long for a chip.
  const SHORT = {
    'Cross-Reference to Related Applications': 'Cross-reference',
    'Statement Regarding Federally Sponsored Research or Development': 'Federally sponsored',
    'Field of the Disclosure': 'Field',
    'Brief Description of the Drawings': 'Drawing descriptions',
    'Detailed Description': 'Detailed description',
  };
  function shortHeading(heading) {
    return SHORT[heading] || heading;
  }
  $('stFileBtn').addEventListener('click', () => showPane('filing'));

  function routeFromHash() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    const pane = ['draft', 'review', 'figures', 'sources', 'history', 'filing']
      .includes(parts[0]) ? parts[0] : 'draft';
    showPane(pane, false);
  }
  window.addEventListener('hashchange', routeFromHash);

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

  function renderChrome() {
    $('stStatus').textContent = S.project.status;
    $('stStatus').className = 'statuspill status-' + S.project.status;
    //  No version number on the page above the application. Every revision is the one draft
    //  that would be filed today; the numbering is bookkeeping and lives in History.
    $('stVersion').textContent = S.project.latest_version_no ? '' : 'no draft yet';
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
    const missing = (S.figures || []).filter((figure) => !figure.uploaded).length;
    $('tabFigures').textContent = (S.figures || []).length || '';
    $('tabFigures').title = missing
      ? `${missing} described figure(s) have no drawing uploaded yet` : '';
    //  One number on the fold, so nothing important disappears just because it is folded.
    const badge = $('tabMoreBadge');
    if (badge) {
      badge.textContent = bad ? bad : '';
      badge.className = 'tabbadge' + (bad ? ' bad' : '');
    }
  }

  function renderAll() {
    renderChrome();
    renderJump();
    renderDraft();
    renderReview();
    renderSources();
    renderFigures();
    renderHistory();
    renderAgentChips();
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

  /* Poll the RECORD, not the terminal: the terminal has its own one-second poll and its own
     append-only renderer. This one is the cheap three-second read that notices a new version, a
     new review, or the agent's own state changing, and re-fetches the whole studio only when
     something has actually moved. It never stops while an agent is alive, because the agent
     publishes on its own schedule and the Draft tab has to follow it. */
  function startPolling() {
    if (polling) return;
    let idle = 0;
    polling = setInterval(async () => {
      let state;
      try {
        state = await api(`/api/drafts/${PID}/studio/poll`);
      } catch (error) { return; }
      if (state.agent) applyAgentState(state.agent);
      const changed = state.latest_version_no !== S.project.latest_version_no ||
        (state.qa && (!S.qa || state.qa.id !== S.qa.id));
      if (changed) { await refresh(); idle = 0; }
      reviewing = !!state.reviewing;
      const working = reviewing || (state.agent || {}).status === 'busy';
      if (!working) {
        idle += 1;
        //  Ten minutes of a quiet agent before the page stops asking. Long, deliberately: the
        //  agent is a person's collaborator and can be given work from another tab, and the cost
        //  of this poll is one small query.
        if (idle > 200) { clearInterval(polling); polling = null; }
      } else idle = 0;
    }, 3000);
  }

  wireTerminal();
  wireUsage();
  //  Paint the agent from the state the page was SERVED with, before asking the server again.
  //  Without this the status pill keeps the template's placeholder until the first poll lands,
  //  so a page opened on a working agent reads "starting" for a second and a page opened on a
  //  dead one reads "starting" until something else happens to update it.
  applyAgentState(S.agent);
  renderAll();
  routeFromHash();
  loadAgentState();
  //  Research is its own record with its own poll: it survives a page the draft has not changed
  //  on, and a search that runs for twenty minutes must not depend on the studio poll noticing.
  loadResearch(false);
  startTermPolling();
  startPolling();
})();
