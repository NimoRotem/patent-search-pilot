"""Official-source enrichment (spec §2.3 + §6 step 8).

Fills full-text holes before the evidence reader runs. Recovery is deliberately multi-source:
the local lemad corpus, EPO OPS for EP/WO, the public Google Patents document page, ScrapingBee
for a throttled Google page, and finally SerpApi when its monthly allowance is available. Every
successful path is normalized into the same claims, paragraphs and chunks with provenance.
"""
from __future__ import annotations
import os, re, json, sys, threading, time
import requests
import db, patent_text as pt
from fulltext_recovery import fetch_google_full_text
from config import DATA
import pubnorm  # zero-padded Google Patents ids (dropped-zero fix)

SERP_KEY = os.environ.get("SERPAPI_API_KEY", "")
SB_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "")


def gp_id(pubnum: str) -> str:
    """US-11999030-B2 -> patent/US11999030B2/en, and US-2015032252-A1 -> US20150032252A1.

    THE SECOND CASE IS THE POINT. Stripping the hyphens is right for a granted patent and wrong for
    a US pre-grant publication: the corpus stores US-2015032252-A1 with the leading zero of the
    serial dropped, and Google Patents only resolves the zero-PADDED US20150032252A1. Every
    pre-grant lookup was therefore requesting a document that does not exist, returning nothing,
    and still spending a SerpApi call. Measured on the field backfill: the hit rate was 27% until
    this was fixed, and US pre-grant applications are a large share of the most-cited targets.

    pubnorm already computes the padded, kind-bearing form and is the same helper that fixed this
    class of bug for the outbound office links and the Mongo lookup.
    """
    try:
        cands = pubnorm.mongo_candidates(pubnum)
        if cands:
            return "patent/" + cands[0] + "/en"
    except Exception:
        pass
    return "patent/" + pubnum.replace("-", "") + "/en"


#  Live searches and the background field backfill share ONE monthly SerpApi allowance.
#  ops/enrich_field.py refuses to start without leaving a reserve; the live path had no such
#  guard, so the batch job stopped politely while searches kept spending until the allowance hit
#  zero mid-run. Both ends need a floor, and the live path's should be the higher one: a search a
#  user is waiting on matters more than a backfill that can resume next month.
LIVE_RESERVE = int(os.environ.get("SERPAPI_LIVE_RESERVE", "200"))
_QUOTA_TTL = float(os.environ.get("SERPAPI_QUOTA_TTL", "600"))
_quota = {"left": None, "at": 0.0}
_quota_lock = threading.Lock()


def searches_left(force: bool = False):
    """SerpApi searches remaining this month, or None if it cannot be read. Cached 10 minutes.

    Never raises and never blocks a search on its own failure: an unreadable account reads as
    'unknown', which is treated as 'allowed', because refusing to search because we could not
    check a quota would be a worse failure than overspending slightly.
    """
    if not SERP_KEY:
        return None
    now = time.time()
    with _quota_lock:
        #  Freshness is decided by the TIMESTAMP alone, so an UNREADABLE account is cached too.
        #  Keying on the value meant a failing account endpoint was re-tried on every reference,
        #  putting a network round-trip and its timeout on each one.
        if not force and _quota["at"] and now - _quota["at"] < _QUOTA_TTL:
            return _quota["left"]
    try:
        r = requests.get("https://serpapi.com/account", params={"api_key": SERP_KEY}, timeout=15)
        left = int((r.json() or {}).get("total_searches_left") or 0) if r.status_code == 200 \
            else None
    except Exception:
        left = None
    with _quota_lock:
        _quota["left"], _quota["at"] = left, now
    return left


def may_spend() -> bool:
    """Is a live SerpApi call allowed right now?

    Separate from fetch_details so the POLICY is testable on its own: the hermetic test suite
    stubs fetch_details out entirely (conftest.no_paid_apis), so a test that drove the guard
    through it would pass whether the guard existed or not.
    """
    if not SERP_KEY:
        return False
    left = searches_left()
    if left is not None and left <= LIVE_RESERVE:
        print(f"[enrich] SerpApi allowance down to {left}; holding the last {LIVE_RESERVE} "
              f"back. Full-text enrichment is off until it renews.", flush=True)
        return False
    return True


