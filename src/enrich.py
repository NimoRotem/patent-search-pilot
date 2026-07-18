"""Official-source enrichment (spec §2.3 + §6 step 8).

Fills EP/WO/DE full-text holes that BigQuery lacks and attaches drawings / facsimile PDF /
legal status for FINAL candidates. Canonical source would be EPO OPS (INADOC family, facsimile)
+ USPTO ODP — we have no OPS credentials here, so we use SerpApi's Google Patents details engine
(structured claims/description/pdf/events) with a ScrapingBee HTML fallback. Provenance records
the real source and a non-authoritative 'scrape' status: the facsimile PDF remains the legal
evidence; scraped OCR text can be wrong.

EPO OPS: `ops_fetch()` is now IMPLEMENTED in `ops.py` (zero-step unlock). The moment
OPS_CONSUMER_KEY/OPS_CONSUMER_SECRET land in `.env`, `ops.backfill(pubnums)` fills the full
EP/WO/DE description+claims+drawings+legal hole. Until then `ops.py` runs in mock/dry-run mode
(`python ops.py --dry-run`, `python test_ops.py`) so the parser + schema mapping are provable
without credentials. One-command backfill: see README.
"""
from __future__ import annotations
import os, re, json, sys, time
import requests
import db, patent_text as pt
from config import DATA

SERP_KEY = os.environ.get("SERPAPI_API_KEY", "")
SB_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "")


def gp_id(pubnum: str) -> str:
    """US-11999030-B2 -> patent/US11999030B2/en"""
    return "patent/" + pubnum.replace("-", "") + "/en"


def fetch_details(pubnum: str, retries=3):
    """SerpApi Google Patents details -> dict, or None."""
    if not SERP_KEY:
        return None
    params = {"engine": "google_patents_details", "patent_id": gp_id(pubnum), "api_key": SERP_KEY}
    for i in range(retries):
        try:
            r = requests.get("https://serpapi.com/search", params=params, timeout=40)
            if r.status_code == 200:
                return r.json()
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


def enrich_publication(pubnum, reembed=False):
    """Fetch official full text + PDF + legal events for one publication; fill gaps + provenance."""
    src = db.get_source_id("serpapi:google_patents", "2026-07")
    d = fetch_details(pubnum)
    if not d:
        return {"pub": pubnum, "ok": False, "reason": "no_details"}
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pubnum,))
        row = cur.fetchone()
        if not row:
            return {"pub": pubnum, "ok": False, "reason": "not_in_corpus"}
        pid = row["id"]
        added_claims = 0
        # claims (only if we currently have none)
        cur.execute("SELECT count(*) c FROM claims WHERE publication_id=%s", (pid,))
        if cur.fetchone()["c"] == 0:
            blob = _claims_from_details(d)
            claims = pt.resolve_claims(pt.split_claims(blob)) if blob else []
            for c in claims:
                cur.execute("INSERT INTO claims(publication_id, claim_no, is_independent, lang, text, resolved_text) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (pid, c["claim_no"], c["is_independent"], "en", c["text"], c["resolved_text"]))
                added_claims += 1
        # facsimile / drawings
        pdf = d.get("pdf") or (d.get("patent") or {}).get("pdf")
        if pdf:
            cur.execute("UPDATE publications SET facsimile_path=%s WHERE id=%s", (pdf, pid))
        # legal status events
        events = d.get("events") or []
        for ev in events:
            if isinstance(ev, dict):
                cur.execute("INSERT INTO legal_events(publication_id, event_code, event_date, raw) "
                            "VALUES (%s,%s,%s,%s)",
                            (pid, ev.get("type") or ev.get("title"),
                             _safe_date(ev.get("date")), json.dumps(ev)))
        # provenance: enrichment is scraped, not authoritative facsimile OCR
        cur.execute("INSERT INTO field_provenance(entity, entity_id, field, source_id, ocr_status) "
                    "VALUES ('publication',%s,'enriched_fulltext',%s,'scrape')", (pid, src))
    res = {"pub": pubnum, "ok": True, "added_claims": added_claims, "pdf": bool(pdf),
           "events": len(events)}
    if reembed and added_claims:
        _reembed_pub(pid)
        res["reembedded"] = True
    return res


def _safe_date(s):
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(s))
    return m.group(0) if m else None


def _reembed_pub(pid):
    """Chunk + embed the newly-added claims of one publication (keeps the index current)."""
    import embed, chunker, json as _j
    with db.cursor() as cur:
        cur.execute("SELECT id, claim_no, is_independent, lang, text, resolved_text FROM claims "
                    "WHERE publication_id=%s AND id NOT IN (SELECT ref_id FROM chunks WHERE ref_id IS NOT NULL "
                    "AND kind LIKE 'claim%%')", (pid,))
        rows = []
        for c in cur.fetchall():
            coord = _j.dumps({"claim_no": c["claim_no"]})
            own = (c["text"] or "")[:8000]
            rows.append((pid, "claim_own", c["id"], coord, c["lang"] or "en", own, max(1, len(own)//4)))
            res = (c["resolved_text"] or "")[:8000]
            if res and res != own:
                rows.append((pid, "claim_resolved", c["id"], coord, c["lang"] or "en", res, max(1, len(res)//4)))
        for r in rows:
            cur.execute("INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)", r)
    embed.run(limit=len(rows) + 5, order_priority=True)


def enrich_final_set(pubnums, reembed=False):
    out = []
    for p in pubnums:
        r = enrich_publication(p, reembed=reembed)
        out.append(r)
        print(f"  {p}: {r}")
    return out


if __name__ == "__main__":
    # demo: enrich the cross-lingual DE anchors that BigQuery left claim-less
    pubs = sys.argv[1:] or ["DE-202019005606-U1", "DE-102017106252-A1", "DE-4327663-A1"]
    enrich_final_set(pubs, reembed="--reembed" in sys.argv)
