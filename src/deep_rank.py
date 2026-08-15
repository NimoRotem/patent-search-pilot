"""Rank by what the references ACTUALLY DISCLOSE, read in full, quote by quote.

MEASURED REASON THIS MODULE EXISTS (RECALL_STUDY_2026-08-02.md)
--------------------------------------------------------------
The report used to be ordered by an LLM score computed from a 900-character snippet
(``relevancy.py`` over ``rerank_listwise._matched_text``), while a SEPARATE stage
(``deep_analysis``) read the top 50 references in full and then fed a tab. The ordering signal was
the weakest text in the pipeline and the strongest one was thrown away.

On the case that prompted this rebuild, the same model on the same reference:

    US-10625955-B2   from the 900-char snippet ... 45      from its full text ... 85
    US-12115659-B1   from the 900-char snippet ... 60      from its full text ... 85
    DE-3724659-A1    never scored (rank 244)              from its full text ... 65-75

and the text budget is monotonic: 900 chars -> 0, all claims (18.5k) -> 75, full text (144k) -> 85.

So the reading becomes the ranking. Three stages:

  1. SCREEN, cheaply and widely. Every candidate the retrieval produced, in batches, on title +
     claims + abstract. Measured: 300 candidates in 12 calls and 25 seconds. This is what lifted
     DE-3724659-A1 from retrieval rank 244 to screen rank 8.
  2. READ, in full, the head of that screen. ``deep_analysis.analyse_reference`` already does this
     properly: every cell carries a verbatim quote, the quote must pass ``grounding.grounded``,
     the location is resolved BY CODE, and every "disclosed" is put to an independent refuter.
  3. SCORE by GROUNDED, RARITY-WEIGHTED evidence, never by a free-form number.

WHY RARITY WEIGHTING, AND WHY NOT A FREE-FORM SCORE
---------------------------------------------------
A free-form 0-100 read of the full text is not safe on its own: measured, it gave **85** to nine
Soviet-era records that hold ZERO characters of text in this corpus, scoring them off the title
alone and inventing 8 to 12 quotes each. Counting only quotes that pass the grounding gate gives
them 0, which is correct.

And not every feature is worth the same. Across 60 charted references for the vacuum-gripper case,
"portable vacuum gripper with a rigid base element" was disclosed by 34 of 60 and
"bracing structure protrudes less than the vacuum seal element" by 9 of 60. The second is what a
novelty attack turns on. Weighting each grounded feature by log(N/df) over the charted set makes
the distinctive disclosure count, and it is computed from the evidence rather than declared.

MEASURED RESULT, end to end on the live candidate list with retrieval untouched:

    US-10625955-B2   card 11 (score 45)          ->  rank 3   (10 of 12 features grounded)
    DE-3724659-A1    rank 244, never shown       ->  rank 18  (6 of 12, incl. both bracing rows)
    21 of the new top 25 had never appeared on the page, all from inside the top 300.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import db
import claim_rescue
import limitations as limmod
import coverage_rank
import deep_analysis
import llm
import oracle

VERSION = 1

#  How many of the retrieval's ranked families get a cheap screen.
#
#  MEASURED: on a real examiner citation list, ELEVEN of twenty-three cited families were retrieved
#  and then never looked at, because they sat at fusion positions 625 to 5,731 and the screen only
#  read the first 600 of 7,328. The screen is the cheapest stage in the pipeline by a wide margin
#  (600 candidates measured at 11 s and 24 model calls), so a depth that leaves most of the ranked
#  list unexamined is the wrong economy. 2,500 is ~100 calls and ~45 s.
#  2,500, NOT more, and this was measured the hard way over eight runs of the same search.
#  Widening the screen to 5,000 and the retrieval publication cap to 6,000 lowered top-50 recall
#  on a real examiner citation list from 7 families to 4-5, three runs in a row, while the two
#  narrower runs scored 7 twice. Every downstream cut is a fixed size, so a wider pool is more
#  competition at each of them; depth past this point buys reading nobody sees.
SCREEN_TOP = int(os.environ.get("DEEP_RANK_SCREEN_TOP", "2500"))
#  Characters of candidate text per screened candidate. This is the lever that keeps a deep screen
#  affordable: adding the CPC symbol and a description fallback pushed a 2,500-candidate screen
#  from 32 s to 163 s purely on prompt size. The screen is deciding ONE thing, whether the document
#  is worth reading, and a thousand characters of abstract or first claims settles that; the
#  reading stage is where the whole document gets read.
SCREEN_CHARS = int(os.environ.get("DEEP_RANK_SCREEN_CHARS", "1600"))
SCREEN_BATCH = int(os.environ.get("DEEP_RANK_SCREEN_BATCH", "25"))
SCREEN_WORKERS = int(os.environ.get("DEEP_RANK_SCREEN_WORKERS", "6"))

#  How many get read IN FULL. Measured ~1.15 references/second at 14 workers, so 300 is ~4-5
#  minutes. Raised from 150 because the screen now looks at 2,500 rather than 600 candidates: on a
#  real examiner citation list, seven cited families were screened at 60-80 and then never read,
#  because the top-150 cut landed in the high 80s.
CHART_TOP = int(os.environ.get("DEEP_RANK_CHART_TOP", "360"))
#  A THRESHOLD, not only a slice. "Worth reading" is a judgement the screen already made on a
#  0-100 scale, so a fixed top-N throws away its answer and substitutes an arbitrary cut: measured,
#  the top-300 cut landed at a screen score of 75-80, and cited references the screen had rated 70
#  were never read. Everything at or above this is read, up to the hard ceiling.
#  MEASURED, and it is not the obvious direction: reading MORE lowered recall at the top 50.
#  Charting 504 references instead of 344 did not improve the order within the read set, it only
#  made the 50-card cut harder, and cited references that had been on the page at chart rank 30-47
#  fell to 54-184. The read set is a shortlist for a fixed-size page, so widening it past what the
#  page can show trades visible recall for invisible reading.
#  Lowered 75 -> 70: the paragraph above records that "cited references the screen had rated 70
#  were never read", and 70 is precisely where the old threshold sat. Read them.
CHART_MIN_SCREEN = int(os.environ.get("DEEP_RANK_CHART_MIN_SCREEN", "70"))
#  Raised 350 -> 420, AND `_DISPLAY_TOP` was raised with it (webapp), because the measurement
#  above is specifically that widening the read set while the PAGE stays a fixed size trades
#  visible recall for invisible reading. The two have to move together or not at all. This also
#  widens the on-demand full-text recovery, which is bounded by exactly this number: a reference
#  the pipeline was never going to read is one it never fetches text for.
CHART_TOP_MAX = int(os.environ.get("DEEP_RANK_CHART_TOP_MAX", "420"))
#  Raised 18 -> 24 to pay for the extra feature batch per reference (deep_analysis.FEATURE_BATCH).
#  The stage is bound by Vertex round-trips, not by this box's four cores.
CHART_WORKERS = int(os.environ.get("DEEP_RANK_CHART_WORKERS", "24"))

#  Always read the head of the RETRIEVAL order regardless of what the screen thought, so a screen
#  miss can never cost a reference the old pipeline would have shown. This is not paranoia: the
#  screen scored US-10625955-B2 **0, three times out of three**, because its abstract in the
#  corpus is a different patent's abstract (upstream: patents.google.com shows the same wrong
#  abstract). A reference the retrieval ranked 15th must not be lost to a bad abstract.
ALWAYS_CHART_RETRIEVAL_HEAD = int(os.environ.get("DEEP_RANK_HEAD", "60"))
#  How far down the retrieval order to rescue candidates the screener was shown no text for.
#  RAISED 400 -> the whole screen depth. 400 was a guess at where useful art stops and it was
#  wrong by a factor of four on a real subject: the reference an attorney called the most
#  comprehensive match sat at retrieval rank 1,817 with nothing but a title. BLIND_RESCUE_MAX is
#  what bounds the cost; the reach should not also be doing that job, because a reach limit
#  silently decides that art beyond it does not exist.
BLIND_RESCUE = int(os.environ.get("DEEP_RANK_BLIND_RESCUE", str(SCREEN_TOP)))
BLIND_RESCUE_MAX = int(os.environ.get("DEEP_RANK_BLIND_RESCUE_MAX", "60"))

#  Evidence weights. "disclosed" survived the refuter; "partial" is the model's own hedge;
#  "uncertain" is a "disclosed" that an independent refuter would not confirm.
#
#  "uncertain" is deliberately NOT near-zero. The refuter is instructed to default to refuted=true
#  when unsure, because its job is to protect a legal chart from overclaim; that is the right bias
#  for a chart and the wrong one for a ranking. An "uncertain" row still means a real, located,
#  verbatim quote exists that a first reading called a disclosure. Measured on the live rebuild:
#  at 0.25 a reference that plainly discloses "portable or hand-held vacuum gripper" and grounds
#  it at claim 1 was scored as if it barely mentioned it, and the whole list compressed.
_W = {"disclosed": 1.0, "partial": 0.55, "uncertain": 0.5, "absent": 0.0}
#  Credit for being the BEST disclosure of a RARE feature. A reference that teaches one thing
#  almost nothing else teaches is what an inventive-step attack is built on: DE-3724659-A1 (1989,
#  9,160 characters) discloses 1 of 12 features and is the SECOND-best disclosure of the
#  characterising one out of 183 references read. Ranked on coverage alone it is off the page;
#  ranked with this credit it is on it, and the card says which feature it leads on.
LEAD_WEIGHT = float(os.environ.get("DEEP_RANK_LEAD_WEIGHT", "0.7"))
LEAD_DEPTH = int(os.environ.get("DEEP_RANK_LEAD_DEPTH", "3"))
#  How much of the displayed score is the reader's HOLISTIC judgement of the document, as opposed
#  to the rarity-weighted count of grounded cells.
#
#  Both are needed. The count alone is conservative by construction: the chart's refuter is told to
#  default to "refuted" and the prompt is told that "absent" is the expected answer, which is right
#  for a legal artefact and compresses a ranking. Measured, a reference an examiner-style read
#  scored 85 grounded only 2 clean "disclosed" cells. The holistic number alone is unsafe: it gave
#  85 to records with ZERO characters of text. So the holistic number is GATED on the reference
#  having been read AND having grounded at least one quote, and then blended.
OVERALL_WEIGHT = float(os.environ.get("DEEP_RANK_OVERALL_WEIGHT", "0.45"))
#  How much text a reference had to have for a grounded-coverage figure to mean what it says.
#
#  MEASURED, and this is the same length bias that had to be removed from retrieval: coverage is a
#  proportion whose denominator assumes the document had a CHANCE to disclose every feature. A
#  9,160-character document physically cannot ground twelve features, so coverage is partly a
#  measure of length. On a real examiner citation list the document with the HIGHEST screen score
#  of 2,500 candidates (95) came 67th on coverage, behind long documents its own reader had scored
#  lower, purely because there was less of it to quote.
#
#  So the coverage term is weighted by how much text there actually was to ground in, and what it
#  gives up goes to the two judgements that do not depend on length: the reader's holistic verdict
#  on the text that exists, and the screen's independent read of the bibliographic core. All three
#  are still gated on the reference having been read at all.
DEPTH_FULL_CHARS = int(os.environ.get("DEEP_RANK_DEPTH_FULL", "60000"))
COVERAGE_WEIGHT = float(os.environ.get("DEEP_RANK_COVERAGE_WEIGHT", "0.55"))
#  How the weight coverage gives up is split between the reader's verdict and the screen.
OVERALL_SHARE = float(os.environ.get("DEEP_RANK_OVERALL_SHARE", "0.60"))
#  How much of its score a reference keeps when nothing could be read of it. 1.0 would ignore
#  depth entirely; 0 would rank an abstract-only record at zero however apt it is. Swept over both
#  benchmark subjects at 0.30 / 0.45 / 0.60 / 0.75 against the sum of the cited references' ranks:
#  0.75 was best (576, against 836 for the old depth-reweighting), and it is also the gentlest,
#  which matters because a large share of the art examiners cite is abstract-only here.
DEPTH_CONFIDENCE_FLOOR = float(os.environ.get("DEEP_RANK_DEPTH_FLOOR", "0.75"))
#  A reference charted against the uploaded patent's own claims gets that counted too, at a
#  discount: a claim is a conjunction of limitations, so a "partial" there is weaker evidence
#  about the whole reference than a "partial" on a single feature.
_CLAIM_WEIGHT = 0.75

#  A reference we could not read is ranked in its OWN TIER, after every reference whose full text
#  was quoted, and shown in its own labelled section.
#
#  This started as a score cap, and the cap was a mistake that only a live run exposed: a ceiling
#  is a MAGNET. Every abstract-only record whose screen score cleared the cap scored exactly the
#  cap, so they formed a plateau, and because that plateau sat above the natural range of
#  genuinely-read documents it evicted them. Measured on a real run: 44 of the 50 displayed
#  references were abstract-only records sitting at exactly 70, and four cited references that had
#  been read in full with grounded quotes were pushed off the page by documents nobody could read.
#
#  Two incomparable things were being sorted into one list. They are now two lists. The cap
#  survives only as the ceiling on what a card may DISPLAY, so a screen score is never presented
#  as if it were evidence.
#  Disclosures charted per reference. The old cap was deep_analysis.MAX_FEATURES=20, sized for
#  a 12-item element summary; a real disclosure list runs to 60+ and the checklist is the
#  point, so this is its own number. Each one lengthens every chart prompt, so it is a real
#  cost and not free to raise.
#  Raised 40 -> 48 only once the truncation below it was fixed. Until then the extra items were
#  worse than useless: deep_analysis charted the first 20 and rarity() still counted every one of
#  them in the denominator, so raising the cap lowered every score and reported the difference as
#  "nothing discloses this".
DISCLOSURE_CAP = int(os.environ.get("DEEP_RANK_DISCLOSURE_CAP", "48"))
#  How many of the read references get the SECOND, concept-led look at the features their first
#  reading returned nothing for (deep_analysis.reread_absent). Bounded to the head because it is
#  one extra model call per reference and only the head is displayed, charted and argued from.
CONCEPT_PASS_TOP = int(os.environ.get("DEEP_RANK_CONCEPT_PASS_TOP", "120"))
#  Order by marginal contribution to disclosure coverage rather than by score alone.
COVERAGE_ORDER = os.environ.get("DEEP_RANK_COVERAGE_ORDER", "1") != "0"
#  How much a CLAIM counts in the coverage objective, relative to a disclosure. 1.0 = the same.
CLAIM_COVERAGE_WEIGHT = float(os.environ.get("DEEP_RANK_CLAIM_COVERAGE_WEIGHT", "1.0"))
#  The window the report page renders. deep_rank must not import webapp, so this mirrors
#  webapp._DISPLAY_TOP; it is what the claim-coverage guarantee promotes into.
DISPLAY_WINDOW = int(os.environ.get("DEEP_RANK_DISPLAY_WINDOW", "60"))
UNREAD_SCORE_CAP = int(os.environ.get("DEEP_RANK_UNREAD_CAP", "70"))

#  ON-DEMAND TEXT, fetched for the references we are ABOUT TO READ and persisted to the corpus.
#
#  84% of this corpus is abstract-only, and a reference with no text cannot be ranked on evidence
#  however relevant it is: it can only be listed. Measured against a real examiner citation list,
#  twelve of the thirteen families that never reached the ranked list were abstract-only here.
#
#  The recovery chain is local Mongo -> official EPO OPS -> direct Google Patents -> ScrapingBee
#  -> SerpApi. Because the quota'd provider is now last rather than the gate, every reference the
#  reader selected is eligible. It remains bounded by CHART_TOP_MAX: the pipeline never fetches a
#  document it was not already about to read, and the persisted text benefits every later search.
ENRICH_TOP = int(os.environ.get("DEEP_RANK_ENRICH_TOP", str(CHART_TOP_MAX)))
ENRICH_WORKERS = int(os.environ.get("DEEP_RANK_ENRICH_WORKERS", "8"))
#  TEXT FETCHED BEFORE THE SCREEN, for candidates the corpus holds nothing readable for.
#
#  Without this the pipeline is circular: no text -> the screener scores what little there is ->
#  the document is not chosen to be read -> its text is never fetched. Measured, it cost the
#  single most on-point reference an attorney cited for a live subject (US-2966138-A: 0 claims,
#  0 paragraphs here, screened 10 of 100, retrieval rank 1,817, never read).
#
#  Bounded hard, because this runs over the whole screen depth rather than the read set: these are
#  network fetches against the recovery chain and the budget is the point. Set 0 to disable.
PRESCREEN_ENRICH_TOP = int(os.environ.get("DEEP_RANK_PRESCREEN_ENRICH", "400"))
#  THE ORPHAN-CLAIM RESCUE, after the reading. Set to 0 to skip it.
#
#  Everything above this line searches, screens and reads against the invention AS A WHOLE, and the
#  claim table falls out of the reading as a by-product. That is why a claim can end a search with
#  no prior art against it while the report shows four hundred references: nothing was ever
#  searched FOR it. See claim_rescue.
RESCUE_CLAIMS = os.environ.get("DEEP_RANK_RESCUE_CLAIMS", "1") != "0"

_STOP = set((
    "a an the of and or to for with in on at by is are be as from that this it its which such "
    "said have has having comprising comprises method system device apparatus assembly unit means "
    "portion member element part first second least one two including includes provided plurality "
    "wherein thereof therein configured surface end side body according present invention"
).split())

_SCREEN_SYS = (
    "You are a patent examiner SCREENING candidate references against a target invention. You are "
    "deciding ONE thing: how likely is it that reading this document IN FULL would turn up "
    "material prior art for the invention. You are not deciding whether it anticipates anything.\n"
    "\n"
    "Score each candidate 0-100 on that question: 90-100 = almost certainly material, it looks "
    "like the same device or mechanism; 70-89 = very likely worth reading, the same problem solved "
    "a similar way; 40-69 = plausibly worth reading, same field and some overlap; 1-39 = same "
    "broad area but a different problem or solution; 0 = unrelated.\n"
    "\n"
    "IMPORTANT, and this is where a screener usually goes wrong. The amount of text you are shown "
    "varies enormously and carries NO information about relevance. Many of these records are old, "
    "foreign, or utility models, and all that exists for them here is a title, a classification "
    "and one short abstract. A forty-word abstract CANNOT demonstrate several elements, so scoring "
    "it low for that reason is scoring it for being old rather than for being irrelevant, and "
    "examiners cite exactly those documents. Judge what the document plainly IS, on the evidence "
    "you have: if a two-line abstract and a classification say it is a suction lifting device and "
    "the invention is a suction lifting device, that is worth reading in full. Reserve the low "
    "bands for candidates that are about something else.\n"
    "\n"
    "Never score on the title alone or on shared words, and never reward length.\n"
    'Return ONLY JSON {"results":[{"id":<batch number>,"score":<0-100>}]} with one entry per '
    "candidate and every batch id exactly once."
)


def _tokens(s):
    return {w for w in re.findall(r"[a-z]{5,}", (s or "").lower()) if w not in _STOP}


def abstract_is_trustworthy(abstract, title, claim_text) -> bool:
    """False when the stored abstract cannot belong to this publication.

    ``publications.abstract`` for US-10625955-B2 ("Electric vacuum suction lifter") is the
    abstract of a touch display device with optical adhesive. That is UPSTREAM, not an ingest bug:
    patents.google.com/patent/US10625955B2 shows the same wrong abstract, and both members of the
    family carry it. Prevalence is low (0 of 1,500 sampled US publications had an abstract sharing
    no content word with their own title or claim 1), but the cost when it happens is total: the
    screen scored that patent 0 three times out of three.

    An abstract that shares no content word with either the title or the claims is therefore not
    shown to the screener. The claims are still shown, so the candidate is still judged.
    """
    a = _tokens(abstract)
    if not a:
        return False
    ref = _tokens(title) | _tokens(claim_text)
    if not ref:
        return True
    return bool(a & ref)


def _candidate_rows(cur, families, reps, limit):
    """[{pub, fam, title, text, rank}] for the screen, best retrieval rank first."""
    rows, pids = [], []
    for i, fam in enumerate(families):
        r = reps.get(fam)
        if not r:
            continue
        rows.append({"pub": r["publication_number"], "fam": fam, "pid": r["id"],
                     "title": r.get("title") or "", "rank": len(rows) + 1})
        pids.append(r["id"])
        if len(rows) >= limit:
            break
    if not pids:
        return rows
    claims = {}
    cur.execute("SELECT publication_id, claim_no, text FROM claims WHERE publication_id = ANY(%s) "
                "AND claim_no <= 3 ORDER BY publication_id, claim_no", (pids,))
    for c in cur.fetchall():
        claims.setdefault(c["publication_id"], []).append(c["text"] or "")
    #  DESCRIPTION FALLBACK. Old US patents in this corpus have no abstract row and no claims
    #  table: their entire disclosure is in paragraph chunks. The screener was therefore shown an
    #  empty string for them and scored them 0 and 10, and two genuinely relevant vacuum lifters
    #  from a real examiner citation list were dropped on that basis. This is the same defect a
    #  previous audit found in the relevance judge, in a different stage.
    paras = {}
    cur.execute("SELECT publication_id, text FROM chunks WHERE publication_id = ANY(%s) "
                "AND kind='paragraph' AND text IS NOT NULL ORDER BY publication_id, id", (pids,))
    for c in cur.fetchall():
        got = paras.setdefault(c["publication_id"], [])
        if len(got) < 4:
            got.append(c["text"] or "")
    abstracts, dates = {}, {}
    cur.execute("SELECT id, abstract, publication_date FROM publications WHERE id = ANY(%s)",
                (pids,))
    for p in cur.fetchall():
        abstracts[p["id"]] = p["abstract"] or ""
        dates[p["id"]] = p["publication_date"]
    #  The CLASSIFICATION, which the screener never used to see. For an abstract-only 1923 filing
    #  it is often the strongest evidence available: a human examiner reaches for the CPC symbol
    #  precisely when the text is thin, and withholding it forced the model to score those
    #  documents on forty words or on nothing.
    cpcs = {}
    cur.execute("SELECT publication_id, symbol FROM classifications "
                "WHERE publication_id = ANY(%s) AND is_first IS NOT FALSE", (pids,))
    for c in cur.fetchall():
        cpcs.setdefault(c["publication_id"], [])
        if len(cpcs[c["publication_id"]]) < 6 and c["symbol"] not in cpcs[c["publication_id"]]:
            cpcs[c["publication_id"]].append(c["symbol"])
    for row in rows:
        cl = " ".join(claims.get(row["pid"], []))[:1400]
        ab = abstracts.get(row["pid"], "")
        if not abstract_is_trustworthy(ab, row["title"], cl):
            ab = ""
        body = (ab + "  " + cl).strip()[:SCREEN_CHARS]
        if not body:
            body = " ".join(paras.get(row["pid"], []))[:SCREEN_CHARS].strip()
        row["has_text"] = bool(body)
        meta = []
        d = dates.get(row["pid"])
        if d:
            meta.append(str(d)[:4])
        if cpcs.get(row["pid"]):
            meta.append("CPC " + ", ".join(cpcs[row["pid"]]))
        if not body:
            meta.append("no text beyond the title in this corpus")
        elif not cl and not ab:
            meta.append("description text only, no abstract or claims in this corpus")
        elif not cl:
            meta.append("abstract only, no claim text in this corpus")
        row["text"] = ((" | ".join(meta) + "\n  " if meta else "") + body).strip()
    return rows


def screen(rows, brief, on_progress=None):
    """{pub: 0-100} over every candidate row, in batches. Fail-soft: an unscored candidate simply
    has no screen score and falls back to its retrieval rank."""
    if not rows:
        return {}
    #  BATCHES ARE INTERLEAVED, NOT CONTIGUOUS SLICES OF THE RANKING.
    #
    #  The screener scores 25 candidates in one call and calibrates WITHIN that call. Cutting a
    #  rank-ordered list into contiguous batches therefore made the first batch all excellent
    #  documents, which the model spread across 60-95, and the two-hundredth batch all mediocre
    #  ones, which it spread across 20-60. The absolute numbers were not comparable between
    #  batches, and they are used as a threshold for what gets read and as a term in the final
    #  ranking.
    #
    #  Measured: the same publication scored 85 in one run and 60 in the next, and 75 on an
    #  isolated re-screen. An A/B on identical batches showed the per-candidate text budget
    #  explains almost none of that (+1.9 on cited references, -0.1 on everything else), so batch
    #  composition was what moved.
    #
    #  Round-robin by rank gives every batch the same spread of quality, so the model has the full
    #  range to calibrate against every time. Deterministic, so a report is still reproducible.
    n_batches = max(1, (len(rows) + SCREEN_BATCH - 1) // SCREEN_BATCH)
    buckets = [[] for _ in range(n_batches)]
    for i, r in enumerate(rows):
        buckets[i % n_batches].append(r)
    batches = [b for b in buckets if b]
    done = [0]
    lock = threading.Lock()

    def one(batch):
        body = "\n".join(f"[{j + 1}] {c['title']}\n  {c['text']}" for j, c in enumerate(batch))
        out = llm.chat_json(_SCREEN_SYS,
                            f"TARGET INVENTION:\n{brief[:6000]}\n\nCANDIDATES:\n{body}",
                            max_tokens=1600) or {}
        got = {}
        for x in (out.get("results") or []):
            if not isinstance(x, dict):
                continue
            try:
                j = int(x["id"]) - 1
                sc = int(x.get("score") if x.get("score") is not None else -1)
            except (TypeError, ValueError, KeyError):
                continue
            if 0 <= j < len(batch) and 0 <= sc <= 100:
                got[batch[j]["pub"]] = sc
        with lock:
            done[0] += 1
            if on_progress:
                try:
                    on_progress(done[0], len(batches))
                except Exception:
                    pass
        return got

    scores = {}
    with ThreadPoolExecutor(max_workers=min(SCREEN_WORKERS, len(batches))) as ex:
        for g in ex.map(one, batches):
            scores.update(g)
    return scores


def _enrich_missing_text(chosen, on_progress=None, limit=None):
    """Fetch and persist full text for the chosen references the corpus holds nothing readable for.

    Returns the number of references that gained text. Never fatal: a quota-exhausted or
    unreachable source leaves the reference exactly as it was, to be listed rather than read.
    """
    if ENRICH_TOP <= 0 or not chosen:
        return 0
    try:
        import enrich
    except Exception:
        return 0
    available = getattr(enrich, "recovery_available", None)
    if available is not None:
        if not available():
            return 0
    elif not getattr(enrich, "SERP_KEY", ""):
        return 0
    pubs = [r["pub"] for r in chosen]
    thin = []
    try:
        conn = db.connect()
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT p.publication_number pub,
                              (SELECT count(*) FROM claims c WHERE c.publication_id=p.id) cl,
                              (SELECT count(*) FROM chunks ch WHERE ch.publication_id=p.id
                                 AND ch.kind='paragraph') pa
                       FROM publications p WHERE p.publication_number = ANY(%s)""", (pubs,))
                have = {r["pub"]: (r["cl"], r["pa"]) for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception:
        return 0
    order = {r["pub"]: i for i, r in enumerate(chosen)}
    for pub in pubs:
        cl, pa = have.get(pub, (0, 0))
        if cl == 0 and pa == 0:
            thin.append(pub)
    thin.sort(key=lambda p: order.get(p, 10 ** 6))
    thin = thin[:(limit or ENRICH_TOP)]
    if not thin:
        return 0
    if on_progress:
        try:
            on_progress("enrich_start", n=len(thin))
        except Exception:
            pass
    done = [0]
    lock = threading.Lock()

    def one(pub):
        try:
            r = enrich.enrich_publication(pub, reembed=False)
            ok = bool(r and r.get("ok") and (r.get("added_claims") or
                                              r.get("added_paragraphs") or
                                              r.get("added_chunks")))
        except Exception:
            ok = False
        with lock:
            done[0] += 1
            if on_progress and (done[0] % 10 == 0 or done[0] == len(thin)):
                try:
                    on_progress("enrich_progress", done=done[0], total=len(thin))
                except Exception:
                    pass
        return ok

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(ENRICH_WORKERS, len(thin))) as ex:
        got = sum(1 for ok in ex.map(one, thin) if ok)
    print(f"[deep_rank] fetched full text for {got}/{len(thin)} references that had none "
          f"({time.time() - t0:.0f}s)", flush=True)
    return got


