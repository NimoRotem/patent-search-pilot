"""Read a reference in full AFTER the search finished, on demand, and re-rank on the result.

WHY. A search reads the head of its screen in full and screens the rest. An unread card therefore
shows a dash and the sentence "no score until it is read in full", which is honest and is a
dead end: the one thing a reader wants when they disagree with the ranking is to make the system
read the document they are looking at. There was no way to ask.

Two shapes, and the second is the one that matters:

  ONE REFERENCE   the reader has spotted something the screen put low. Reading it produces a real
                  grounded score for that card, in place of the dash.
  EVERY UNREAD ONE   the ranking itself is what is in question. Reading them all and re-scoring
                  every reference against the widened evidence is a different answer, not a longer
                  list: rarity is measured over the references actually charted, so a disclosure
                  that looked distinctive across 45 documents may not be across 200.

RE-SCORING IS THE POINT, NOT A SIDE EFFECT. `deep_rank.score_reference` divides the rarity-weighted
mass a reference grounds by the mass available, and `deep_rank.rarity` computes those weights over
the charted set. Adding charts changes the denominator for everyone, so every score is recomputed
here, not just the new ones. Doing otherwise would leave two scores on one page computed against
two different corpora of evidence, which is the kind of number that looks fine and compares
nothing.

NOTHING IS WRITTEN UNTIL EVERYTHING SUCCEEDS. The report json and the deep json have to agree: the
card reads its score from `report["deep_rank"]["by_pub"]` and the grid reads its cells from
`<slug>.deep.json`. A half-applied read would show a score on a card whose chart is not in the
grid. Both files are written at the end, or neither is.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

#  Concurrent full reads. Each is up to fourteen READ-tier prompts carrying the whole document, so
#  this is the same shape as the reading wave in a search and is bounded for the same reason.
WORKERS = int(os.environ.get("READ_MORE_WORKERS", "8"))
#  A ceiling on one request. It was 120, sized against "read everything on a 60-card page", but the
#  page was never the pool: a quick search screens 500 to 600 candidates and a deep one a couple of
#  thousand, and the reading is what a reader is here to buy. 250 is the same order as what the
#  deep tier reads unattended (~220), it is bounded, and the page states both the cap and the pool
#  so a request that cannot take everything says how much it did take.
MAX_PER_CALL = int(os.environ.get("READ_MORE_MAX", "250"))

_JOBS: dict = {}
_LOCK = threading.Lock()


def job(slug):
    with _LOCK:
        j = _JOBS.get(slug)
        return dict(j) if j else None


def _set(slug, **kw):
    with _LOCK:
        j = _JOBS.setdefault(slug, {})
        j.update(kw)
        return dict(j)


def running(slug) -> bool:
    return (job(slug) or {}).get("state") == "running"


def _write_atomic(path, payload):
    tmp = str(path) + ".tmp-readmore"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, default=str)
    os.replace(tmp, path)


def rescore_all(report, deep, features, claim_labels):
    """Recompute every reference's score over the charted set. Mutates `report` and `deep`.

    Returns the number of references that now carry a full reading.
    """
    import deep_rank
    charts = [r for r in (deep.get("references") or []) if isinstance(r, dict)]
    if not charts:
        return 0
    rar = deep_rank.rarity(charts, features, claim_labels)
    #  `leaders` returns a MAP, {pub: [(feature, idf)]}, and `score_reference` takes that pub's
    #  own list. Passing the map meant iterating its KEYS, so every reference raised
    #  `ValueError: too many values to unpack` and the `continue` below swallowed all of them:
    #  the job reported "done", both files were written, and `by_pub` was never touched. Two
    #  rounds of "the read runs but the score does not persist" were this line.
    #
    #  Built exactly the way `deep_rank.run` builds it, claim leaders included, so an on-demand
    #  read scores a reference identically to the search that would have read it.
    lead_map = deep_rank.leaders(charts, rar)
    for _pub, _leads in deep_rank._claim_leaders(charts, rar).items():
        lead_map.setdefault(_pub, []).extend(_leads)
    dr = report.setdefault("deep_rank", {})
    by_pub = dr.setdefault("by_pub", {})
    unread = dr.setdefault("unread", {})
    scored = []
    for ref in charts:
        pub = ref.get("pub")
        if not pub:
            continue
        #  NO `continue` HERE. Scoring is the whole job: a reference that cannot be scored is a
        #  bug, not a row to skip, and skipping it silently is what let the shape error above
        #  survive two rounds of testing. Raise, and let the caller mark the job failed with
        #  nothing written.
        score, detail = deep_rank.score_reference(ref, rar, lead=lead_map.get(pub, ()))
        prev = by_pub.get(pub) or {}
        row = dict(prev)
        #  `detail` is authoritative, not this module's own arithmetic. `score_reference` already
        #  decides what "read in full" means (method is llm AND at least one paragraph or claim
        #  was actually read) and returns it, and a second definition here got it wrong in the
        #  generous direction: counting `method == "llm"` alone called all 76 references read
        #  where the search that produced them counted 39. A number on a report that is easier to
        #  earn than the one it replaces is the one kind of drift worth guarding against.
        row.update({
            "score": score,
            "read_in_full": bool(detail.get("read_in_full")),
            "n_disclosed": detail.get("n_disclosed", 0),
            "n_partial": detail.get("n_partial", 0),
            "n_features": len(features),
            "covered": detail.get("covered") or [],
            "chars_read": detail.get("chars_read", ref.get("chars", 0)),
            #  CHARTED BUT NOT READ. A reference the corpus holds only a title and an abstract for
            #  can be charted and cannot be READ, so asking again will always give the same
            #  answer. Recorded so the card can say that instead of offering the button a second
            #  time, which is the one outcome that would make this control feel broken.
            #  THERE WAS NOTHING TO READ, which is not the same as "was not read". Deriving
            #  this from `read_in_full` marked all 36 charted-but-unread references as having no
            #  full text and took their "read in full" buttons away, which is both wrong and the
            #  generous direction: it turns "we did not read it" into "it cannot be read".
            #  `method` is the reader's own verdict on whether text existed at all.
            "no_text": ref.get("method") != "llm",
        })
        #  `why` is the sentence under the score. deep_rank writes it from the same detail, and a
        #  stale one from the screen would describe a document that has since been read.
        try:
            row["why"] = deep_rank._why(ref, detail)
        except Exception:                                                 # noqa: BLE001
            row.pop("why", None)
        by_pub[pub] = row
        unread.pop(pub, None)
        scored.append((score, pub))
    scored.sort(reverse=True)
    for i, (_s, pub) in enumerate(scored, 1):
        by_pub[pub]["rank"] = i
    #  The header counts the page prints.
    dr["charted"] = len(charts)
    dr["read_in_full"] = sum(1 for v in by_pub.values() if v.get("read_in_full"))
    deep["n_references"] = len(charts)
    deep["n_analysed"] = dr["read_in_full"]
    return dr["read_in_full"]


def start(slug, pubs, report_path, deep_path, report, view, titles=None):
    """Read `pubs` in full in a background thread, then rescore everything. -> the job dict."""
    import deep_analysis
    pubs = [p for p in dict.fromkeys(pubs or []) if p][:MAX_PER_CALL]
    if not pubs:
        return _set(slug, state="idle", msg="Nothing to read.")
    if running(slug):
        return job(slug)
    features, claims, qd = deep_analysis.subject_material(report, view)
    claim_items = claims or []
    claim_labels = [c.get("label") for c in claim_items if isinstance(c, dict) and c.get("label")]
    titles = titles or {}

    _set(slug, state="running", done=0, total=len(pubs), msg="Reading %d reference%s in full"
         % (len(pubs), "" if len(pubs) == 1 else "s"), error=None, started=time.time())

    def work():
        charts = []
        try:
            hints = None
            try:
                hints = deep_analysis.concept_expansions(features, qd)
            except Exception:                                             # noqa: BLE001
                hints = None

            def one(pub):
                try:
                    return deep_analysis.analyse_reference(
                        pub, features, claim_items, title=titles.get(pub, ""), hints=hints)
                except Exception:                                         # noqa: BLE001
                    traceback.print_exc()
                    return None

            with ThreadPoolExecutor(max_workers=min(WORKERS, len(pubs))) as ex:
                for i, got in enumerate(ex.map(one, pubs), 1):
                    if got:
                        charts.append(got)
                    _set(slug, done=i, msg="Read %d of %d in full" % (i, len(pubs)))

            if not charts:
                _set(slug, state="error", msg="", error="None of them could be read in full.")
                return

            with open(deep_path) as fh:
                deep = json.load(fh)
            have = {r.get("pub"): i for i, r in enumerate(deep.get("references") or [])}
            refs = list(deep.get("references") or [])
            for c in charts:
                pub = c.get("pub")
                if pub in have:
                    refs[have[pub]] = c
                else:
                    refs.append(c)
            deep["references"] = refs

            _set(slug, msg="Re-scoring every reference against the widened evidence")
            n_read = rescore_all(report, deep, features, claim_labels)

            #  BOTH FILES OR NEITHER. See the module docstring.
            _write_atomic(deep_path, deep)
            _write_atomic(report_path, report)
            #  AND DROP THE RENDERED VIEW. `webapp._write_report` unlinks these whenever it
            #  publishes, because the report page serves a cached `<slug>.view.json` and a card
            #  reads its score from there. Writing the report without doing the same leaves the
            #  new scores on disk and the old ones on screen, which is indistinguishable from
            #  the write having failed.
            for sib in (".view.json", ".detail-preview.json"):
                try:
                    os.remove(str(report_path).replace(".json", sib))
                except OSError:
                    pass
            #  SAY WHICH ONES COULD NOT BE READ, by name. "76 read in full" over a request for
            #  one document that turned out to have no text is a true sentence that answers a
            #  different question from the one that was asked.
            asked = set(pubs)
            no_text = sorted(c.get("pub") for c in charts
                             if c.get("pub") in asked and c.get("method") != "llm")
            msg = "%d of %d read end to end. Every reference re-scored." % (
                len(asked) - len(no_text), len(asked))
            if no_text:
                msg += ("  %s could not be read: this corpus holds only a title and an abstract "
                        "for %s, so %s charted from that and carries no full-text score."
                        % (", ".join(no_text[:4]) + (" and %d more" % (len(no_text) - 4)
                                                     if len(no_text) > 4 else ""),
                           "them" if len(no_text) > 1 else "it",
                           "they" if len(no_text) > 1 else "it"))
            _set(slug, state="done", done=len(pubs), no_text=no_text, msg=msg)
        except Exception as exc:                                          # noqa: BLE001
            traceback.print_exc()
            _set(slug, state="error", error=str(exc)[:200], msg="")

    threading.Thread(target=work, daemon=True, name="read-more-%s" % slug).start()
    return job(slug)
