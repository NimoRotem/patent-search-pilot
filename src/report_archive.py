"""Automatic top-50 full-text Markdown archive for every completed search.

The report page remains capped for speed.  This worker takes its authoritative listwise-ranked
head, appends the retrieval-ranked tail to fifty unique publications, resolves complete text from
Postgres/Mongo/Google Patents, and writes one Markdown file per publication plus a manifest into a
ZIP.  Work is proactive and single-wide: it starts when a report completes or an older report is
opened, never from a blocking download request.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import threading
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

import db
import enrich_display
import mongo_corpus
import pubnorm
import webview
from config import DATA


ARCHIVE_DIR = DATA / "reports" / "archives"
TEXT_CACHE = DATA / "archive_text_cache"
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "https://rotem.ai/patents").rstrip("/")
TOP_N = max(1, min(int(os.environ.get("ARCHIVE_TOP_N", "50")), 50))
ARCHIVE_FORMAT_VERSION = 2
_POOL = ThreadPoolExecutor(max_workers=max(1, int(os.environ.get("ARCHIVE_WORKERS", "1"))),
                           thread_name_prefix="patent-archive")
_LOCK = threading.Lock()
_FUTURES = {}
_STATE = {}
_STOP = threading.Event()


class _ArchiveInterrupted(RuntimeError):
    """A graceful web-worker shutdown interrupted an archive between patent files."""


def _signature(report):
    payload = {
        "archive_format": ARCHIVE_FORMAT_VERSION,
        "query": report.get("query"),
        "mode": report.get("mode"),
        "ranked": (report.get("ranked_families") or [])[:160],
        "fed": [h.get("pub") for h in ((report.get("federation") or {}).get("hits") or [])[:80]],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _join_key(pub):
    return re.sub(r"[^A-Za-z0-9]", "", str(pub or "")).upper()


def _state_path(slug, reports_dir):
    return Path(reports_dir) / f"{slug}.archive.json"


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp", delete=False) as f:
        tmp = Path(f.name)
        json.dump(payload, f, default=str, indent=1)
    os.replace(tmp, path)


def _set_state(slug, reports_dir, **values):
    with _LOCK:
        state = dict(_STATE.get(slug) or {})
        state.update(values)
        state["slug"] = slug
        _STATE[slug] = state
    try:
        _atomic_json(_state_path(slug, reports_dir), state)
    except Exception:
        pass
    return state


def status(slug, reports_dir):
    with _LOCK:
        memory = dict(_STATE.get(slug) or {})
    if memory:
        return memory
    p = _state_path(slug, reports_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"slug": slug, "status": "not_started", "ready": False, "top_n": TOP_N}


def metadata(slug, reports_dir):
    st = status(slug, reports_dir)
    return {k: st.get(k) for k in (
        "status", "ready", "top_n", "n_patents", "n_full_text", "n_missing_text",
        "missing_publications", "generated_at", "download_name", "error")
            if st.get(k) is not None}


def ensure(slug, report, final_view, reports_dir):
    """Schedule the archive if the current report signature is not already ready/running."""
    if _STOP.is_set():
        return {"slug": slug, "status": "interrupted", "ready": False, "top_n": TOP_N,
                "message": "Archive will resume after the service restart."}
    if not report or report.get("partial"):
        return {"status": "waiting", "ready": False, "top_n": TOP_N}
    sig = _signature(report)
    current = status(slug, reports_dir)
    if current.get("signature") == sig and current.get("ready"):
        archive = ARCHIVE_DIR / str(current.get("archive_file") or "")
        if archive.is_file():
            return current
    with _LOCK:
        fut = _FUTURES.get(slug)
        if fut and not fut.done():
            return dict(_STATE.get(slug) or current)
        state = {"slug": slug, "status": "queued", "ready": False, "top_n": TOP_N,
                 "signature": sig, "started_at": datetime.now(timezone.utc).isoformat()}
        _STATE[slug] = state
        _FUTURES[slug] = _POOL.submit(_build, slug, report, final_view, Path(reports_dir), sig)
    _atomic_json(_state_path(slug, reports_dir), state)
    return state


def archive_path(slug, reports_dir):
    st = status(slug, reports_dir)
    if not st.get("ready") or not st.get("archive_file"):
        return None
    path = ARCHIVE_DIR / st["archive_file"]
    return path if path.is_file() else None


def _candidate_cards(report, final_view):
    """Authoritative listwise head first, then the untouched retrieval-ranked tail."""
    cards, seen = [], set()

    def add(source):
        for card in source or []:
            pub = card.get("pub")
            key = _join_key(pub) if pub else ""
            if not pub or not key or key in seen:
                continue
            seen.add(key)
            cards.append(dict(card))
            if len(cards) >= TOP_N:
                return True
        return False

    if add((final_view or {}).get("cards")):
        return cards
    wide = webview.build_view(report, top_n=TOP_N)
    try:
        webview.mongo_enrich_cards(wide.get("cards") or [])
    except Exception:
        pass
    # The interactive report defines the authoritative listwise head (currently 25). Beyond that
    # depth no listwise order exists, so define the archive tail explicitly by the same per-card
    # semantic relevancy used by the UI. This also lets a strong federated-only hit compete for
    # ranks 26-50 instead of being blindly appended behind fifty local rows.
    tail = list(wide.get("cards") or [])
    tail.sort(key=lambda c: (float(c.get("relevancy_score") if c.get("relevancy_score") is not None
                                  else c.get("relevancy") or 0),
                             float(c.get("match_score") or 0), int(c.get("n_covers") or 0)),
              reverse=True)
    add(tail)
    for i, card in enumerate(cards, 1):
        card["archive_rank"] = i
    return cards


def _local_record(cur, pub):
    keys = list(dict.fromkeys(_join_key(v) for v in pubnorm.variants(pub) if _join_key(v)))
    if not keys:
        keys = [_join_key(pub)]
    cur.execute(
        "SELECT * FROM publications WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g'))=ANY(%s) "
        "ORDER BY (title IS NOT NULL) DESC,(abstract IS NOT NULL) DESC LIMIT 1", (keys,))
    p = cur.fetchone()
    if not p:
        return None
    pid = p["id"]
    cur.execute("SELECT claim_no,is_independent,lang,text,resolved_text FROM claims "
                "WHERE publication_id=%s ORDER BY claim_no,id", (pid,))
    claims = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT para_no,heading,page_no,lang,text FROM paragraphs "
                "WHERE publication_id=%s ORDER BY id", (pid,))
    paragraphs = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT figure_no,caption,reference_numbers FROM figures "
                "WHERE publication_id=%s ORDER BY id", (pid,))
    figures = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT scheme,symbol,version_date,is_first FROM classifications "
                "WHERE publication_id=%s ORDER BY is_first DESC,symbol", (pid,))
    classifications = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT role,raw_name,normalized_name FROM parties WHERE publication_id=%s "
                "ORDER BY role,raw_name", (pid,))
    parties = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT dst_pub,category,origin,search_phase,occurrences FROM citations "
                "WHERE src_pub=%s ORDER BY occurrences DESC,dst_pub", (p["publication_number"],))
    citations = [dict(r) for r in cur.fetchall()]
    return {"publication": dict(p), "claims": claims, "paragraphs": paragraphs,
            "figures": figures, "classifications": classifications, "parties": parties,
            "citations": citations, "sources": ["local Postgres corpus"]}


class _GoogleTextParser(HTMLParser):
    """Small tolerant extractor for Google Patents' itemprop sections; no new dependency."""
    BLOCKS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.claims = []
        self.description = []
        self.abstract = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        section = None
        item = (a.get("itemprop") or "").lower()
        classes = set((a.get("class") or "").lower().split())
        if item in ("claims", "description", "abstract"):
            section = item
        elif "claims" in classes:
            section = "claims"
        elif "abstract" in classes:
            section = "abstract"
        self.stack.append(section or (self.stack[-1] if self.stack else None))
        if self.stack[-1] and tag in self.BLOCKS:
            getattr(self, self.stack[-1]).append("\n")

    def handle_startendtag(self, tag, attrs):
        if self.stack and self.stack[-1] and tag in self.BLOCKS:
            getattr(self, self.stack[-1]).append("\n")

    def handle_endtag(self, tag):
        if self.stack:
            section = self.stack[-1]
            if section and tag in self.BLOCKS:
                getattr(self, section).append("\n")
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1]:
            getattr(self, self.stack[-1]).append(data)

    @staticmethod
    def clean(parts):
        text = html.unescape("".join(parts))
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        out = []
        for line in lines:
            if line and (not out or out[-1] != line):
                out.append(line)
        return out