def _grounded_rows(ref, kind):
    """The rows of one charted reference that carry real, located, verbatim evidence."""
    return [r for r in (ref.get(kind) or [])
            if r.get("grounding") == "verified" and r.get("verdict") in _W
            and _W[r["verdict"]] > 0]


def rarity(charts, features, claims):
    """log(N/df) per feature and per claim, computed over the references actually charted.

    Rarity is measured, not declared: it says how unusual this disclosure is AMONG THE ART THIS
    SEARCH FOUND, which is exactly the question a novelty argument turns on.
    """
    readable = [c for c in charts if c.get("method") == "llm"] or charts
    n = max(1, len(readable))
    fdf = {f: 0 for f in features}
    cdf = {c: 0 for c in claims}
    for ref in readable:
        for r in _grounded_rows(ref, "features"):
            if r["item"] in fdf:
                fdf[r["item"]] += 1
        for r in _grounded_rows(ref, "claims"):
            if r["item"] in cdf:
                cdf[r["item"]] += 1
    fidf = {f: math.log(n / max(1, d)) + 0.15 for f, d in fdf.items()}
    cidf = {c: math.log(n / max(1, d)) + 0.15 for c, d in cdf.items()}
    return {"n": n, "feature_df": fdf, "claim_df": cdf,
            "feature_idf": fidf, "claim_idf": cidf}


