"""Before a document is discarded, look for the member of its family that published EARLIER.

THE FINDING THIS EXISTS FOR, and counsel put it above everything else on the list.

The search read Schmalz's own DE 10 2024 105 114 A1, saw that it published 28 August 2025, after
their own 9 September 2024 priority date, and correctly kept it out of a US submission. It is the
only document in a search of 233 that teaches the one limitation nothing else reaches. What nobody
looked for is that Schmalz filed the SAME disclosure as a Gebrauchsmuster on the same day, and a
German utility model publishes on REGISTRATION rather than at eighteen months:

    DE 20 2024 100 869 U1, "Polschuh fuer einen Magnetgreifer und Magnetgreifer mit Polschuh",
    J. Schmalz GmbH, filed 23.02.2024, registered 12.03.2024, gazetted 18.04.2024

Six months BEFORE the priority date it is being cited against. Dead in the United States, where
102(b)(1)(A) very likely removes the applicant's own disclosure inside the grace year. Full EPC
Art. 54(2) and § 3(1) PatG prior art at the EPO and the DPMA, for novelty AND inventive step,
because neither has a general grace period. Lethal in Europe, and only if somebody goes looking.

SO: WHENEVER A DOCUMENT IS EXCLUDED, SWEEP ITS FAMILY ACROSS KIND CODES FIRST. Excluded as
self-collision, as later-published secret art the forum cannot reach, or as post-dating the
priority date: all three are reasons to look, because all three are statements about ONE
publication and a family is many. Utility models are the high-value case (DE `U1`, CN `U`,
KR `U`/`Y`, JP `U`), because they can publish twelve to eighteen months ahead of the corresponding
A publication of the very same disclosure.

WHAT THIS IS NOT. It is not a second retrieval engine and it does not read anything. It answers one
question, "is there an earlier-published member of this family", from the family data the app
already fetches and caches, and hands back the member with its kind code so the citation can be
bound to it. Reading it, if it is worth reading, is the practitioner's next move and the page says
so.
"""
from __future__ import annotations

import datetime
import os
import re
import traceback

import search_modes

#  Kind codes that mean "utility model" at the office that issues them. A utility model is
#  registered rather than examined, so it publishes on registration: months, not eighteen months.
#  That is why it is the member most likely to predate a priority date its A-publication sibling
#  cannot touch.
_UTILITY_MODEL = {
    "DE": ("U1", "U"),
    "CN": ("U", "U1", "Y", "Y1"),
    "KR": ("U", "U1", "Y", "Y1", "Y2"),
    "JP": ("U", "U1"),
    "TW": ("U", "U1"),
    "AT": ("U1",),
    "ES": ("U",),
    "IT": ("U1",),
}

UTILITY_MODEL_NOTE = (
    "A utility model publishes on registration rather than at eighteen months, so it is routinely "
    "the earliest publication of a disclosure by a year or more. It is full prior art wherever it "
    "published before the effective filing date.")


def is_utility_model(country, kind):
    return str(kind or "").upper() in _UTILITY_MODEL.get(str(country or "").upper()[:2], ())


def _as_date(v):
    if isinstance(v, datetime.date):
        return v
    s = str(v or "")
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    if not m:
        return None
    try:
        return datetime.date(*[int(x) for x in m.groups()])
    except ValueError:
        return None


def _bare(pub):
    return re.sub(r"[^A-Z0-9]", "", str(pub or "").upper())


# --------------------------------------------------------------------------- the members


def _from_ops(pub):
    """Publication-level family members from the EPO INPADOC family, or []. Never raises.

    `ops_family.fetch_family` collapses publications of one application into a single timeline
    entry, which is right for the card that shows a family's geographic spread and wrong here: the
    collapse is exactly where the kind code goes, and the kind code is the answer.
    """
    try:
        import ops_family
        import pubnorm
        #  The cache key is the corpus-canonical hyphenated form, and a bare "DE102024105114A1"
        #  raises rather than resolving. Normalise here, once, rather than teaching every caller.
        return ops_family.publication_members(pubnorm.canonical(pub) or pub) or []
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return []


_CORPUS_SQL = (
    "SELECT publication_number, country, kind_code, publication_date, filing_date, "
    "       earliest_priority_date "
    "  FROM publications "
    " WHERE simple_family_id IS NOT NULL AND simple_family_id <> '' "
    "   AND simple_family_id = (SELECT simple_family_id FROM publications "
    "                            WHERE replace(upper(publication_number),'-','') = ANY(%s) "
    "                            LIMIT 1) "
    " LIMIT 200")


def _from_corpus(pub):
    """Whatever the local corpus holds of the same simple family. Partial by construction: the
    corpus is seeded from one field's classification branches and a sibling in another office is
    frequently not in it at all. Used as the floor, never as the answer."""
    keys = {_bare(pub)}
    try:
        import pubnorm
        keys |= {_bare(c) for c in pubnorm.mongo_candidates(pub)}
    except Exception:                                                     # noqa: BLE001
        pass
    keys = sorted(k for k in keys if k)
    if not keys:
        return []
    try:
        import db
        with db.cursor() as cur:
            cur.execute(_CORPUS_SQL, (keys,))
            rows = cur.fetchall()
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return []
    out = []
    for r in rows:
        out.append({"pub": r["publication_number"],
                    "country": (r["country"] or "")[:2].upper(),
                    "kind": (r.get("kind_code") or "").upper(),
                    "pub_date": r["publication_date"],
                    "eff_date": r.get("earliest_priority_date") or r.get("filing_date")})
    return out