def _google_cache_path(pub):
    canon = pubnorm.canonical(pub)
    if not canon:
        return None
    return TEXT_CACHE / f"{canon}.json"


def _google_full_text(pub):
    cache = _google_cache_path(pub)
    result = {"claims": [], "description": [], "abstract": []}
    if cache and cache.exists():
        try:
            saved = json.loads(cache.read_text())
            for key in result:
                if isinstance(saved.get(key), list):
                    result[key] = saved[key]
            # A complete cached specification is immutable.  An abstract-only or claims-only
            # response is not: Google occasionally serves a transient consent/throttle shell,
            # and the old worker cached that partial response forever.
            if result["claims"] and result["description"]:
                return result
        except Exception:
            pass

    urls = []
    primary = pubnorm.google_url(pub)
    if primary:
        urls.append(primary)
    # A few upstream providers drop the leading zero from US pre-grant serials.  Google normally
    # accepts the padded form, but trying the remaining normalized spellings makes the archive
    # resilient to older report/cache records without turning this into a broad web search.
    for candidate in pubnorm.mongo_candidates(pub):
        compact = re.sub(r"[^A-Za-z0-9]", "", candidate).upper()
        parsed = pubnorm.parse(compact)
        if not parsed or not parsed[2]:
            continue
        url = f"https://patents.google.com/patent/{compact}/en"
        if url not in urls:
            urls.append(url)
        if len(urls) >= 3:
            break

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; rotemAI patent research archive/2.0)",
        "Accept-Language": "en-US,en;q=0.8",
    }
    for url_index, url in enumerate(urls):
        attempts = 3 if url_index == 0 else 1
        for attempt in range(attempts):
            if _STOP.is_set():
                raise _ArchiveInterrupted()
            response = None
            try:
                response = requests.get(url, headers=headers, timeout=30)
                if response.status_code == 200 and len(response.text) < 25_000_000:
                    parser = _GoogleTextParser()
                    parser.feed(response.text)
                    fetched = {"claims": parser.clean(parser.claims),
                               "description": parser.clean(parser.description),
                               "abstract": parser.clean(parser.abstract)}
                    for key, values in fetched.items():
                        if values and not result[key]:
                            result[key] = values
                    if result["claims"] and result["description"]:
                        break
                if response.status_code not in (408, 425, 429, 500, 502, 503, 504):
                    break
            except requests.RequestException:
                pass
            if attempt + 1 < attempts and _STOP.wait(1.0 + attempt):
                raise _ArchiveInterrupted()
        if result["claims"] and result["description"]:
            break
    if cache and any(result.values()):
        try:
            _atomic_json(cache, result)
        except Exception:
            pass
    return result