def _claim_leaders(charts, rar, depth=LEAD_DEPTH):
    """{pub: [(claim label, idf)]} — the rare CLAIMS each reference is among the best disclosures of.

    The same credit `leaders` gives for a rare feature, for a rare claim. It matters more here: a
    claim disclosed by two references out of five hundred is the claim an attorney builds an
    attack on, and the reference that discloses it usually discloses nothing else, so without this
    it earns nothing anywhere in the ranking.
    """
    cidf = rar.get("claim_idf") or {}
    if not cidf:
        return {}
    idfs = sorted(cidf.values())
    median = idfs[len(idfs) // 2]
    hits = {}
    for ref in charts:
        for r in _grounded_rows(ref, "claims"):
            if r["verdict"] == "uncertain":
                continue
            hits.setdefault(r["item"], []).append(
                (ref["pub"], r["verdict"], float(r.get("confidence") or 0.0)))
    out = {}
    for item, hs in hits.items():
        if cidf.get(item, 0.0) < median:
            continue
        hs.sort(key=lambda h: ({"disclosed": 0, "partial": 1}.get(h[1], 2), -h[2]))
        for pub, _v, _c in hs[:depth]:
            out.setdefault(pub, []).append((item, round(cidf[item], 3)))
    return out


def leaders(charts, rar, depth=LEAD_DEPTH):
    """{pub: [(feature, idf)]} — the RARE features each reference is among the best disclosures of.

    "Rare" is measured, not declared: a feature whose idf is at or above the median across the
    features of this search. Only `depth` references per feature qualify, so this promotes the
    best disclosure of a distinctive teaching rather than everything that mentions it.
    """
    idfs = sorted(rar["feature_idf"].values())
    if not idfs:
        return {}
    median = idfs[len(idfs) // 2]
    out = {}
    for row in by_feature(charts, rar, top=depth):
        #  Compare the UNROUNDED idf: by_feature rounds for display, and the rarest feature in a
        #  small set can be the median, so `round(x, 3) < x` silently dropped it.
        if rar["feature_idf"].get(row["feature"], 0.0) < median:
            continue
        for r in row["references"][:depth]:
            if r["verdict"] == "uncertain":
                continue
            out.setdefault(r["pub"], []).append((row["feature"], row["idf"]))
    return out


def score_reference(ref, rar, lead=()):
    """(0-100 score, detail) for ONE charted reference.

    The score is the share of the invention's DISTINCTIVE MASS this reference covers: the sum of
    the rarity weights it grounded, over the sum of all rarity weights available. That makes it
    interpretable on its own ("covers 78% of what makes this invention distinctive") instead of
    being a rank in disguise, and it does not move when an unrelated reference is added.
    """
    fidf, cidf = rar["feature_idf"], rar["claim_idf"]
    total = sum(fidf.values()) + _CLAIM_WEIGHT * sum(cidf.values())
    got = 0.0
    lead = list(lead or [])
    n_disc = n_part = n_unc = 0
    covered = []
    for r in _grounded_rows(ref, "features"):
        w = _W[r["verdict"]]
        got += w * fidf.get(r["item"], 0.0)
        n_disc += r["verdict"] == "disclosed"
        n_part += r["verdict"] == "partial"
        n_unc += r["verdict"] == "uncertain"
        covered.append({"item": r["item"], "verdict": r["verdict"], "kind": "feature",
                        "idf": round(fidf.get(r["item"], 0.0), 3),
                        "location": r.get("location") or "", "quote": r.get("quote") or "",
                        #  WHICH PUBLICATION THIS QUOTE CAME FROM. Today it is always the charted
                        #  reference itself, so the field is redundant and that is the point of
                        #  adding it now: the family-member substitution planned next will let a
                        #  sibling's text stand in for a reference we cannot read, and claims are
                        #  amended between offices. A quote taken from a US sibling does not
                        #  prove what the cited DE publication disclosed. Recording the source
                        #  publication per cell is what makes that checkable rather than assumed.
                        "evidence_pub": r.get("evidence_pub") or ref.get("pub") or ""})
    for r in _grounded_rows(ref, "claims"):
        got += _CLAIM_WEIGHT * _W[r["verdict"]] * cidf.get(r["item"], 0.0)
        #  CLAIMS GO INTO `covered` TOO. They used to contribute to the score above and then
        #  vanish: `covered` is what coverage_rank consumes to order the page, so the one
        #  reference that disclosed claim 5 and nothing else had a marginal gain of zero and was
        #  ordered as though it added nothing. Measured: three of the ten references an attorney
        #  cited for this subject were read in full and ranked 152, 253 and 346 of 498.
        n_disc += r["verdict"] == "disclosed"
        n_part += r["verdict"] == "partial"
        n_unc += r["verdict"] == "uncertain"
        covered.append({"item": r["item"], "verdict": r["verdict"], "kind": "claim",
                        "idf": round(cidf.get(r["item"], 0.0), 3),
                        "location": r.get("location") or "", "quote": r.get("quote") or "",
                        "evidence_pub": r.get("evidence_pub") or ref.get("pub") or ""})
    got += LEAD_WEIGHT * sum(idf for _f, idf in lead)
    covered.sort(key=lambda c: -c["idf"])
    pct = 0.0 if total <= 0 else max(0.0, min(1.0, got / total))
    coverage = 100.0 * pct
    #  Blend in the reader's holistic verdict, but ONLY for a reference that was actually read AND
    #  grounded at least one quote. An ungrounded or text-less document scores on coverage alone,
    #  which is 0 — that is the gate that keeps a title-only record out of the head.
    overall = (ref.get("overall") or {}).get("score")
    #  "Read in full" has to mean the corpus HELD more than an abstract. 84% of this corpus is
    #  abstract-only, and charting twelve features against forty words is not a reading: it
    #  grounds almost nothing, so grounded coverage drives the document to the floor and it is
    #  ranked last for being old rather than for being irrelevant. Those are exactly the documents
    #  examiners cite.
    read_in_full = ref.get("method") == "llm" and int(ref.get("n_paragraphs_read") or 0) + \
        int(ref.get("n_claims_read") or 0) > 0
    chars = int(ref.get("chars_read") or ref.get("chars") or 0)
    screen = ref.get("screen")
    if overall is not None and read_in_full and covered:
        #  WEIGHTS ARE FIXED; DEPTH DISCOUNTS THE RESULT.
        #
        #  This used to scale the coverage weight by depth and hand the weight it gave up to
        #  `overall` and `screen` -- two judgements made from a snippet. The effect was that the
        #  LESS of a document we managed to read, the MORE the score came from a guess about it,
        #  and the guess is systematically optimistic. Measured on two finished reports:
        #
        #      WO-2017215163  coverage  7, read   8k chars -> 77, displayed at rank 10
        #      US-10625955    coverage 52, read 142k chars -> 63, NOT displayed, rank 53
        #
        #  A reference measured to disclose half the invention from its full text lost to one
        #  measured to disclose almost nothing, because the second had been read too thinly to be
        #  measured and inherited its screener's optimism. Both subjects of the benchmark failed
        #  this way: every cited reference that got read sat at rank 57-290 behind a wall of
        #  thin records, and the median text behind a displayed card was 15,432 characters.
        #
        #  So: weight coverage the same however much was read, and let a shallow reading discount
        #  the whole score instead of redistributing it. Not reading a document can no longer
        #  raise its rank. The floor keeps the discount modest -- an abstract-only record still
        #  competes on what it does say, which matters because examiners cite plenty of them.
        depth = max(0.0, min(1.0, chars / float(DEPTH_FULL_CHARS))) if DEPTH_FULL_CHARS else 1.0
        rest = max(0.0, 1.0 - COVERAGE_WEIGHT)
        if screen is None:
            w_ovr, w_scr = rest, 0.0
        else:
            w_ovr, w_scr = rest * OVERALL_SHARE, rest * (1.0 - OVERALL_SHARE)
        raw = (COVERAGE_WEIGHT * coverage + w_ovr * float(overall)
               + w_scr * float(screen if screen is not None else 0))
        score = raw * (DEPTH_CONFIDENCE_FLOOR + (1.0 - DEPTH_CONFIDENCE_FLOOR) * depth)
    else:
        score = coverage
        overall = None
    #  A document we could NOT properly read is ranked on the best evidence that does exist for it
    #  (its screen score, capped), or on whatever it did manage to ground, whichever is higher. The
    #  cap is what keeps it below every reference whose full text was quoted; the floor is what
    #  stops "we have no text" being reported as "it discloses nothing".
    if not read_in_full:
        #  Ranked WITHIN ITS OWN TIER on the best evidence that exists for it. No cap here: a
        #  ceiling flattens the tier into a plateau, and the tier is already held behind every
        #  read reference by the ordering. `UNREAD_SCORE_CAP` bounds what is DISPLAYED, so a
        #  screen score is never shown as if it were a reading.
        if screen is not None:
            score = max(score, float(screen))
    return int(round(max(0.0, min(100.0, score)))), {
        "coverage": int(round(coverage)), "overall": overall, "screen": screen,
        "n_disclosed": n_disc, "n_partial": n_part, "n_uncertain": n_unc,
        "n_features": len(rar["feature_df"]), "covered": covered,
        "leads": [f for f, _idf in lead],
        "read_in_full": read_in_full,
        "chars_read": int(ref.get("chars") or 0),
        #  The publication whose TEXT was read. Equal to the reference itself unless a family
        #  member stood in for it. `evidence_publication_id` is the identifier any displayed
        #  evidence must be attributed to, and `is_proxy_text` says whether that attribution
        #  differs from the reference being cited.
        "evidence_publication_id": ref.get("text_source_pub") or ref.get("pub") or "",
        "is_proxy_text": bool(ref.get("text_source_pub")
                              and ref.get("text_source_pub") != ref.get("pub")),
    }


def _why(ref, detail):
    """A one-sentence opinion assembled from the EVIDENCE, not written by a model.

    The old per-card opinion was a model sentence about a 900-character snippet, and it is what
    told the searcher that a reference disclosing 10 of 12 features "does not disclose the
    specific loop-shaped seal, the continuous air extraction, or the unique bracing structure".
    This one can only say what a grounded, located, refuter-survived quote supports.
    """
    if not detail["read_in_full"]:
        return ("Not read in full: this corpus holds only a title and an abstract for this "
                "reference, so it is ranked on that and held below every reference whose full "
                "text was quoted. The office copy may say considerably more.")
    leads = detail.get("leads") or []
    lead_note = (" Best disclosure found for: " + "; ".join(leads[:2]) + ".") if leads else ""
    n = detail["n_disclosed"] + detail["n_partial"] + detail["n_uncertain"]
    if not n:
        return "Read in full: no feature of the invention could be grounded in a verbatim " \
               "passage of this reference."
    top = [c for c in detail["covered"] if c["verdict"] == "disclosed"][:2] or detail["covered"][:2]
    named = "; ".join(f"{c['item']} ({c['location']})".strip() for c in top if c.get("item"))
    ov = detail.get("overall")
    ov_note = f" Read as a whole it scores {ov}/100." if ov is not None else ""
    head = (f"Read in full ({detail['chars_read']:,} characters). Grounds "
            f"{detail['n_disclosed']} disclosed, {detail['n_partial']} partial and "
            f"{detail['n_uncertain']} unconfirmed of {detail['n_features']} features")
    return (f"{head}, the rarest being {named}.{ov_note}{lead_note}" if named
            else f"{head}.{ov_note}{lead_note}")


def by_feature(charts, rar, top=6):
    """The best reference for EACH feature, rarest feature first.

    A reference is not only a novelty candidate. DE-3724659-A1 (1989) discloses 2 to 6 of 12
    features and would never lead a novelty list, but it is the best single disclosure of "a
    spacer that limits the compression of the sealing lip", which is the characterising feature of
    the patent that was uploaded. That is an inventive-step reference, and the old report had no
    place to put it.
    """
    out = []
    for feat, idf in sorted(rar["feature_idf"].items(), key=lambda kv: -kv[1]):
        hits = []
        for ref in charts:
            for r in _grounded_rows(ref, "features"):
                if r["item"] != feat:
                    continue
                hits.append({"pub": ref["pub"], "title": ref.get("title") or "",
                             "verdict": r["verdict"], "location": r.get("location") or "",
                             "quote": r.get("quote") or "",
                             "confidence": float(r.get("confidence") or 0.0)})
        hits.sort(key=lambda h: ({"disclosed": 0, "partial": 1, "uncertain": 2}.get(h["verdict"], 3),
                                 -h["confidence"]))
        out.append({"feature": feat, "idf": round(idf, 3),
                    "df": rar["feature_df"].get(feat, 0), "references": hits[:top]})
    return out


def run(report, reports_dir=None, slug=None, on_progress=None):
    """Screen wide, read deep, and return the authoritative ranking. Mutates `report`.

    Writes the charts in ``deep_analysis``'s own schema so the "What it discloses" tab and
    ``/analysis/<slug>`` render from THIS reading instead of starting a second, separate one.
    """
    started = time.time()
    #  THE CHECKLIST THE SEARCH IS ARGUED AGAINST. `elements` is a 11-12 item summary of the
    #  invention; `disclosures` is its claim limitations, its whole independent claims, what each
    #  dependent claim adds, and the teachings the description supports but the claims do not
    #  cover. Measured on real subjects: 63 disclosures against 12 elements.
    #
    #  This is not cosmetic. With 12 disclosures the top 50 saturates by document nine -- 42 of
    #  the 50 slots were measured to add NOTHING to coverage -- so ranking by what a reference
    #  CONTRIBUTES has nothing to work with, and the report is structurally blind to the document
    #  that answers only claim 10. See coverage_rank.
    disc = report.get("disclosures") or []
    if disc:
        features = [d["text"] for d in disc][:DISCLOSURE_CAP]
        disc_weight = {d["text"]: d.get("weight", 1.0) for d in disc}
    else:
        features = [str(e).strip() for e in (report.get("elements") or []) if str(e).strip()]
        features = features[:deep_analysis.MAX_FEATURES]
        disc_weight = {}
    qd = report.get("query_document") or {}
    claim_items = []
    for c in (qd.get("claims") or [])[:deep_analysis.MAX_INPUT_CLAIMS]:
        text = str(c.get("text") or "").strip()
        if text:
            claim_items.append({"label": f"claim {c.get('claim_no') or len(claim_items) + 1}",
                                "claim_no": c.get("claim_no") or len(claim_items) + 1,
                                #  Carried through because an INDEPENDENT claim with no prior art
                                #  is the most serious hole a search can leave — it is the claim
                                #  the patent stands on — and both the rescue's priority order and
                                #  the chart's row labels need to know which ones those are.
                                "independent": bool(c.get("independent")),
                                "text": text})
    #  TYPE A vs TYPE B. A disclosure with no claims is a concept search; a patent WITH claims is
    #  an attack, and its unit of work is the limitation, not the claim and not the invention. See
    #  limitations.py — the two objectives disagree constantly and sharing one was measurably
    #  wrong: three references an attorney cited were read in full, grounded 0/0/1 of 28 invention
    #  features, and were ranked 253/346/152 because each answers exactly one requirement.
    stype = limmod.search_type(report)
    lims = []
    if stype == limmod.TYPE_B and claim_items:
        try:
            lims = limmod.split_claims(claim_items)
        except Exception:
            traceback.print_exc()
        if lims:
            #  The reader charts the limitations INSTEAD of the whole claims. Same machinery, same
            #  grounding and refutation gates; a better question. The label is the ledger key.
            claim_items = limmod.as_chart_items(lims)
    report["search_type"] = stype
    ranked = list(report.get("ranked_families") or [])
    #  ORACLE INJECTION, diagnostic only. Hands a stage the gold it never received so the stages
    #  BELOW it can be measured in isolation. Disarmed unless a flag, a valid stage and a non-empty
    #  gold list are all present, and every report it touches is stamped so no metric can score it
    #  as a real run. See src/oracle.py.
    _orc = report.get("_oracle") or {}
    orc = oracle.Oracle(stage=_orc.get("stage", ""), gold_families=_orc.get("gold") or [])
    if orc:
        before = len(ranked)
        ranked = orc.inject(ranked, "before_screen")
        report[oracle.REPORT_KEY] = orc.stamp()
        if len(ranked) != before:
            print(f"[oracle] injected {len(ranked) - before} gold families before the screen",
                  flush=True)
    if not ranked or not (features or claim_items):
        return None

    def emit(stage, **kw):
        if on_progress:
            try:
                on_progress(stage, kw)
            except Exception:
                pass

    import query_set
    import webview
    brief = query_set.retrieval_text(report.get("query") or "")
    subject_pub = deep_analysis._norm_pub(qd.get("publication_number"))

    conn = db.connect()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        reps = webview.resolve_family_reps(cur, ranked[:SCREEN_TOP])
        rows = _candidate_rows(cur, ranked[:SCREEN_TOP], reps, SCREEN_TOP)
    finally:
        conn.close()
    rows = [r for r in rows if not deep_analysis._same_pub(subject_pub, r["pub"])]
    if not rows:
        return None

    #  FETCH FIRST, SCREEN SECOND. See PRESCREEN_ENRICH_TOP. `_enrich_missing_text` decides for
    #  itself which of these hold no claims and no paragraphs, in retrieval order, so the whole
    #  candidate list is offered and the budget picks the head of what is genuinely unreadable.
    prescreen_enriched = 0
    if PRESCREEN_ENRICH_TOP > 0:
        t_pre = time.time()
        emit("prescreen_enrich_start", n=len(rows))
        prescreen_enriched = _enrich_missing_text(rows, on_progress=emit,
                                                  limit=PRESCREEN_ENRICH_TOP)
        if prescreen_enriched:
            #  Rebuild the candidate text so the screener sees what was just fetched. One query
            #  set over the same pids — the alternative is tracking which rows changed, and a row
            #  that silently keeps its old empty text is the exact defect being fixed.
            try:
                conn2 = db.connect()
                conn2.autocommit = True
                try:
                    rebuilt = _candidate_rows(conn2.cursor(), ranked[:SCREEN_TOP], reps, SCREEN_TOP)
                finally:
                    conn2.close()
                fresh = {r["pub"]: r for r in rebuilt}
                gained = 0
                for r in rows:
                    f = fresh.get(r["pub"])
                    if f and f.get("has_text") and not r.get("has_text"):
                        r["text"], r["has_text"] = f["text"], True
                        gained += 1
                print(f"[deep_rank] pre-screen text recovery: {prescreen_enriched} fetched, "
                      f"{gained} candidates the screener can now read "
                      f"({time.time() - t_pre:.0f}s)", flush=True)
            except Exception:
                traceback.print_exc()

    emit("screen_start", n=len(rows), batches=(len(rows) + SCREEN_BATCH - 1) // SCREEN_BATCH)
    t0 = time.time()
    scores = screen(rows, brief,
                    on_progress=lambda d, t: emit("screen_progress", done=d, total=t))
    screen_seconds = time.time() - t0

    #  Choose what to read: the head of the screen, plus the head of the RETRIEVAL order no matter
    #  what the screen said (see ALWAYS_CHART_RETRIEVAL_HEAD).
    by_screen = sorted(rows, key=lambda r: (-(scores.get(r["pub"], -1)), r["rank"]))
    chosen, seen = [], set()
    for i, r in enumerate(by_screen):
        if len(chosen) >= CHART_TOP_MAX:
            break
        if i >= CHART_TOP and scores.get(r["pub"], -1) < CHART_MIN_SCREEN:
            break
        chosen.append(r)
        seen.add(r["pub"])
    for r in rows[:ALWAYS_CHART_RETRIEVAL_HEAD]:
        if r["pub"] not in seen:
            chosen.append(r)
            seen.add(r["pub"])
    #  before_read: everything the screen saw whose family is gold gets read, whatever it scored.
    #  This isolates reading and below from any screening error.
    if orc.at("before_read") or orc.at("before_charting"):
        goldset = set(orc.gold)
        added = 0
        for r in rows:
            if r["pub"] in seen or r.get("fam") not in goldset:
                continue
            chosen.append(r)
            seen.add(r["pub"])
            added += 1
        report[oracle.REPORT_KEY] = orc.stamp()
        print(f"[oracle] forced {added} gold references into the read set", flush=True)
    #  A low score from a screener that was shown NOTHING is not evidence of irrelevance, it is
    #  the absence of evidence, and it must not be the reason a reference is never read. Any
    #  candidate the screen could not see text for is read anyway if the retrieval ranked it
    #  inside BLIND_RESCUE. Bounded, and it is exactly the set that on-demand text can rescue.
    rescued = 0
    for r in rows[:BLIND_RESCUE]:
        if rescued >= BLIND_RESCUE_MAX:
            break
        if r["pub"] not in seen and not r.get("has_text"):
            chosen.append(r)
            seen.add(r["pub"])
            rescued += 1
    if rescued:
        print(f"[deep_rank] reading {rescued} references the screen could not see any text for",
              flush=True)
    #  Fetch the missing text BEFORE reading, for the references we have just chosen to read.
    enriched = _enrich_missing_text(chosen, on_progress=emit)

    #  THE CONCEPT LAYER, built ONCE for the whole search and handed to every reader.
    #
    #  The checklist is extracted from the subject's own claims, so it speaks the subject's
    #  vocabulary: "at least one bracing structure protrudes from the second side of the base
    #  element". Almost no prior art says that. Measured on adhoc-939fb711122b, "seal element
    #  elastically deformable at contact surface" is taught by hundreds of the references read, in
    #  words like sealing lip, gasket, resilient ring, Dichtlippe. Giving each reader the IDEA and
    #  the other words for it, before it decides "absent", is what turns a string comparison back
    #  into a reading.
    emit("concepts_start", n=len(features))
    try:
        hints = deep_analysis.concept_expansions(
            features, title=(qd.get("label") or qd.get("title") or ""), brief=brief)
    except Exception:
        traceback.print_exc()
        hints = {}
    print(f"[deep_rank] concept expansions for {len(hints)}/{len(features)} features", flush=True)
    emit("chart_start", n=len(chosen))

    done = [0]
    lock = threading.Lock()

    def one(row):
        try:
            ref = deep_analysis.analyse_reference(row["pub"], features, claim_items, row["title"],
                                                  hints=hints)
        except Exception as exc:
            ref = {"pub": row["pub"], "title": row["title"], "found": False, "features": [],
                   "claims": [], "method": "error", "error": str(exc)[:200], "chars": 0}
        ref["screen"] = scores.get(row["pub"])
        ref["retrieval_rank"] = row["rank"]
        ref["family"] = row["fam"]
        with lock:
            done[0] += 1
            if done[0] % 5 == 0 or done[0] == len(chosen):
                emit("chart_progress", done=done[0], total=len(chosen), pub=row["pub"])
        return ref

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(CHART_WORKERS, max(1, len(chosen)))) as ex:
        charts = list(ex.map(one, chosen))
    chart_seconds = time.time() - t0

    #  SECOND LOOK at the head, feature by feature, on the concepts rather than the wording. Only
    #  the rows a first reading left empty are re-asked, and an upgrade still has to pass the same
    #  grounding and location gates plus the refuter — a second look widens what the reader
    #  RECOGNISES, never what it is allowed to assert.
    reread_changed = reread_refs = 0
    if CONCEPT_PASS_TOP > 0 and hints:
        prelim = sorted(
            [c for c in charts if c.get("method") == "llm"],
            key=lambda c: (-(int((c.get("overall") or {}).get("score") or 0)),
                           -(c.get("screen") if c.get("screen") is not None else -1),
                           c.get("retrieval_rank") or 10 ** 6))[:CONCEPT_PASS_TOP]

        def second(ref):
            try:
                n = deep_analysis.reread_absent(ref["pub"], ref.get("features") or [], hints=hints)
                if n:
                    #  Refute ONLY what the second pass changed. The dicts are shared with
                    #  ref["features"], so the downgrade lands on the chart itself.
                    deep_analysis._refute(
                        [r for r in ref["features"] if r.get("second_pass")], ref["pub"])
                    ref["counts"] = deep_analysis._counts(
                        (ref.get("features") or []) + (ref.get("claims") or []))
                return n
            except Exception:
                return 0

        emit("reread_start", n=len(prelim))
        t1 = time.time()
        with ThreadPoolExecutor(max_workers=min(CHART_WORKERS, max(1, len(prelim)))) as ex:
            gained = list(ex.map(second, prelim))
        reread_changed = sum(gained)
        reread_refs = sum(1 for g in gained if g)
        print(f"[deep_rank] second concept pass: {reread_changed} cells recovered across "
              f"{reread_refs} of {len(prelim)} references ({time.time() - t1:.0f}s)", flush=True)
        chart_seconds += time.time() - t1

    #  THE ORPHAN-CLAIM RESCUE. Runs HERE — after the whole search and reading are finished, before
    #  anything is scored or ordered — so the references it brings back compete on the same
    #  evidence as the rest of the report rather than being listed beside it. See claim_rescue.
    #  THE LEDGER. One row per limitation, filled only from grounded, located, refuter-survived
    #  cells, and it is the stopping rule for a Type B search: a budget that expires while claim 7
    #  has nothing against it is an abandoned search, not a finished one, and the two have looked
    #  identical on the page.
    ledger = None
    if lims:
        try:
            ledger = limmod.Ledger(lims)
            n = ledger.ingest_charts(charts)
            s = ledger.summary()
            report["ledger"] = ledger.to_dict()
            print(f"[ledger] {s['n_limitations']} limitations across {s['n_claims']} claims: "
                  f"{s['counts']['covered']} covered, {s['counts']['partial']} partial, "
                  f"{s['counts']['uncovered']} with nothing ({n} cells)"
                  + (f" — ANTICIPATED: {', '.join(s['anticipated'])}" if s["anticipated"] else ""),
                  flush=True)
        except Exception:
            traceback.print_exc()
            ledger = None

    rescue = {"ran": False}
    if RESCUE_CLAIMS and claim_items:
        t2 = time.time()
        try:
            rescued_refs, rescue = claim_rescue.run(
                charts, claim_items, features, hints,
                subject=_subject_for(report, qd), mode=report.get("mode") or "novelty",
                retriever=_shared_retriever(), brief=brief,
                title=(qd.get("label") or qd.get("title") or ""),
                description=_subject_description(report, qd),
                exclude_pubs=({r["pub"] for r in rows} |
                              {c.get("pub") for c in charts if c.get("pub")}),
                exclude_families={r["fam"] for r in rows},
                enrich=lambda chosen: _enrich_missing_text(chosen, on_progress=emit),
                ledger=ledger, emit=emit)
            if rescued_refs:
                charts.extend(rescued_refs)
        except Exception:
            traceback.print_exc()
        chart_seconds += time.time() - t2
        #  Re-ingest so the stored ledger is the FINAL state. Without this the report ships the
        #  coverage the rescue was launched to fix, which is the one number it must not show.
        if ledger is not None:
            try:
                ledger.ingest_charts(charts)
                report["ledger"] = ledger.to_dict()
                s2 = ledger.summary()
                print(f"[ledger] after the rescue: {s2['counts']['covered']} covered, "
                      f"{s2['counts']['partial']} partial, {s2['counts']['uncovered']} with "
                      f"nothing; done={s2['done']}"
                      + (f" — ANTICIPATED: {', '.join(s2['anticipated'])}"
                         if s2["anticipated"] else ""), flush=True)
            except Exception:
                traceback.print_exc()

    rar = rarity(charts, features, [c["label"] for c in claim_items])
    lead_map = leaders(charts, rar)
    for pub, leads in _claim_leaders(charts, rar).items():
        lead_map.setdefault(pub, []).extend(leads)
    by_pub, order = {}, []
    for ref in charts:
        sc, detail = score_reference(ref, rar, lead=lead_map.get(ref["pub"], ()))
        by_pub[ref["pub"]] = {
            "score": sc, "screen": ref.get("screen"), "family": ref.get("family"),
            "title": ref.get("title") or "",
            "retrieval_rank": ref.get("retrieval_rank"), "why": _why(ref, detail),
            **{k: v for k, v in detail.items() if k != "covered"},
            "covered": detail["covered"][:8],
        }
        order.append(ref["pub"])
    #  Ordering: READ-IN-FULL FIRST, then evidence score, then the screen, then the retrieval's
    #  own opinion. The first key is the one that matters: a reference whose full text was quoted
    #  and a reference we hold forty words of are not comparable quantities, and sorting them into
    #  one list by a single number is what produced a page of 50 on which 44 were records nobody
    #  had read. Every key is deterministic, so the same report always renders the same way.
    order.sort(key=lambda p: (0 if by_pub[p].get("read_in_full") else 1,
                              -by_pub[p]["score"],
                              -(by_pub[p]["screen"] if by_pub[p]["screen"] is not None else -1),
                              by_pub[p]["retrieval_rank"] or 10**6))

    #  RANK BY WHAT EACH REFERENCE ADDS, not by how much it resembles the invention.
    #
    #  The sort above is pointwise: every reference scored alone, then ordered. That is the wrong
    #  objective for a search whose whole purpose is a legal argument. If ten disclosures exist and
    #  fifty documents each cover the same nine, all fifty outrank the one document covering the
    #  tenth, so the report shows the fifty most similar results and stays permanently blind to the
    #  only reference that speaks to disclosure ten. Measured on a finished report: 42 of 50
    #  displayed documents added nothing at all to coverage.
    #
    #  Greedy maximum-coverage fixes the objective. The first pick is unchanged (nothing is covered
    #  yet, so it is still the strongest reference); what changes is every slot after it.
    if orc.at("before_portfolio"):
        goldset = set(orc.gold)
        head = [p for p in order if (by_pub.get(p) or {}).get("family") in goldset]
        order = head + [p for p in order if p not in set(head)]
        report[oracle.REPORT_KEY] = orc.stamp()
        print(f"[oracle] moved {len(head)} charted gold references to the front", flush=True)
    if COVERAGE_ORDER and len(order) > 1:
        try:
            idf = dict(rar["feature_idf"])
            if disc_weight:                    # rarity x how much the disclosure carries legally
                idf = {k: v * disc_weight.get(k, 1.0) for k, v in idf.items()}
            #  THE CLAIMS ARE PART OF THE OBJECTIVE. Same scale as a feature by default: a claim
            #  and a disclosure are both things the report has to find art for, and weighting one
            #  above the other is a thumb on the scale nobody measured. What makes the claims
            #  reliably visible is the guarantee pass below, not a bigger number here.
            for label, v in (rar.get("claim_idf") or {}).items():
                idf[label] = v * CLAIM_COVERAGE_WEIGHT
            reordered, gains = coverage_rank.rank(
                order, by_pub, idf, score_of=lambda p: by_pub[p]["score"])
            if len(reordered) == len(order):
                for p, g in zip(reordered, gains):
                    by_pub[p]["marginal_gain"] = g
                order = reordered
        except Exception:
            traceback.print_exc()

    #  EVERY CLAIM'S BEST DISCLOSER GETS A SLOT. Greedy coverage maximises total mass, so a claim
    #  disclosed once, weakly, by a document that covers nothing else is the very last thing it
    #  picks — which is exactly the document an attorney needs and exactly the one that was at
    #  rank 253 of 498. This promotes it into the window the page actually renders. It is a
    #  reordering only: nothing is added, nothing is dropped.
    if claim_items and order:
        try:
            promoted = coverage_rank.guarantee(
                order, by_pub, [c["label"] for c in claim_items], window=DISPLAY_WINDOW)
            if promoted:
                order = promoted["order"]
                for p in promoted["promoted"]:
                    by_pub[p]["promoted_for_claim"] = promoted["promoted"][p]
                print(f"[deep_rank] promoted {len(promoted['promoted'])} references into the "
                      f"top {DISPLAY_WINDOW} as the only art found for a claim: "
                      + ", ".join(f"{p} ({v})" for p, v in
                                  list(promoted["promoted"].items())[:6]), flush=True)
        except Exception:
            traceback.print_exc()

    #  Candidates that were screened but not read keep their screen score, capped, so they stay
    #  visible without ever outranking a reference whose full text was quoted.
    unread = []
    for r in rows:
        if r["pub"] in by_pub:
            continue
        s = scores.get(r["pub"])
        if s is None:
            continue
        unread.append((r, min(int(s), UNREAD_SCORE_CAP)))
    unread.sort(key=lambda t: (-t[1], t[0]["rank"]))

    #  Reorder the ranked families: charted first (by evidence), then screened-but-unread (by the
    #  capped screen score), then everything the screen never reached, in retrieval order.
    fam_order, seenfam = [], set()
    for p in order:
        f = by_pub[p].get("family")
        if f and f not in seenfam:
            seenfam.add(f)
            fam_order.append(f)
    for r, _s in unread:
        if r["fam"] not in seenfam:
            seenfam.add(r["fam"])
            fam_order.append(r["fam"])
    for f in ranked:
        if f not in seenfam:
            seenfam.add(f)
            fam_order.append(f)
    report["ranked_families"] = fam_order

    result = {
        "version": VERSION,
        "order": order,
        "by_pub": by_pub,
        "unread": {r["pub"]: s for r, s in unread},
        #  WHAT EACH STAGE SAW, recorded so the pipeline can be audited after the fact instead of
        #  only re-run. Without this, "was this reference retrieved and then dropped, or never
        #  retrieved at all?" cannot be answered from a finished report, and that is the first
        #  question anyone asks when a known citation is missing. Publication numbers only, in the
        #  order the retrieval produced them, plus every screen score.
        #  The second tier, as its own list: everything the search identified and screened as
        #  worth reading but could not read, because this corpus holds only a title and an
        #  abstract for it. Hiding these behind an unlabelled tail is how a searcher concludes the
        #  art does not exist; the office copy frequently says a great deal more.
        "not_readable": [
            {"pub": p, "title": (by_pub[p].get("title") or ""),
             "screen": by_pub[p].get("screen"),
             "retrieval_rank": by_pub[p].get("retrieval_rank")}
            for p in order if not by_pub[p].get("read_in_full")
        ][:120],
        "candidates": [r["pub"] for r in rows],
        "candidate_families": [r["fam"] for r in rows],
        "screen_scores": {p: int(v) for p, v in scores.items()},
        "screened": len(scores),
        "enriched": enriched,
        #  What the concept layer and the second look actually did, so both can be measured from a
        #  finished report rather than taken on trust.
        "prescreen_enriched": prescreen_enriched,
        "concept_hints": len(hints),
        "reread_refs": reread_refs,
        "reread_cells": reread_changed,
        "rescue": rescue,
        "search_type": stype,
        "n_limitations": len(lims),
        "ledger_summary": (ledger.summary() if ledger else None),
        "claim_idf": {k: round(v, 3) for k, v in (rar.get("claim_idf") or {}).items()},
        "claim_df": rar.get("claim_df") or {},
        "n_candidates": len(rows),
        "charted": len(charts),
        "n_features": len(features),
        "read_in_full": sum(1 for c in charts if c.get("method") == "llm"),
        "no_text": sum(1 for c in charts if c.get("method") == "no-text"),
        "chars_read": sum(int(c.get("chars") or 0) for c in charts),
        "feature_df": rar["feature_df"],
        "feature_idf": {k: round(v, 3) for k, v in rar["feature_idf"].items()},
        "by_feature": by_feature(charts, rar),
        "screen_seconds": round(screen_seconds, 1),
        "chart_seconds": round(chart_seconds, 1),
        "seconds": round(time.time() - started, 1),
    }
    report["deep_rank"] = result

    #  Hand the reading straight to deep_analysis's cache: it is the same schema, the same
    #  grounding and the same refutation, so re-reading these references would be pure waste.
    if reports_dir and slug:
        try:
            _publish_deep_analysis(reports_dir, slug, report, charts, order, features, claim_items,
                                   qd, subject_pub)
        except Exception:
            pass
    return result


def _subject_for(report, qd):
    """A search_modes.Subject for the document the search started from, or None.

    The rescue searches the corpus again, and a search with no subject has no date cutoff: it will
    happily return art published after the invention. `external.subject_from_doc` resolves the
    local row first and App A's merged record second, which is the same route the search itself
    used, so the cutoff is identical.
    """
    num = report.get("subject") or qd.get("publication_number") or ""
    if not num:
        return None
    try:
        import external
        return external.subject_from_doc(num)
    except Exception:
        return None


def _subject_description(report, qd):
    """The invention's own description, as context for restating a dependent claim.

    An upload carries its full text on the report. A LINK does not — only the claims are stashed —
    so the description is read back out of the corpus, which is where the search found the document
    in the first place.
    """
    txt = str(qd.get("disclosure_text") or "").strip()
    if len(txt) >= 400:
        return txt[:20000]
    num = report.get("subject") or qd.get("publication_number") or ""
    if not num:
        return txt
    try:
        ref = deep_analysis.full_text(num, max_chars=30000)
        if not ref.get("found"):
            return txt
        body = " ".join(p["text"] for p in ref["passages"]
                        if p["kind"] not in ("claim",))[:20000]
        return body or txt
    except Exception:
        return txt


def _shared_retriever():
    """The app's one Retriever. Building another loads a multi-million-row family map."""
    try:
        import webapp
        return webapp.retriever()
    except Exception:
        import retrieval
        return retrieval.Retriever()


def _publish_deep_analysis(reports_dir, slug, report, charts, order, features, claim_items, qd,
                           subject_pub):
    rank_of = {p: i + 1 for i, p in enumerate(order)}
    refs = sorted(charts, key=lambda r: rank_of.get(r["pub"], 10 ** 6))
    for r in refs:
        r["rank"] = rank_of.get(r["pub"])
    totals = {v: 0 for v in deep_analysis.VERDICTS}
    for r in refs:
        for row in (r.get("features") or []) + (r.get("claims") or []):
            v = row.get("verdict", "absent")
            totals[v] = totals.get(v, 0) + 1
    uncovered = []
    for i, f in enumerate(features):
        hit = any(len(r.get("features") or []) > i
                  and r["features"][i].get("verdict") in ("disclosed", "partial") for r in refs)
        if not hit:
            uncovered.append(f)
    data = {
        "version": deep_analysis.VERSION, "status": "done", "available": True,
        "features": features, "claims": claim_items,
        "subject_label": qd.get("label") or "", "has_subject_claims": bool(claim_items),
        "references": refs, "n_references": len(refs),
        "n_analysed": sum(1 for r in refs if r.get("method") == "llm"),
        "n_no_text": sum(1 for r in refs if r.get("method") == "no-text"),
        "chars_read": sum(int(r.get("chars") or 0) for r in refs),
        "refuted": sum(int(r.get("refuted") or 0) for r in refs),
        "uncovered_features": uncovered, "subject_pub_excluded": subject_pub or "",
        "counts": totals, "seconds": (report.get("deep_rank") or {}).get("chart_seconds"),
        "source": "deep_rank",
    }
    deep_analysis._write_atomic(deep_analysis._path(reports_dir, slug), data)


def card_fields(report, pub):
    """What a rendered card should show for `pub`, or None when this search has no deep rank."""
    dr = (report or {}).get("deep_rank") or {}
    if not dr:
        return None
    hit = (dr.get("by_pub") or {}).get(pub)
    if hit:
        return {"deep_score": hit["score"], "deep_why": hit["why"],
                "deep_leads": hit.get("leads") or [],
                "deep_read": bool(hit.get("read_in_full")),
                "deep_disclosed": hit.get("n_disclosed", 0),
                "deep_partial": hit.get("n_partial", 0),
                "deep_features": hit.get("n_features", 0),
                "deep_covered": hit.get("covered") or [],
                "deep_chars": hit.get("chars_read", 0),
                "deep_screen": hit.get("screen")}
    s = (dr.get("unread") or {}).get(pub)
    if s is None:
        return None
    #  Capped HERE as well as where the list is built. The invariant that matters is at the point
    #  of use: whatever is in the report, a card the reading never reached must not be able to
    #  outrank one whose full text was quoted.
    return {"deep_score": min(int(s), UNREAD_SCORE_CAP), "deep_read": False,
            "deep_screen": int(s),
            "deep_disclosed": 0, "deep_partial": 0,
            "deep_features": len(dr.get("feature_df") or {}), "deep_covered": [],
            "deep_chars": 0,
            "deep_why": "Screened on its abstract and claims but not read in full, so it is "
                        "ranked below every reference whose full text was quoted."}
