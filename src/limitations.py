"""The unit of an invalidity search is the LIMITATION, not the claim, and not the invention.

TWO SEARCHES, TWO OBJECTIVES
----------------------------
Type A — a disclosure arrives with no claims. The objective is coverage of the invention's
concepts, ranked by relevance, and the search stops when marginal new coverage flattens. That is
what this pipeline has always done and it is right for that input.

Type B — a granted or published patent arrives WITH ITS CLAIMS. This is an attack. The objective is
not "which documents resemble this invention" but "for every requirement of every claim, what dated
citable document already disclosed it". Those are different objectives and they disagree constantly:
measured on US 2026/0109053 A1, three references an attorney cited were read in full, grounded 0, 0
and 1 of 28 invention features, scored 19/16/27 against a page cut of 29, and were ranked 253, 346
and 152 — because each one is cited for exactly ONE requirement and resembles the invention barely
at all.

WHY THE CLAIM IS THE WRONG UNIT TOO
-----------------------------------
A claim is a conjunction. "A grip unit comprising a hollow grip portion, an electric vacuum
generating device, and a sound-damping device arranged in the exhaust path" is three separate
searchable requirements wearing one label, and a document teaching the third is correctly judged
"absent" for the whole claim. Measured: reading 17 references found specifically for the thing a
dependent claim ADDS grounded ONE claim cell in 221, while grounding 25 feature cells in the same
reading. The reader was not wrong; the question was.

So claims are split into limitations, one row per requirement, and the ledger below tracks each one
separately. A claim is covered when all of its limitations are; a claim is ANTICIPATED when a single
document covers all of them at once, which is the §102 kill and is worth far more than a
combination of several.

THE LEDGER IS THE STOPPING RULE
-------------------------------
Type A stops on marginal yield. Type B stops when every limitation has evidence, or when a
limitation has been escalated to exhaustion and we say so by name. A budget is not a stopping rule
for an attack: running out of rounds while claim 7 has nothing against it is not a finished search,
it is an abandoned one, and the two have looked identical on the page.
"""
from __future__ import annotations

import json
import os
import re

TYPE_A = "disclosure"
TYPE_B = "claims"

#  How many references must ground a limitation before it counts as covered. 1 is a finding; 2 is
#  an argument that survives one reference being distinguished.
COVER_MIN = int(os.environ.get("LEDGER_COVER_MIN", "2"))
#  Limitations charted per model call. Same reason the feature checklist is batched: a single
#  answer carrying sixty quoted rows is where a reader starts economising.
BATCH = int(os.environ.get("LEDGER_BATCH", "12"))
MAX_LIMITATIONS = int(os.environ.get("LEDGER_MAX", "80"))
#  Verdicts that count as evidence, and what each is worth when ranking which limitation is
#  weakest and should be searched next.
_W = {"disclosed": 1.0, "partial": 0.55, "uncertain": 0.25, "absent": 0.0}


def search_type(report_or_doc) -> str:
    """TYPE_B when the search started from a document that carried claims, else TYPE_A."""
    d = report_or_doc or {}
    qd = d.get("query_document") if isinstance(d.get("query_document"), dict) else d
    return TYPE_B if (qd or {}).get("claims") else TYPE_A


# ---------------------------------------------------------------------------
# splitting a claim into what it actually requires
# ---------------------------------------------------------------------------
_DEP = re.compile(
    r"\b(?:of|in|according to|as claimed in|as set forth in|as recited in)\s+"
    r"(?:any\s+(?:one\s+)?of\s+)?claims?\s+(\d+)", re.I)
#  Clause boundaries a drafter actually uses. Semicolons separate limitations; "wherein",
#  "further comprising" and "characterised in that" introduce them.
_SPLIT = re.compile(
    r";\s*(?:and\s+)?|\.\s+(?=[A-Z])|"
    r"(?<=[,\s])(?:wherein|whereby|further comprising|characteri[sz]ed in that|"
    r"and wherein|said)\s+", re.I)
_PREAMBLE = re.compile(r"\bcompris(?:ing|es)\b|\bconsisting of\b|\bincluding\b|:", re.I)


def parent_of(text) -> int | None:
    m = _DEP.search(str(text or ""))
    return int(m.group(1)) if m else None


def _clauses(text):
    """Split one claim body into requirement-sized pieces, structurally.

    Deliberately conservative: a piece shorter than a phrase is noise, and over-splitting produces
    limitations nothing can ever disclose, which then read as uncovered art forever.
    """
    body = " ".join(str(text or "").split())
    m = _PREAMBLE.search(body)
    if m:
        body = body[m.end():]
    parts = [p.strip(" ,;:.") for p in _SPLIT.split(body) if p and p.strip(" ,;:.")]
    out, buf = [], ""
    for p in parts:
        #  Glue fragments onto the previous clause rather than emitting them: "said seal" alone is
        #  not a requirement, it is the tail of one.
        if len(p) < 25 and out:
            out[-1] = (out[-1] + "; " + p).strip()
            continue
        if len(p) < 25:
            buf = (buf + " " + p).strip()
            continue
        out.append((buf + " " + p).strip() if buf else p)
        buf = ""
    if buf and out:
        out[-1] = (out[-1] + " " + buf).strip()
    elif buf:
        out.append(buf)
    return [o for o in out if len(o) >= 25][:12]


