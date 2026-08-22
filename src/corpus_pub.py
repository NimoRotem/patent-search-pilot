"""Match a fetched publication number to the row the corpus already holds.

THE FAILURE THIS EXISTS TO CATCH
--------------------------------
Workstream C writes `parsed/{PUBLICATION}/{provider}.json` with the number spelled the way its
providers spell it, COMPACT: `AT10718U1`, `DE10023344C2`, `ITBO20090216A1`. `sources_docstore`
does the same: `CN111923076A`. The corpus spells the same publication HYPHENATED:
`AT-10718-U1`, `DE-10023344-C2`, `IT-BO20090216-A1`.

An equality join on the raw string therefore matches NOTHING, and the consequence is not an empty
result, which somebody would notice. It is worse in two ways at once:

  * every fetched document is handed a NEGATIVE surrogate id, so a publication the corpus holds is
    staged as one it does not, and workstream F cannot join a single staged row back to a real
    publication;
  * `publications_with_paragraphs()`, the query that keeps this pipeline off
    `patents-desc-backfill`'s population, is asked about ids that do not exist, answers "none of
    them", and both jobs embed the same descriptions.

Measured 2026-08-22 on the live database: all 51 documents already staged by the interrupted run
carry a surrogate id, and 100% of them are in `publications` under the hyphenated spelling.

HOW IT MATCHES
--------------
`publications` carries a functional index that is exactly the compact form:

    ix_pub_number_norm ON publications (upper(regexp_replace(publication_number,
                                                             '[^A-Za-z0-9]', '', 'g')))

so the lookup below is an index scan on 5M rows rather than the sequential scan the brief forbids.
`SQL_NORM` is that expression written character for character; changing one of them without the
other turns this into a full scan of `publications` on a box that is serving production.

The US pre-grant ladder is still needed on top of it, because the two spellings differ by more
than punctuation: BigQuery drops leading zeros from the 7-digit serial, so Google's
`US20190168875A1` is the corpus's `US-2019168875-A1` and neither compacts to the other.
`pubnorm.mongo_candidates` already computes that ladder and is reused rather than re-derived.
"""
from __future__ import annotations

import re

import pubnorm

#  Character for character the expression behind ix_pub_number_norm. See the module docstring.
SQL_NORM = "upper(regexp_replace(publication_number, '[^A-Za-z0-9]', '', 'g'))"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def compact(pub) -> str:
    """The corpus's normalised spelling: uppercase, letters and digits only."""
    return _NON_ALNUM.sub("", str(pub or "")).upper()


def corpus_keys(pub) -> list:
    """-> the compact spellings to try for one fetched number, most likely first.

    The number as given always comes first: it is right for every office except US pre-grant, and
    trying a padding variant ahead of an exact match is how a document gets attached to its
    neighbour. `pubnorm.mongo_candidates` returns [] for a number its regex cannot parse
    (`ATA24670A`, `ITBO20090216A1`, `BRPI0701963B1`, 196 of C's 13,872 objects on 2026-08-22), and
    those are exactly the ones the plain compact form already resolves.
    """
    first = compact(pub)
    if not first:
        return []
    out = [first]
    for cand in pubnorm.mongo_candidates(pub):
        c = compact(cand)
        if c and c not in out:
            out.append(c)
    return out


def resolve(conn, pubs) -> dict:
    """-> `{publication_number_as_given: {"id": int, "publication_number": corpus_spelling}}`.

    Only publications the corpus holds appear. Everything else is the caller's surrogate case.
    `min(id)` because `publications` is UNIQUE on `(publication_number, kind_code)` and not on the
    number alone, so one number can carry more than one row: two workers must pick the same one.
    """
    pubs = [p for p in dict.fromkeys(pubs) if p]
    if not pubs:
        return {}
    keys, wanted = {}, set()
    for p in pubs:
        k = corpus_keys(p)
        keys[p] = k
        wanted.update(k)
    if not wanted:
        return {}
    with conn.cursor() as cur:
        cur.execute(f"SELECT {SQL_NORM} AS norm, publication_number, min(id) AS id "
                    f"FROM publications WHERE {SQL_NORM} = ANY(%s) "
                    f"GROUP BY 1, 2 ORDER BY 3", (sorted(wanted),))
        rows = cur.fetchall()
    by_norm = {}
    for r in rows:
        by_norm.setdefault(r["norm"], {"id": r["id"], "publication_number": r["publication_number"]})
    out = {}
    for p, cands in keys.items():
        for c in cands:
            hit = by_norm.get(c)
            if hit:
                out[p] = dict(hit)
                break
    return out