def fetch_details(pubnum: str, retries=3):
    """SerpApi Google Patents details -> dict, or None."""
    if not may_spend():
        return None
    params = {"engine": "google_patents_details", "patent_id": gp_id(pubnum), "api_key": SERP_KEY}
    for i in range(retries):
        try:
            r = requests.get("https://serpapi.com/search", params=params, timeout=40)
            if r.status_code == 200:
                with _quota_lock:            # one spent, without re-reading the account
                    if _quota["left"]:
                        _quota["left"] -= 1
                return r.json()
            if r.status_code == 429:
                with _quota_lock:            # exhausted or throttled: re-check before the next
                    _quota["at"] = 0.0
                return None
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return None


def _claims_from_details(d):
    """Return a single claims blob from SerpApi 'claims' (list or string)."""
    cl = d.get("claims")
    if isinstance(cl, list):
        return "\n".join(f"{i+1}. {c}" if not re.match(r'^\s*\d', str(c)) else str(c)
                         for i, c in enumerate(cl))
    return cl or ""


def recovery_available():
    """Whether the live reader may try text recovery.

    Direct Google Patents needs no credential, so recovery remains available when SerpApi is out
    of quota. Operators can still disarm all outbound recovery explicitly for an offline run.
    """
    return os.environ.get("FULLTEXT_RECOVERY_DISABLED", "0") != "1"


def _as_lines(value):
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    out = []
    for item in value if isinstance(value, (list, tuple)) else [value]:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = (item.get("text") or item.get("claim_text") or item.get("description") or
                    item.get("value") or "")
            if isinstance(text, list):
                text = " ".join(str(part) for part in text if part)
        else:
            text = str(item)
        text = " ".join(str(text or "").split())
        if text:
            out.append(text)
    return out


def _merge_text(target, candidate, source):
    used = False
    for key in ("claims", "description", "abstract"):
        values = _as_lines((candidate or {}).get(key))
        if values and not target[key]:
            target[key] = values
            used = True
    if used and source and source not in target["sources"]:
        target["sources"].append(source)
    return bool(target["claims"] and target["description"])


def fetch_best_full_text(pubnum):
    """Resolve the richest available specification without making SerpApi a single point of failure."""
    result = {"claims": [], "description": [], "abstract": [], "sources": [],
              "_serp_raw": None}

    # The pre-built corpus is instant and costs no provider request.
    try:
        import mongo_corpus
        md = mongo_corpus.get_detail(pubnum) or {}
        if _merge_text(result, md, "lemad:mongo"):
            return result
    except Exception:
        pass

    # OPS is the issuing-office source for EP/WO full text and is preferred when configured.
    if str(pubnum).upper()[:2] in ("EP", "WO"):
        try:
            import ops
            if ops.have_creds():
                recovered = ops.ops_fetch(pubnum, want=("claims", "description"))
                payload = {"claims": recovered.get("claims"),
                           "description": recovered.get("paragraphs")}
                if _merge_text(result, payload, "epo:ops"):
                    return result
        except Exception:
            pass

    # Google normally answers directly. ScrapingBee is used only if the document page is blocked,
    # throttled or incomplete, so an exhausted SerpApi account cannot strand the reader.
    try:
        google = fetch_google_full_text(pubnum, SB_KEY)
        if _merge_text(result, google, google.get("source")):
            return result
    except Exception:
        pass

    # SerpApi remains useful as a last structured source when it has allowance, but it no longer
    # controls whether recovery runs at all.
    details = fetch_details(pubnum)
    if details:
        result["_serp_raw"] = details
        patent = details.get("patent") if isinstance(details.get("patent"), dict) else {}
        payload = {
            "claims": details.get("claims") or patent.get("claims"),
            "description": (details.get("description") or details.get("full_description") or
                            patent.get("description")),
            "abstract": details.get("abstract") or patent.get("abstract"),
        }
        _merge_text(result, payload, "serpapi:google_patents")
    return result


def _claim_blob(lines):
    numbered = []
    for i, line in enumerate(lines or [], 1):
        line = " ".join(str(line or "").split())
        if not line:
            continue
        match = re.match(r"^\s*(\d{1,3})\s*[.)、]\s*(.*)$", line)
        numbered.append(f"{match.group(1)}. {match.group(2)}" if match else f"{i}. {line}")
    return "\n".join(numbered)