def _image_urls(card, display):
    out = []
    for image in (card.get("images") or []) + (display.get("images") or []):
        if not isinstance(image, dict):
            continue
        url = image.get("full") or image.get("src_url") or image.get("thumbnail")
        if not url and image.get("file"):
            url = (f"{PUBLIC_BASE_URL}/figures/{quote(card['pub'], safe='')}/"
                   f"{quote(str(image['file']), safe='')}")
        if not url:
            continue
        parsed = urlparse(str(url))
        if parsed.scheme not in ("http", "https"):
            continue
        if url not in out:
            out.append(url)
    return out[:80]


def _merge_record(cur, card):
    pub = card["pub"]
    local = _local_record(cur, pub)
    p = dict((local or {}).get("publication") or {})
    claims = list((local or {}).get("claims") or [])
    paragraphs = list((local or {}).get("paragraphs") or [])
    figures = list((local or {}).get("figures") or [])
    classes = list((local or {}).get("classifications") or [])
    parties = list((local or {}).get("parties") or [])
    citations = list((local or {}).get("citations") or [])
    sources = list((local or {}).get("sources") or [])

    md = None
    try:
        md = mongo_corpus.get_detail(pub)
    except Exception:
        pass
    display = {}
    if not claims or not paragraphs or not card.get("images"):
        try:
            display = enrich_display.enrich_for_display(pub) or {}
        except Exception:
            display = {}
    if md:
        sources.append("lemad keyed patent corpus")
    if display:
        sources.append(display.get("source") or "patent detail recovery")

    raw_claims = (md or {}).get("claims") or display.get("claims") or []
    if not claims:
        claims = [{"claim_no": i + 1, "is_independent": None, "lang": (md or {}).get("lang"),
                   "text": text, "resolved_text": None}
                  for i, text in enumerate(raw_claims) if text]
    raw_desc = (md or {}).get("description") or display.get("description") or []
    if not paragraphs:
        paragraphs = [{"para_no": None, "heading": None, "page_no": None,
                       "lang": (md or {}).get("lang"), "text": text}
                      for text in raw_desc if text]

    # The local normalized corpus is complete when present.  For external/thin hits, Google
    # Patents is the final full-document fallback rather than silently labelling a stub as full.
    if not claims or not paragraphs:
        gp = _google_full_text(pub)
        if gp.get("claims") and not claims:
            claims = [{"claim_no": i + 1, "is_independent": None, "lang": None,
                       "text": text, "resolved_text": None}
                      for i, text in enumerate(gp["claims"]) if text]
        if gp.get("description") and not paragraphs:
            paragraphs = [{"para_no": None, "heading": None, "page_no": None, "lang": None,
                           "text": text} for text in gp["description"] if text]
        if any(gp.values()):
            sources.append("Google Patents full-document page")
        if not p.get("abstract") and gp.get("abstract"):
            p["abstract"] = "\n".join(gp["abstract"])

    # Google does not consistently expose every EP/WO specification.  OPS is the issuing-office
    # source for those records, so use its bounded, quota-aware text endpoints only for fields
    # that are still absent.  National records are deliberately excluded because OPS documents
    # that their full-text endpoints return 404 for those jurisdictions.
    if (not claims or not paragraphs) and str(pub).upper()[:2] in ("EP", "WO"):
        try:
            import ops
            if ops.have_creds():
                wanted = tuple(name for name, missing in (
                    ("claims", not claims), ("description", not paragraphs)) if missing)
                recovered = ops.ops_fetch(pub, want=wanted)
                if not claims:
                    claims = [{"claim_no": c.get("claim_no") or i + 1,
                               "is_independent": None,
                               "lang": recovered.get("claims_lang"),
                               "text": c.get("text"), "resolved_text": None}
                              for i, c in enumerate(recovered.get("claims") or [])
                              if c.get("text")]
                if not paragraphs:
                    paragraphs = [{"para_no": para.get("para_no"),
                                   "heading": para.get("heading"), "page_no": None,
                                   "lang": recovered.get("desc_lang"),
                                   "text": para.get("text")}
                                  for para in (recovered.get("paragraphs") or [])
                                  if para.get("text")]
                if recovered.get("claims") or recovered.get("paragraphs"):
                    sources.append("EPO OPS official full-text service")
        except Exception:
            pass

    for key in ("title", "abstract", "country", "publication_date", "filing_date",
                "earliest_priority_date", "simple_family_id"):
        if not p.get(key):
            p[key] = card.get(key) or (md or {}).get(key) or display.get(key)
    p["publication_number"] = p.get("publication_number") or pub
    if not classes:
        classes = (md or {}).get("classifications") or display.get("classifications") or card.get("cpc") or []
    if not parties:
        for role, values in (("assignee", card.get("assignees") or (md or {}).get("assignees") or []),
                             ("inventor", card.get("inventors") or (md or {}).get("inventors") or [])):
            parties.extend({"role": role, "raw_name": value, "normalized_name": None}
                           for value in values if value)

    return {"publication": p, "claims": claims, "paragraphs": paragraphs,
            "figures": figures, "classifications": classes, "parties": parties,
            "citations": citations, "images": _image_urls(card, display),
            "sources": list(dict.fromkeys(s for s in sources if s)),
            "full_text": bool(claims and paragraphs)}