_SPLIT_SYS = (
    "You are a patent examiner preparing an invalidity search. Split each claim into its "
    "LIMITATIONS: the separate technical requirements a reference would have to disclose.\n"
    "\n"
    "Rules that matter:\n"
    "- A limitation is one requirement, stated so it can be searched ON ITS OWN. Write it as a "
    "complete technical statement, not a fragment: \"a sound-damping device in the exhaust air "
    "path\", never \"said device\".\n"
    "- For a DEPENDENT claim, return ONLY what it ADDS to the claim it depends from. Do not repeat "
    "the parent's requirements; they are tracked against the parent.\n"
    "- Do not split a single requirement into its grammatical parts. \"a hollow grip portion "
    "extending between a front and a rear housing section\" is ONE limitation, not three.\n"
    "- Drop pure preamble (\"A grip unit for a vacuum handling apparatus, comprising:\") unless it "
    "carries a real structural or functional requirement.\n"
    "- 2 to 8 limitations for an independent claim; 1 to 3 for a dependent one.\n"
    "\n"
    'Return ONLY JSON: {"claims":[{"item":"<the claim label verbatim, e.g. \\"claim 3\\">",'
    '"depends_on":<claim number or null>,"limitations":["...","..."]}]} with one entry per claim, '
    "in the order given."
)