def _persist_full_text(pubnum, payload, reembed=False):
    """Write recovered text once, including the chunks consumed by ``deep_analysis``."""
    sources = list(payload.get("sources") or [])
    source_name = "+".join(sources) or "unknown:fulltext"
    source_id = db.get_source_id(source_name, "live")
    status = "authoritative" if sources == ["epo:ops"] else "scrape"
    added = {"claims": 0, "paragraphs": 0, "chunks": 0}
    chunk_ids = []
    with db.cursor() as cur:
        cur.execute("SELECT id, abstract FROM publications WHERE publication_number=%s "
                    "LIMIT 1 FOR UPDATE", (pubnum,))
        row = cur.fetchone()
        if not row:
            return {"pub": pubnum, "ok": False, "reason": "not_in_corpus"}
        pid = row["id"]
        claim_rows, para_rows = [], []

        cur.execute("SELECT count(*) c FROM claims WHERE publication_id=%s", (pid,))
        have_claims = int(cur.fetchone()["c"] or 0)
        if not have_claims:
            claims = pt.resolve_claims(pt.split_claims(_claim_blob(payload.get("claims") or [])))
            for claim in claims[:200]:
                cur.execute(
                    "INSERT INTO claims(publication_id,claim_no,is_independent,lang,text,resolved_text) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (pid, claim["claim_no"], claim["is_independent"], "en", claim["text"],
                     claim["resolved_text"]))
                claim_rows.append({"id": cur.fetchone()["id"], "claim_no": claim["claim_no"],
                                   "lang": "en", "text": claim["text"],
                                   "resolved_text": claim["resolved_text"]})
                added["claims"] += 1
        else:
            cur.execute("SELECT count(*) c FROM chunks WHERE publication_id=%s "
                        "AND kind LIKE 'claim%%'", (pid,))
            if not int(cur.fetchone()["c"] or 0):
                cur.execute("SELECT id,claim_no,lang,text,resolved_text FROM claims "
                            "WHERE publication_id=%s ORDER BY claim_no", (pid,))
                claim_rows = [dict(item) for item in cur.fetchall()]

        cur.execute("SELECT count(*) c FROM paragraphs WHERE publication_id=%s", (pid,))
        have_paragraphs = int(cur.fetchone()["c"] or 0)
        if not have_paragraphs:
            description = "\n".join(payload.get("description") or [])
            paragraphs = pt.split_paragraphs(description)
            if description.strip() and not paragraphs:
                paragraphs = [{"para_no": "p0001", "heading": None, "text": description}]
            for paragraph in paragraphs[:1200]:
                cur.execute(
                    "INSERT INTO paragraphs(publication_id,para_no,heading,page_no,lang,text) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (pid, paragraph.get("para_no"), paragraph.get("heading"), None, "en",
                     paragraph.get("text")))
                para_rows.append({"id": cur.fetchone()["id"],
                                  "para_no": paragraph.get("para_no"),
                                  "heading": paragraph.get("heading"), "lang": "en",
                                  "text": paragraph.get("text")})
                added["paragraphs"] += 1
        else:
            cur.execute("SELECT count(*) c FROM chunks WHERE publication_id=%s "
                        "AND kind='paragraph'", (pid,))
            if not int(cur.fetchone()["c"] or 0):
                cur.execute("SELECT id,para_no,heading,lang,text FROM paragraphs "
                            "WHERE publication_id=%s ORDER BY id", (pid,))
                para_rows = [dict(item) for item in cur.fetchall()]

        import ops
        for chunk in ops._chunk_rows(pid, claim_rows, para_rows):
            cur.execute(
                "INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id", chunk)
            chunk_ids.append(cur.fetchone()["id"])
        added["chunks"] = len(chunk_ids)

        abstract = "\n".join(payload.get("abstract") or []).strip()
        if abstract and not (row.get("abstract") or "").strip():
            cur.execute("UPDATE publications SET abstract=%s WHERE id=%s", (abstract, pid))
        if any(added.values()):
            cur.execute("SELECT 1 FROM field_provenance WHERE entity='publication' AND "
                        "entity_id=%s AND field='recovered_fulltext' AND source_id=%s LIMIT 1",
                        (pid, source_id))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO field_provenance(entity,entity_id,field,source_id,ocr_status) "
                    "VALUES ('publication',%s,'recovered_fulltext',%s,%s)",
                    (pid, source_id, status))

    if reembed and chunk_ids:
        _embed_chunk_ids(chunk_ids)
    return {"pub": pubnum, "ok": bool(any(added.values()) or have_claims or have_paragraphs),
            "source": source_name, "added_claims": added["claims"],
            "added_paragraphs": added["paragraphs"], "added_chunks": added["chunks"],
            "reembedded": bool(reembed and chunk_ids)}