def _md_escape(value):
    return str(value or "").replace("|", "\\|")


def _markdown(card, record):
    p = record["publication"]
    pub = p.get("publication_number") or card["pub"]
    title = p.get("title") or card.get("title") or "Untitled patent publication"
    google = pubnorm.google_url(pub)
    espacenet = pubnorm.espacenet_url(pub, p.get("simple_family_id"))
    lines = [f"# {card['archive_rank']:02d}. {title}", "",
             f"- **Archive rank:** {card['archive_rank']} of {TOP_N}",
             f"- **Publication:** [{pub}]({google})"]
    if espacenet:
        lines.append(f"- **Espacenet:** [open record]({espacenet})")
    if p.get("publication_date"):
        lines.append(f"- **Publication date:** {_md_escape(p.get('publication_date'))}")
    if p.get("filing_date"):
        lines.append(f"- **Filing date:** {_md_escape(p.get('filing_date'))}")
    if p.get("earliest_priority_date"):
        lines.append(f"- **Earliest priority:** {_md_escape(p.get('earliest_priority_date'))}")
    if p.get("simple_family_id"):
        lines.append(f"- **Family:** {_md_escape(p.get('simple_family_id'))}")
    lines.append(f"- **Text status:** {'claims and description captured' if record['full_text'] else 'source text incomplete — use the office links above'}")
    if record["sources"]:
        lines.append(f"- **Sources used:** {', '.join(record['sources'])}")
    lines.append("")

    assignees = [x.get("raw_name") or x.get("normalized_name") for x in record["parties"]
                 if str(x.get("role") or "").lower() in ("assignee", "applicant")]
    inventors = [x.get("raw_name") or x.get("normalized_name") for x in record["parties"]
                 if str(x.get("role") or "").lower() == "inventor"]
    if assignees or inventors:
        lines.extend(["## Parties", ""])
        if assignees:
            lines.append("- **Applicant / assignee:** " + "; ".join(dict.fromkeys(x for x in assignees if x)))
        if inventors:
            lines.append("- **Inventors:** " + "; ".join(dict.fromkeys(x for x in inventors if x)))
        lines.append("")

    lines.extend(["## Abstract", "", str(p.get("abstract") or "_No abstract was available from the queried sources._"), ""])

    if record["images"]:
        lines.extend(["## Sketches and drawings", ""])
        for i, url in enumerate(record["images"], 1):
            lines.extend([f"[Sketch {i}]({url})", "", f"![Sketch {i} for {pub}]({url})", ""])
    else:
        lines.extend(["## Sketches and drawings", "", "_No digitized sketch link was returned._", ""])

    lines.extend(["## Claims", ""])
    if record["claims"]:
        for i, claim in enumerate(record["claims"], 1):
            no = claim.get("claim_no") or i
            suffix = " (independent)" if claim.get("is_independent") else ""
            lines.extend([f"### Claim {no}{suffix}", "", str(claim.get("text") or claim.get("resolved_text") or "").strip(), ""])
    else:
        lines.extend(["_Claims were not available from the queried sources._", ""])

    lines.extend(["## Full description", ""])
    if record["paragraphs"]:
        last_heading = None
        for paragraph in record["paragraphs"]:
            heading = (paragraph.get("heading") or "").strip()
            if heading and heading != last_heading:
                lines.extend([f"### {heading}", ""])
                last_heading = heading
            coord = paragraph.get("para_no")
            if coord:
                lines.append(f"**[{coord}]**")
                lines.append("")
            lines.extend([str(paragraph.get("text") or "").strip(), ""])
    else:
        lines.extend(["_The description was not available from the queried sources._", ""])

    if record["figures"]:
        lines.extend(["## Figure captions", ""])
        for fig in record["figures"]:
            lines.append(f"- **Figure {_md_escape(fig.get('figure_no') or '?')}:** {_md_escape(fig.get('caption'))}")
        lines.append("")
    if record["classifications"]:
        lines.extend(["## Classifications", ""])
        for item in record["classifications"]:
            code = item.get("symbol") or item.get("code") or item.get("cpc")
            if code:
                lines.append(f"- {_md_escape(code)}")
        lines.append("")
    if record["citations"]:
        lines.extend(["## Cited patent documents", ""])
        for cite in record["citations"][:250]:
            cited = cite.get("dst_pub") or cite.get("pub")
            if cited:
                lines.append(f"- [{_md_escape(cited)}]({pubnorm.google_url(cited)})"
                             f" — {_md_escape(cite.get('category') or cite.get('origin') or '')}")
        lines.append("")
    lines.extend(["---", "",
                  "Generated by rotemAI patent search. Verify legal conclusions against the official publication.", ""])
    return "\n".join(lines)