def _norm_member(m):
    """One member in the shape the sweep works in, whatever produced it."""
    pub = str(m.get("pub") or "")
    country = str(m.get("country") or pub[:2] or "").upper()[:2]
    kind = str(m.get("kind") or "").upper()
    return {
        "pub": pub,
        "country": country,
        "kind": kind,
        "pub_date": _as_date(m.get("pub_date")),
        "eff_date": _as_date(m.get("eff_date") or m.get("prio_date") or m.get("app_date")),
        "utility_model": is_utility_model(country, kind),
    }


def members(pub):
    """Every PUBLICATION of this document's family, with its kind code. -> ([member], source)

    The office family first, because it is the only source that knows about a member this corpus
    never ingested, and that is the whole point: the sibling worth finding is usually the one
    nobody indexed.
    """
    raw = _from_ops(pub)
    source = "ops"
    if not raw:
        raw = _from_corpus(pub)
        source = "corpus"
    out, seen = [], set()
    for m in raw:
        got = _norm_member(m)
        key = _bare(got["pub"])
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(got)
    return out, (source if out else "none")


# --------------------------------------------------------------------------- the sweep


def sweep(pub, subject_efd, own=False, forums=search_modes.FORUMS):
    """Is there a member of this family that published before `subject_efd`? -> dict

    -> {"pub", "checked", "source", "n_members", "earlier": [...], "best": member|None,
        "matrix": [...], "note": str}

    `best` is the EARLIEST-published qualifying member, because the earliest is the one no priority
    argument can reach. `matrix` is what that member is worth at each office, which is a separate
    question from whether it exists and the one that decides whether it is worth reading.
    """
    efd = _as_date(subject_efd)
    out = {"pub": pub, "checked": False, "source": "none", "n_members": 0,
           "earlier": [], "best": None, "matrix": [], "note": ""}
    if not pub or efd is None:
        out["note"] = ("The application's effective filing date is not known here, so no sibling "
                       "can be compared against it.")
        return out
    mem, source = members(pub)
    out["checked"] = source != "none"
    out["source"], out["n_members"] = source, len(mem)
    if not mem:
        out["note"] = ("No family could be resolved for this publication, so nothing rules out an "
                       "earlier-published sibling. Check the family by hand before discarding it.")
        return out
    here = _bare(pub)
    earlier = [m for m in mem
               if _bare(m["pub"]) != here and m["pub_date"] and m["pub_date"] < efd]
    earlier.sort(key=lambda m: (m["pub_date"], m["pub"]))
    out["earlier"] = earlier
    if not earlier:
        out["note"] = ("%d family member%s checked across kind codes; none published before %s."
                       % (len(mem), "" if len(mem) == 1 else "s", efd.isoformat()))
        return out
    best = earlier[0]
    out["best"] = best
    out["matrix"] = search_modes.forum_matrix(
        best["country"], best["pub_date"], best["eff_date"], efd, own=own, forums=forums)
    live = [m["forum"] for m in out["matrix"] if m.get("available")]
    out["note"] = (
        "%s%s published %s, which is BEFORE the application's effective filing date of %s. The "
        "publication excluded here is not the only publication of this disclosure. %s%s"
        % (best["pub"], " (a %s utility model)" % best["country"] if best["utility_model"] else "",
           best["pub_date"].isoformat(), efd.isoformat(),
           (UTILITY_MODEL_NOTE + " ") if best["utility_model"] else "",
           ("Prior art at: %s." % ", ".join(live)) if live
           else "It is still not prior art at any of the offices checked."))
    return out


#  How many excluded candidates one call will sweep. A family is cached on disk for ever, so the
#  second load of a page costs nothing; the cap is for the FIRST load, where each miss is a small
#  EPO call and a picker with two hundred rows must not become two hundred of them.
SWEEP_MAX = int(os.environ.get("FAMILY_SWEEP_MAX", "8"))
SWEEP_WORKERS = int(os.environ.get("FAMILY_SWEEP_WORKERS", "6"))


def sweep_excluded(cands, subject_efd, limit=None, is_excluded=None):
    """Sweep the families of the candidates this selection is throwing away. Mutates `cands`.

    Ordered by how much the document reads on, so the budget is spent on the exclusions that would
    have mattered. Every swept candidate gets `sibling`; one with an earlier-published member gets
    `sibling_alert` as well, which is what the page shows.

    Concurrent, because this runs on a page load and the lookups are independent I/O. Serial, the
    first render of a report with eight cold families waited for eight round trips in a row.
    """
    limit = SWEEP_MAX if limit is None else limit
    excluded = [c for c in (cands or []) if (is_excluded or _default_excluded)(c)]
    excluded.sort(key=lambda c: -int(c.get("reads_on") or 0))
    todo = excluded[:max(0, int(limit))]
    if not todo:
        return cands

    def one(c):
        try:
            got = sweep(c.get("pub"), subject_efd, own=bool(c.get("co_owned")))
        except Exception:                                                 # noqa: BLE001
            traceback.print_exc()
            return
        c["sibling"] = got
        c["sibling_alert"] = bool(got.get("best"))

    if len(todo) == 1:
        one(todo[0])
        return cands
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(SWEEP_WORKERS, len(todo))),
                            thread_name_prefix="family-sweep") as ex:
        list(ex.map(one, todo))
    return cands


def _default_excluded(c):
    """A candidate the selection will not offer, for a reason a sibling could answer.

    Not readable and already of record are exclusions too, and a sibling does not help with
    either: an unread abstract stays unread whichever member it belongs to, and a document the
    examiner already has is already in front of them.
    """
    return bool(c.get("co_owned")) or c.get("basis") in ("secret", "not_art")