def _persist_serp_metadata(pubnum, raw):
    if not raw:
        return {"pdf": False, "events": 0}
    pdf = raw.get("pdf") or (raw.get("patent") or {}).get("pdf")
    events = [event for event in (raw.get("events") or []) if isinstance(event, dict)]
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pubnum,))
        row = cur.fetchone()
        if not row:
            return {"pdf": False, "events": 0}
        pid = row["id"]
        if pdf:
            cur.execute("UPDATE publications SET facsimile_path=%s WHERE id=%s", (pdf, pid))
        for event in events:
            code = event.get("type") or event.get("title")
            date = _safe_date(event.get("date"))
            cur.execute("SELECT 1 FROM legal_events WHERE publication_id=%s AND event_code=%s "
                        "AND event_date IS NOT DISTINCT FROM %s LIMIT 1", (pid, code, date))
            if not cur.fetchone():
                cur.execute("INSERT INTO legal_events(publication_id,event_code,event_date,raw) "
                            "VALUES (%s,%s,%s,%s)", (pid, code, date, json.dumps(event)))
    return {"pdf": bool(pdf), "events": len(events)}


def enrich_publication(pubnum, reembed=False):
    """Recover and persist one specification using every configured source in priority order."""
    if not recovery_available():
        return {"pub": pubnum, "ok": False, "reason": "recovery_disabled"}
    payload = fetch_best_full_text(pubnum)
    if not (payload.get("claims") or payload.get("description")):
        return {"pub": pubnum, "ok": False, "reason": "no_full_text_source"}
    result = _persist_full_text(pubnum, payload, reembed=reembed)
    result.update(_persist_serp_metadata(pubnum, payload.get("_serp_raw")))
    return result


def _safe_date(s):
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(s))
    return m.group(0) if m else None


def _embed_chunk_ids(chunk_ids):
    """Embed EXACTLY these chunk ids, using the corpus's embedding contract.

    Why not `embed.run(limit=n)` (what this module used to do): `run()` selects
    `WHERE embedding IS NULL ORDER BY <kind priority>, id` across the WHOLE table. Newly
    inserted rows have the highest ids, so whenever a backlog exists (there are ~25k
    unembedded chunks right now) `run(limit=n)` embeds n rows off the FRONT of that backlog
    and never reaches the rows we just wrote. The enrichment then looks successful — claims
    inserted, no error — while the new text stays unembedded and therefore invisible to
    every vector channel. Silent, and exactly the failure mode that makes enrichment appear
    not to work.

    Contract must match src/embed.py exactly or retrieval degrades for these documents:
    Vertex gemini-embedding-001, EMBED_DIM (768), task_type=RETRIEVAL_DOCUMENT.
    """
    import embed as _embed
    from config import EMBED_DIM
    chunk_ids = [c for c in (chunk_ids or []) if c is not None]
    if not chunk_ids:
        return 0
    done = 0
    with db.cursor() as cur:
        cur.execute("SELECT id, text FROM chunks WHERE id = ANY(%s) AND embedding IS NULL "
                    "ORDER BY id", (chunk_ids,))
        rows = cur.fetchall()
    for i in range(0, len(rows), 200):                     # 200 = embed.SUB
        sub = rows[i:i + 200]
        vecs = _embed.embed_texts([r["text"] for r in sub], EMBED_DIM,
                                  task_type="RETRIEVAL_DOCUMENT")
        with db.cursor() as cur:
            for r, v in zip(sub, vecs):
                cur.execute("UPDATE chunks SET embedding=%s::vector WHERE id=%s",
                            ("[" + ",".join(f"{x:.6f}" for x in v) + "]", r["id"]))
        done += len(sub)
    return done