def _safe_filename(rank, pub, title):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{rank:02d}-{pub}-{title}").strip("-.")[:150]
    return (stem or f"{rank:02d}-patent") + ".md"


def _build(slug, report, final_view, reports_dir, sig):
    _set_state(slug, reports_dir, status="building", ready=False, signature=sig,
               message="Resolving the top 50 publications and their full text…")
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        cards = _candidate_cards(report, final_view)
        archive_name = f"{slug}-{sig}-top-{TOP_N}-full-text.zip"
        output = ARCHIVE_DIR / archive_name
        manifest = []
        n_full = 0
        missing_publications = []
        with tempfile.NamedTemporaryFile(dir=ARCHIVE_DIR, prefix=f".{slug}.", suffix=".tmp",
                                         delete=False) as tmp_file:
            tmp = Path(tmp_file.name)
        try:
            conn = db.connect()
            conn.autocommit = True
            cur = conn.cursor()
            try:
                with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED,
                                     compresslevel=6) as zf:
                    for idx, card in enumerate(cards, 1):
                        if _STOP.is_set():
                            raise _ArchiveInterrupted()
                        card["archive_rank"] = idx
                        _set_state(slug, reports_dir, status="building", ready=False,
                                   signature=sig, n_done=idx - 1, n_patents=len(cards),
                                   message=f"Saving full text {idx} of {len(cards)}: {card['pub']}")
                        record = _merge_record(cur, card)
                        markdown = _markdown(card, record)
                        filename = _safe_filename(idx, card["pub"], record["publication"].get("title"))
                        zf.writestr(filename, markdown.encode("utf-8"))
                        n_full += int(record["full_text"])
                        if not record["full_text"]:
                            missing_publications.append(card["pub"])
                        manifest.append({"rank": idx, "publication": card["pub"],
                                         "title": record["publication"].get("title"),
                                         "file": filename, "full_text": record["full_text"],
                                         "n_claims": len(record["claims"]),
                                         "n_description_parts": len(record["paragraphs"]),
                                         "n_sketch_links": len(record["images"]),
                                         "sources": record["sources"]})
                    generated = datetime.now(timezone.utc).isoformat()
                    readme = (f"# Top {len(cards)} full-text patent archive\n\n"
                              f"Search: {report.get('query','')}\n\n"
                              f"Generated: {generated}\n\n"
                              f"{n_full} of {len(cards)} publications include both claims and description. "
                              "Any unavailable source text is called out inside its Markdown file; "
                              "every file links to the official web records and available sketches.\n")
                    zf.writestr("README.md", readme.encode("utf-8"))
                    zf.writestr("manifest.json", json.dumps({"slug": slug, "signature": sig,
                                                              "generated_at": generated,
                                                              "patents": manifest},
                                                             default=str, indent=2).encode("utf-8"))
            finally:
                cur.close()
                conn.close()
            if not output.exists():
                os.replace(tmp, output)
                tmp = None
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        state = _set_state(
            slug, reports_dir, status="ready", ready=True, signature=sig, top_n=TOP_N,
            n_patents=len(cards), n_full_text=n_full, n_missing_text=len(cards) - n_full,
            missing_publications=missing_publications,
            n_done=len(cards), archive_file=archive_name,
            download_name=f"prior-art-{slug}-top-{TOP_N}-full-text.zip",
            generated_at=datetime.now(timezone.utc).isoformat(), message="Archive ready")
        return state
    except _ArchiveInterrupted:
        return _set_state(slug, reports_dir, status="interrupted", ready=False, signature=sig,
                          message="Archive interrupted by a service restart; it will resume automatically.")
    except Exception as exc:
        traceback.print_exc()
        return _set_state(slug, reports_dir, status="error", ready=False, signature=sig,
                          error=str(exc)[:300], message="Archive generation failed")


def shutdown(wait=False):
    _STOP.set()
    _POOL.shutdown(wait=wait, cancel_futures=True)