def split_claims(claim_items, use_llm=True, log=print):
    """[claim] -> [limitation]. Each limitation is independently searchable and independently owned.

    A limitation carries the claim it came from, whether that claim is independent, and which claim
    it inherits from, because a dependent claim is only invalid if its parent is too and the report
    has to be able to say so.
    """
    claims = [c for c in (claim_items or []) if str(c.get("text") or "").strip()]
    if not claims:
        return []
    parsed = {}
    if use_llm:
        try:
            import llm
            for i in range(0, len(claims), BATCH):
                chunk = claims[i:i + BATCH]
                out = llm.chat_json(_SPLIT_SYS, json.dumps(
                    {"claims": [{"item": c["label"], "text": str(c["text"])[:4000]}
                                for c in chunk]}, ensure_ascii=False), max_tokens=6000) or {}
                for row in (out.get("claims") or []):
                    if not isinstance(row, dict):
                        continue
                    lims = [" ".join(str(x).split()) for x in (row.get("limitations") or [])
                            if len(str(x).split()) >= 4]
                    if lims:
                        parsed[str(row.get("item") or "").strip()] = {
                            "limitations": lims[:12], "depends_on": row.get("depends_on")}
        except Exception:
            import traceback
            traceback.print_exc()

    out = []
    for c in claims:
        label = c["label"]
        got = parsed.get(label) or {}
        lims = got.get("limitations")
        source = "model"
        if not lims:
            #  STRUCTURAL FALLBACK, never an empty list. A claim with no limitations is a claim
            #  the ledger cannot track, and it would silently drop out of the stopping rule.
            lims = _clauses(c["text"]) or [" ".join(str(c["text"]).split())[:600]]
            source = "structural"
        dep = got.get("depends_on")
        if dep is None:
            dep = parent_of(c["text"])
        for j, text in enumerate(lims):
            out.append({
                "id": f"{label}[{chr(97 + j)}]",
                "claim_label": label,
                "claim_no": c.get("claim_no"),
                "index": j,
                "text": text[:1200],
                "independent": bool(c.get("independent")),
                "depends_on": dep,
                "source": source,
            })
            if len(out) >= MAX_LIMITATIONS:
                log(f"[limitations] capped at {MAX_LIMITATIONS}; "
                    f"{len(claims)} claims produced more")
                return out
    n_model = sum(1 for l in out if l["source"] == "model")
    log(f"[limitations] {len(claims)} claims -> {len(out)} limitations "
        f"({n_model} split by the model, {len(out) - n_model} structurally)")
    return out


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------
class Ledger:
    """One row per limitation, and the stopping rule for a Type B search.

    Evidence is only what the reading grounded — a verbatim quote found in the reference and
    located in a passage — so a limitation is never marked covered by a retrieval score.
    """

    def __init__(self, limitations, cover_min=COVER_MIN):
        self.limitations = list(limitations or [])
        self.cover_min = max(1, int(cover_min))
        self.by_id = {l["id"]: l for l in self.limitations}
        self.evidence = {l["id"]: [] for l in self.limitations}

    # -- filling it in -----------------------------------------------------
    def add(self, lim_id, pub, verdict, quote="", location="", date=None, confidence=0.0):
        if lim_id not in self.evidence or verdict not in _W or _W[verdict] <= 0:
            return False
        if any(e["pub"] == pub for e in self.evidence[lim_id]):
            return False
        self.evidence[lim_id].append({
            "pub": pub, "verdict": verdict, "quote": quote[:400], "location": location,
            "date": str(date or ""), "confidence": round(float(confidence or 0.0), 2)})
        self.evidence[lim_id].sort(key=lambda e: (-_W.get(e["verdict"], 0), -e["confidence"]))
        return True

    def ingest_charts(self, charts, dates=None):
        """Read a deep_analysis chart set. -> cells recorded.

        Rows are matched on the limitation id, which is what the reader was asked about when the
        limitations were charted directly. A run that charted whole claims instead contributes
        nothing here rather than guessing which limitation a claim-level verdict belongs to — an
        invented mapping is worse than an empty ledger, because it reads as coverage.
        """
        dates = dates or {}
        n = 0
        for ref in (charts or []):
            if ref.get("method") != "llm":
                continue
            pub = ref.get("pub")
            for row in (ref.get("claims") or []):
                if row.get("grounding") != "verified":
                    continue
                if self.add(str(row.get("item") or ""), pub, row.get("verdict", "absent"),
                            row.get("quote") or "", row.get("location") or "",
                            dates.get(pub), row.get("confidence") or 0.0):
                    n += 1
        return n

    # -- reading it out ----------------------------------------------------
    def status(self, lim_id):
        ev = self.evidence.get(lim_id) or []
        strong = [e for e in ev if e["verdict"] == "disclosed"]
        if len(strong) >= self.cover_min:
            return "covered"
        if ev:
            return "partial"
        return "uncovered"

    def uncovered(self, include_partial=True):
        """The limitations still to be searched for, weakest and most load-bearing first.

        Independent-claim limitations first: an independent claim with a hole is the hole that
        matters, because every dependent claim survives with it.
        """
        rows = [l for l in self.limitations
                if self.status(l["id"]) == "uncovered"
                or (include_partial and self.status(l["id"]) == "partial")]
        rows.sort(key=lambda l: (not l["independent"],
                                 len(self.evidence.get(l["id"]) or []),
                                 l.get("claim_no") or 0, l["index"]))
        return rows

    def claim_status(self, label):
        """(status, anticipating publications) for a whole claim.

        ANTICIPATED means one single document disclosed every limitation — the §102 kill. It is
        computed, never asserted, and it is the single most valuable thing this ledger produces.
        """
        lims = [l for l in self.limitations if l["claim_label"] == label]
        if not lims:
            return "unknown", []
        per_pub = {}
        for l in lims:
            for e in self.evidence.get(l["id"]) or []:
                if e["verdict"] == "disclosed":
                    per_pub.setdefault(e["pub"], set()).add(l["id"])
        need = {l["id"] for l in lims}
        anticipators = sorted(p for p, got in per_pub.items() if got >= need)
        if anticipators:
            return "anticipated", anticipators
        st = [self.status(l["id"]) for l in lims]
        if all(s == "covered" for s in st):
            return "covered", []
        if any(s != "uncovered" for s in st):
            return "partial", []
        return "uncovered", []

    def done(self):
        """The stopping rule. True when nothing is left uncovered."""
        return not any(self.status(l["id"]) == "uncovered" for l in self.limitations)

    def summary(self):
        counts = {"covered": 0, "partial": 0, "uncovered": 0}
        for l in self.limitations:
            counts[self.status(l["id"])] += 1
        claims = {}
        for label in sorted({l["claim_label"] for l in self.limitations}):
            st, anticipators = self.claim_status(label)
            claims[label] = {"status": st, "anticipated_by": anticipators[:6]}
        return {
            "n_limitations": len(self.limitations),
            "n_claims": len(claims),
            "cover_min": self.cover_min,
            "counts": counts,
            "done": self.done(),
            "claims": claims,
            "anticipated": sorted(l for l, v in claims.items() if v["status"] == "anticipated"),
            "uncovered": [l["id"] for l in self.limitations
                          if self.status(l["id"]) == "uncovered"][:60],
        }

    def to_dict(self):
        return {
            "limitations": [dict(l, status=self.status(l["id"]),
                                 evidence=(self.evidence.get(l["id"]) or [])[:8])
                            for l in self.limitations],
            "summary": self.summary(),
        }


def as_chart_items(limitations):
    """Limitations in the shape deep_analysis.analyse_reference charts.

    The reader already answers a list of labelled items against a full text with a grounded quote
    and a located coordinate. A limitation is just a better item than a whole claim, so it goes
    through exactly that machinery rather than a second one — the label IS the ledger key, which is
    what lets ingest_charts match without guessing.
    """
    return [{"label": l["id"], "claim_no": l.get("claim_no"), "text": l["text"],
             "independent": bool(l.get("independent"))} for l in (limitations or [])]
