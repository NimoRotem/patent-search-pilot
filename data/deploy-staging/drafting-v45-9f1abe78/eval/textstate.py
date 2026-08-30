"""What text do we actually hold for a publication, at the granularity that decides what it can do.

Shared by the text-condition funnel (plan step 3.1), the family-member inventory (3.2) and the
acquisition funnel (4), so all three speak the same vocabulary and a number from one can be joined
to a number from another.

WHY THESE LABELS AND NOT "in corpus" / "not in corpus"
------------------------------------------------------
"In corpus" is a boolean that hides the thing that matters. Measured on the dev gold set, 62% of
the references the corpus DOES hold are a title and an abstract. Those rank, they embed, they pass
a screen, and they ground almost nothing, so counting them as held overstates coverage by a factor
of three. The labels below are ordered by what the pipeline can DO with the record:

    absent                      no publications row at all
    metadata_only               a row, no chunk text of any kind
    title_only                  a title, nothing else indexed
    abstract_only               an abstract. Retrievable, not readable
    claims_only                 claims but no description
    partial_description         some description, under DESC_FULL characters
    full_description_and_claims claims plus description at or above DESC_FULL

DESC_FULL is deliberately not the 3,000 character floor used by gold_text_coverage.py. That floor
answers "can this be read at all"; this scale answers "what is missing", and the two questions want
different cut points. Both are reported so neither has to be inferred from the other.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import db  # noqa: E402
import pubnorm  # noqa: E402

#  A granted mechanical patent's description runs to tens of thousands of characters. 6,000 is
#  about the point below which a description is an excerpt rather than a document, and it is twice
#  the readability floor so the two labels cannot be confused.
DESC_FULL = int(os.environ.get("TEXTSTATE_DESC_FULL", "6000"))

ORDER = ["absent", "metadata_only", "title_only", "abstract_only", "claims_only",
         "partial_description", "full_description_and_claims"]

#  Which labels can support which use. Kept here so no caller has to re-derive it.
RETRIEVABLE = {"abstract_only", "claims_only", "partial_description",
               "full_description_and_claims"}
READABLE = {"partial_description", "full_description_and_claims"}


def label(row) -> str:
    """row: the dict returned by fetch(). -> one of ORDER."""
    if not row or not row.get("exists"):
        return "absent"
    if not (row["n_abstract"] or row["n_claim"] or row["n_para"] or row["n_whole"]):
        return "title_only" if row.get("has_title") else "metadata_only"
    if row["n_para"]:
        return ("full_description_and_claims"
                if row["para_chars"] >= DESC_FULL and row["n_claim"]
                else "partial_description")
    if row["n_claim"]:
        return "claims_only"
    if row["n_abstract"] or row["n_whole"]:
        return "abstract_only"
    return "title_only" if row.get("has_title") else "metadata_only"


def fetch(pubs):
    """-> {input_pub: {exists, has_title, n_abstract, n_claim, n_para, para_chars, total_chars,
    authority, publication_date, simple_family_id, state}}

    One query for the whole batch. Matching is on the normalised publication number on BOTH sides,
    because the gold set, the adapters and the corpus pad and punctuate identifiers differently and
    a mismatch here reads downstream as "we do not have this document".
    """
    want = {}
    for p in pubs:
        key = (p or "").strip()
        if key:
            want.setdefault(_norm(key), []).append(key)
    if not want:
        return {}

    out = {}
    keys = list(want)
    CHUNK = 500
    for i in range(0, len(keys), CHUNK):
        batch = keys[i:i + CHUNK]
        with db.cursor() as cur:
            cur.execute("""
                SELECT upper(regexp_replace(p.publication_number,'[^A-Za-z0-9]','','g')) k,
                       p.publication_number, p.country, p.publication_date, p.simple_family_id,
                       (p.title IS NOT NULL AND p.title <> '') has_title,
                       count(*) FILTER (WHERE ch.kind = 'abstract')       n_abstract,
                       count(*) FILTER (WHERE ch.kind LIKE 'claim%%')     n_claim,
                       count(*) FILTER (WHERE ch.kind = 'paragraph')      n_para,
                       count(*) FILTER (WHERE ch.kind = 'whole')          n_whole,
                       coalesce(sum(length(ch.text)) FILTER (WHERE ch.kind='paragraph'), 0) para_chars,
                       coalesce(sum(length(ch.text)), 0)                  total_chars
                  FROM publications p
                  LEFT JOIN chunks ch ON ch.publication_id = p.id
                 WHERE upper(regexp_replace(p.publication_number,'[^A-Za-z0-9]','','g'))
                       = ANY(%s)
                 GROUP BY 1,2,3,4,5,6""", (batch,))
            rows = {r["k"]: dict(r) for r in cur.fetchall()}
        for k in batch:
            r = rows.get(k)
            rec = ({"exists": True, "has_title": bool(r["has_title"]),
                    "n_abstract": r["n_abstract"], "n_claim": r["n_claim"],
                    "n_para": r["n_para"], "n_whole": r["n_whole"],
                    "para_chars": int(r["para_chars"]), "total_chars": int(r["total_chars"]),
                    "authority": r["country"] or (r["publication_number"] or "")[:2],
                    "publication_date": r["publication_date"],
                    "simple_family_id": r["simple_family_id"],
                    "publication_number": r["publication_number"]}
                   if r else
                   {"exists": False, "has_title": False, "n_abstract": 0, "n_claim": 0,
                    "n_para": 0, "n_whole": 0, "para_chars": 0, "total_chars": 0,
                    "authority": "", "publication_date": None, "simple_family_id": None,
                    "publication_number": None})
            rec["state"] = label(rec)
            for original in want[k]:
                out[original] = rec
    return out


def _norm(pub: str) -> str:
    """Normalised key. Uses pubnorm's canonical form when it can, then strips to alphanumerics."""
    try:
        c = pubnorm.canonical(pub) or pub
    except Exception:
        c = pub
    return "".join(ch for ch in c.upper() if ch.isalnum())


def authority_of(pub: str) -> str:
    p = (pub or "").strip().upper()
    return p[:2] if len(p) >= 2 and p[:2].isalpha() else ""


#  Authorities whose full text we can expect to obtain in English through a bulk route. Used by the
#  family-member inventory to separate "fetch an English member" from "genuinely foreign only".
ENGLISH_BULK = {"US", "EP", "WO", "GB", "AU", "CA", "IN"}