def _reembed_pub(pid):
    """Chunk + embed a publication's not-yet-chunked claims (keeps the index current).

    Returns the number of chunks embedded. Only touches chunks it created. Fine for one
    publication inside a search; a BULK caller should use chunk_pub_claims() and then embed the
    collected ids in batches, because one Vertex round-trip per publication is about twenty chunks
    and measured out at one publication per second.
    """
    return _embed_chunk_ids(chunk_pub_claims(pid))


def chunk_pub_claims(pid):
    """Insert claim chunks for a publication's not-yet-chunked claims. Returns the new chunk ids.

    Pure database work, no embedding: separated so a backfill can create every chunk first and
    then embed them in properly sized, parallel batches.
    """
    import json as _j
    new_ids = []
    with db.cursor() as cur:
        #  Which of this publication's claims are already chunked, looked up BY PUBLICATION.
        #
        #  This used to be `id NOT IN (SELECT ref_id FROM chunks WHERE ... kind LIKE 'claim%')`,
        #  an uncorrelated subquery over the WHOLE chunks table: 16 million claim chunks scanned
        #  once per publication. Invisible when a search chunks one publication, and fatal in a
        #  backfill, where it did fewer than a thousand publications in twenty minutes. The
        #  publication-scoped lookup uses ix_chunks_pub.
        cur.execute("SELECT ref_id FROM chunks WHERE publication_id=%s AND ref_id IS NOT NULL "
                    "AND kind LIKE 'claim%%'", (pid,))
        already = {r["ref_id"] for r in cur.fetchall()}
        cur.execute("SELECT id, claim_no, is_independent, lang, text, resolved_text FROM claims "
                    "WHERE publication_id=%s", (pid,))
        rows = []
        for c in cur.fetchall():
            if c["id"] in already:
                continue
            coord = _j.dumps({"claim_no": c["claim_no"]})
            own = (c["text"] or "")[:8000]
            if own:
                rows.append((pid, "claim_own", c["id"], coord, c["lang"] or "en", own,
                             max(1, len(own) // 4)))
            res = (c["resolved_text"] or "")[:8000]
            if res and res != own:
                rows.append((pid, "claim_resolved", c["id"], coord, c["lang"] or "en", res,
                             max(1, len(res) // 4)))
        for r in rows:
            cur.execute("INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id", r)
            new_ids.append(cur.fetchone()["id"])
    return new_ids


def enrich_final_set(pubnums, reembed=False):
    out = []
    for p in pubnums:
        r = enrich_publication(p, reembed=reembed)
        out.append(r)
        print(f"  {p}: {r}")
    return out


# =====================================================================================
# SIBLING-VERSION CONTENT RECOVERY (ported from the federated app's detail.py _variants)
# =====================================================================================
# The federated app recovers text/drawings for content-free publications (EP…A4 search
# reports, EP…A3) by re-probing sibling kind codes of the SAME base number until it finds
# a version that has content, recording provenance like "content from sibling EP…A1".
#
# MEASURED IMPACT ON THIS CORPUS (2026-07-19, 107,795 pubs / 48,790 with claims):
#
#   claimless publications ........................................ 59,005
#   recoverable via STRICT kind-code sibling (the federated rule) ..    119  (0.2%)
#   recoverable via SIMPLE FAMILY sibling ......................... 31,390  (53%)
#   families with ZERO claims anywhere in the corpus ............... 24,314 of 64,972
#   claimless pubs with no abstract either ........................ 17,553
#   pubs with claims but no embedded claim chunk ..................     54
#
# READ THIS BEFORE EXPECTING A RECALL WIN — three conclusions, in order of importance:
#
# 1. The federated app's exact heuristic is IMMATERIAL here (119 pubs, 0.2%). It pays off
#    there because that app resolves arbitrary user-supplied numbers live; our corpus was
#    ingested de-duplicated at the base-number level, so only 3,581 base numbers have more
#    than one kind code at all. Ported anyway (it is nearly free and fixes the detail view
#    for those 119), but it is NOT the fix for the 68%-missing-claims problem.
#
# 2. Family-level recovery looks 264x bigger but is worth ~0 on the headline metric, and
#    it is important to say so rather than bank a fake win. recall@k is scored over gold
#    FAMILIES. If pub A is claimless and its family sibling B has claims, family F is
#    ALREADY retrievable through B — giving A a copy of B's text adds a second way to
#    retrieve a family we could already reach. It cannot lift family recall, and it costs
#    real precision: duplicate near-identical vectors crowd the top-k and make
#    dedup-by-family harder.
#
# 3. The genuine hole is the 24,314 families (37%) with NO claims anywhere in the corpus.
#    Nothing local can fix those — no sibling has the text either. That requires external
#    enrichment (EPO OPS via ops.py, or SerpApi via enrich_publication above), which is
#    exactly what REACHABILITY.md concluded. Sibling recovery is not a substitute for it.
#
# DESIGN DECISION that follows from the above:
#   * STRICT kind-code siblings ARE embedded. EP-1609990-A4 and EP-1609990-A1 are the same
#     application and the same disclosure published twice, so the text legitimately belongs
#     to both records.
#   * FAMILY siblings are NEVER embedded. A US equivalent's claims are a different legal
#     document with different scope; embedding them under the EP record would poison the
#     index (wrong claim scope attributed to a publication, plus the duplicate-vector
#     problem in point 2). They are exposed for DISPLAY and claim-chart context only, always
#     labelled with the publication they came from.
# Both paths write to field_provenance so nothing recovered is ever mistaken for original.

_KIND_SPLIT = re.compile(r"^(.*?)-([A-Z]\d?)$")

# Kind-code preference. The federated app had to GUESS a fixed list of kind codes because it
# probed a remote API blind — it could only ask "does EP-1609990-A1 exist?" one call at a time.
# We have the corpus locally, so we LOOK UP which siblings actually exist and merely RANK them.
# That distinction matters: a hardcoded whitelist silently missed real recoveries here
# (DE-…-B4, DE-…-T5, DE-…-T2 all carry claims but are not in the federated app's list), which
# is exactly the kind of false negative that is invisible until you diff against the DB.
#
# Lower rank = preferred. Granted specifications first (their claims are the operative,
# examined scope), then published applications, then national-phase translations.
_KIND_RANK = {
    "B1": 0, "B2": 1, "B3": 2, "B4": 3, "B9": 4,          # granted
    "C1": 5, "C2": 6, "C3": 7,                             # granted (older DE)
    "A1": 10, "A2": 11, "A3": 12, "A4": 13, "A9": 14, "A": 15,   # published applications
    "T1": 20, "T2": 21, "T3": 22, "T4": 23, "T5": 24,      # DE translations of EP specs
    "U1": 30, "U8": 31,                                    # utility models
}
_KIND_FALLBACK_RANK = 50

# Kept for callers that want the "what could exist" view without touching the DB.
SIBLING_KIND_ORDER = tuple(sorted(_KIND_RANK, key=_KIND_RANK.get))


def split_pubnum(pubnum: str):
    """'EP-1609990-A4' -> ('EP-1609990', 'A4'). Returns (pubnum, '') if no kind suffix."""
    m = _KIND_SPLIT.match((pubnum or "").strip())
    return (m.group(1), m.group(2)) if m else ((pubnum or "").strip(), "")


def sibling_candidates(pubnum: str) -> list:
    """Speculative sibling numbers of the same base, most-content-rich first.

    Only used for display / the federated-style blind probe. `find_sibling_with_claims`
    does NOT use this — it asks the database what really exists.
    """
    base, kind = split_pubnum(pubnum)
    if not base:
        return []
    return [f"{base}-{k}" for k in SIBLING_KIND_ORDER if k != kind]


def kind_rank(kind: str) -> int:
    return _KIND_RANK.get((kind or "").upper(), _KIND_FALLBACK_RANK)


def find_sibling_with_claims(pubnum: str, cur):
    """The best LOCAL sibling that actually has claims, or None.

    Strict: same base number only (same application, same disclosure) — never a family
    member, which would be a different legal document. Ranks real DB rows by kind
    preference, breaking ties on claim count.
    """
    base, kind = split_pubnum(pubnum)
    if not base:
        return None
    cur.execute(
        """SELECT p.id, p.publication_number, p.kind_code, count(c.id) AS n
           FROM publications p JOIN claims c ON c.publication_id = p.id
           WHERE p.publication_number <> %s
             AND regexp_replace(p.publication_number, '-[A-Z][0-9]?$', '') = %s
           GROUP BY p.id, p.publication_number, p.kind_code
           HAVING count(c.id) > 0""",
        (pubnum, base))
    rows = cur.fetchall()
    if not rows:
        return None
    rows.sort(key=lambda r: (kind_rank(r["kind_code"]), -r["n"]))
    return rows[0]


def audit_sibling_recovery() -> dict:
    """Re-measure the numbers in the comment block above. Cheap, read-only; run it after an
    ingest to see whether local recovery has become worth more than it is today."""
    out = {}
    with db.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE _sib ON COMMIT DROP AS
            SELECT p.id, p.publication_number, p.simple_family_id AS fam,
                   regexp_replace(p.publication_number, '-[A-Z][0-9]?$', '') AS basenum,
                   EXISTS(SELECT 1 FROM claims c WHERE c.publication_id = p.id) AS hc
            FROM publications p""")
        cur.execute("CREATE INDEX ON _sib(basenum)")
        cur.execute("CREATE INDEX ON _sib(fam)")
        cur.execute("SELECT count(*) t, count(*) FILTER (WHERE hc) w FROM _sib")
        r = cur.fetchone()
        out["total"] = r["t"]
        out["with_claims"] = r["w"]
        out["claimless"] = r["t"] - r["w"]
        cur.execute("""SELECT count(*) n FROM _sib s
                       JOIN (SELECT basenum b, bool_or(hc) a FROM _sib GROUP BY 1) g
                         ON s.basenum = g.b
                       WHERE NOT s.hc AND g.a""")
        out["recoverable_strict_sibling"] = cur.fetchone()["n"]
        cur.execute("""SELECT count(*) n FROM _sib s
                       JOIN (SELECT fam f, bool_or(hc) a FROM _sib WHERE fam IS NOT NULL GROUP BY 1) g
                         ON s.fam = g.f
                       WHERE NOT s.hc AND g.a""")
        out["recoverable_family_sibling"] = cur.fetchone()["n"]
        cur.execute("""SELECT count(*) n FROM (SELECT fam FROM _sib WHERE fam IS NOT NULL
                       GROUP BY fam HAVING bool_or(hc) = false) x""")
        out["families_with_zero_claims"] = cur.fetchone()["n"]
    return out


def recover_from_sibling(pubnum, reembed=True, dry_run=False):
    """Copy claims from a strict kind-code sibling ALREADY IN THE CORPUS onto `pubnum`.

    Free: no external API, no network. The copied claims are chunked and embedded exactly
    like the rest of the corpus via `_reembed_pub` (Vertex gemini-embedding-001, 768-d,
    RETRIEVAL_DOCUMENT) so the index stays homogeneous — a mismatched task_type or
    dimension here would silently degrade retrieval for every affected document.

    Provenance is mandatory: every copied claim is recorded in field_provenance with
    ocr_status='sibling' and normalized_value naming the source publication, so recovered
    text is never confused with text the office actually published under this number.

    No-ops if the publication already has claims. Returns a result dict.
    """
    src_id = db.get_source_id("local:sibling_recovery", "2026-07")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pubnum,))
        row = cur.fetchone()
        if not row:
            return {"pub": pubnum, "ok": False, "reason": "not_in_corpus"}
        pid = row["id"]
        cur.execute("SELECT count(*) c FROM claims WHERE publication_id=%s", (pid,))
        if cur.fetchone()["c"] > 0:
            return {"pub": pubnum, "ok": False, "reason": "already_has_claims"}
        sib = find_sibling_with_claims(pubnum, cur)
        if not sib:
            return {"pub": pubnum, "ok": False, "reason": "no_sibling_with_claims"}
        if dry_run:
            return {"pub": pubnum, "ok": True, "dry_run": True,
                    "sibling": sib["publication_number"], "available_claims": sib["n"]}
        cur.execute("""SELECT claim_no, is_independent, lang, text, resolved_text
                       FROM claims WHERE publication_id=%s ORDER BY claim_no""", (sib["id"],))
        copied = 0
        for c in cur.fetchall():
            cur.execute("""INSERT INTO claims(publication_id, claim_no, is_independent, lang,
                                              text, resolved_text)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (pid, c["claim_no"], c["is_independent"], c["lang"],
                         c["text"], c["resolved_text"]))
            copied += 1
        cur.execute("""INSERT INTO field_provenance(entity, entity_id, field, source_id,
                                                    original_value, normalized_value, ocr_status)
                       VALUES ('publication',%s,'claims',%s,%s,%s,'sibling')""",
                    (pid, src_id, sib["publication_number"],
                     f"content from sibling {sib['publication_number']}"))
    res = {"pub": pubnum, "ok": True, "sibling": sib["publication_number"], "copied_claims": copied}
    if reembed and copied:
        _reembed_pub(pid)
        res["reembedded"] = True
    return res


def recover_all_siblings(limit=None, dry_run=True, reembed=True):
    """Sweep the corpus for claimless pubs whose STRICT sibling has claims and recover them.

    dry_run=True by default: this mutates the index, so the caller must opt in.
    Expect ~119 candidates on the current corpus (see the comment block above).
    """
    with db.cursor() as cur:
        cur.execute("""
            WITH s AS (
              SELECT p.id, p.publication_number,
                     regexp_replace(p.publication_number, '-[A-Z][0-9]?$', '') AS basenum,
                     EXISTS(SELECT 1 FROM claims c WHERE c.publication_id = p.id) AS hc
              FROM publications p)
            SELECT s.publication_number FROM s
            JOIN (SELECT basenum, bool_or(hc) a FROM s GROUP BY basenum) g USING (basenum)
            WHERE NOT s.hc AND g.a
            ORDER BY s.publication_number""")
        pubs = [r["publication_number"] for r in cur.fetchall()]
    if limit:
        pubs = pubs[:limit]
    out = {"candidates": len(pubs), "recovered": 0, "claims": 0, "dry_run": dry_run, "results": []}
    for p in pubs:
        r = recover_from_sibling(p, reembed=reembed and not dry_run, dry_run=dry_run)
        out["results"].append(r)
        if r.get("ok"):
            out["recovered"] += 1
            out["claims"] += r.get("copied_claims") or r.get("available_claims") or 0
    return out


def family_sibling_context(pubnum, max_claims=20):
    """Claims from a SIMPLE-FAMILY sibling, for DISPLAY / claim-chart context ONLY.

    Deliberately NOT embedded and NOT written into the claims table — a family member is a
    different legal document (different jurisdiction, different granted scope), so treating
    its claims as this publication's would misstate the prior art. Returns text clearly
    attributed to its real source, or {} when nothing is available.

    This is the honest way to use the 31,390-pub family overlap: show the user "no claims on
    this record; the family member US-… does disclose the following", without pretending the
    text is this publication's or double-indexing the family.
    """
    with db.cursor() as cur:
        cur.execute("""SELECT id, simple_family_id FROM publications
                       WHERE publication_number=%s LIMIT 1""", (pubnum,))
        row = cur.fetchone()
        if not row or not row["simple_family_id"]:
            return {}
        cur.execute("""SELECT p.id, p.publication_number, count(c.id) n
                       FROM publications p JOIN claims c ON c.publication_id = p.id
                       WHERE p.simple_family_id = %s AND p.id <> %s
                       GROUP BY p.id, p.publication_number
                       ORDER BY n DESC LIMIT 1""", (row["simple_family_id"], row["id"]))
        sib = cur.fetchone()
        if not sib:
            return {}
        cur.execute("""SELECT claim_no, text FROM claims WHERE publication_id=%s
                       ORDER BY claim_no LIMIT %s""", (sib["id"], max_claims))
        claims = [{"claim_no": c["claim_no"], "text": c["text"]} for c in cur.fetchall() if c["text"]]
    return {"source_pub": sib["publication_number"], "family_id": row["simple_family_id"],
            "claims": claims, "embedded": False,
            "note": f"Claims shown are from family member {sib['publication_number']}, "
                    f"not from {pubnum}. Scope may differ."}


if __name__ == "__main__":
    # demo: enrich the cross-lingual DE anchors that BigQuery left claim-less
    pubs = sys.argv[1:] or ["DE-202019005606-U1", "DE-102017106252-A1", "DE-4327663-A1"]
    enrich_final_set(pubs, reembed="--reembed" in sys.argv)
