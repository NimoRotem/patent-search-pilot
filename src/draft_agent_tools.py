"""What the drafting agent can ask the corpus while it drafts: a search, and a claim chart.

WHY THE AGENT NEEDS ITS OWN SEARCH
    Until now the drafting agent could only LOOK UP a publication it had already been handed.
    The art it was handed came from a search a person ran from the Research panel, on the draft
    as it stood, and was attached whole. So the first draft of most projects was written against
    nothing at all, and every later claim amendment was made without asking the corpus what sat
    next to the amended wording. A drafter who cannot search while drafting is drafting blind, and
    thirteen of the twenty-two projects on this server were.

    ``search_corpus`` is the dense channel of the product's own search, run on one query the agent
    writes, against the same 5M-publication corpus, with the passage that matched. It costs one
    embedding call and one ANN scan and answers in seconds. ``attach`` puts what it found into the
    project exactly as the Sources tab would, so the citation check, the IDS and the workspace all
    see the same list.

WHY THE CHART IS A TOOL AND NOT AN OPINION
    The agent can read the references and form a view of what they disclose. That view is the
    drafter's own, and a drafter grading their own claims is the one reading nobody should trust.
    ``novelty`` charts the CURRENT claims (the unpublished files in the workspace, not the last
    version) against every attached reference with ``claim_chart.build_chart``: the product's
    grounded, refuted-on-the-other-side chart, whose quotes must be found in the reference text or
    are thrown away. What comes back is the same measurement the deepest research tier makes, on
    demand, before a publish rather than after one.

    The headline is the NEAREST SINGLE REFERENCE per independent claim, because novelty falls one
    document at a time, and the half a drafter acts on is the list of elements NO reference was
    found to disclose. The combination figure is reported separately and never folded in.

WHAT IT IS NOT
    Not a patentability opinion, and every rendering says so. It measures what this corpus, read
    this way, shows about these claims right now.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping, Sequence

import draft_cite
import draft_qa

SEARCH_TOP_DEFAULT = 10
SEARCH_TOP_MAX = 25
SEARCH_QUERY_MAX_CHARS = 6000
SEARCH_POOL = 300                      # chunks pulled per pool before grouping by publication
ATTACH_MAX = 10

NOVELTY_MAX_REFERENCES = 10
NOVELTY_MAX_CLAIMS = 4
NOVELTY_ELEMENT_BATCH = 12            # claim_chart.MAX_ELEMENTS: more than this is silently cut
NOVELTY_WORKERS = 6
NOVELTY_RUNS_PER_DAY = 24
NOVELTY_JOB_TTL = 3600
SPEND_APP = "patent-drafting"
#  Vertex gemini-2.5-flash list price per million tokens, which is what claim_chart runs on. The
#  fleet guard prices a model it does not know at the dearest tier, which would make one chart
#  read like a day of Opus.
_FLASH_USD_PER_M_IN = 0.30
_FLASH_USD_PER_M_OUT = 2.50

_RUNS: dict[int, list[float]] = {}
_RUNS_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


class ToolError(ValueError):
    """The agent asked for something the tool cannot do, in words it can act on."""


# =============================================================================================
# Search
# =============================================================================================
_ALL_SQL = ("SELECT c.publication_id, c.kind, c.coord, left(c.text, 700) AS text, "
            "1-(c.embedding <=> %s::vector) AS score FROM chunks c "
            "WHERE c.embedding IS NOT NULL ORDER BY c.embedding <=> %s::vector LIMIT %s")
#  The abstract/whole pool exists because 84% of the corpus is abstract-only and a long modern
#  patent gets a hundred chances in the all-kinds top-K where a thin old one gets one. Weighted
#  below a passage match, exactly as the product's own channel is.
_BRIEF_SQL = ("SELECT c.publication_id, c.kind, c.coord, left(c.text, 700) AS text, "
              "1-(c.embedding <=> %s::vector) AS score FROM chunks c "
              "WHERE c.embedding IS NOT NULL AND c.kind IN ('abstract','whole') "
              "ORDER BY c.embedding <=> %s::vector LIMIT %s")
_BRIEF_WEIGHT = 0.97


def relevancy(cosine: Any) -> int:
    """The 1-99 figure the report cards show for a cosine, same calibration as the cards."""
    try:
        value = float(cosine or 0)
    except (TypeError, ValueError):
        value = 0.0
    pct = (value - 0.35) / (0.90 - 0.35) * 100.0
    return int(max(1, min(99, round(pct))))


def _passage_label(kind: str, coord: Any) -> str:
    coord = coord if isinstance(coord, Mapping) else {}
    kind = str(kind or "")
    if kind.startswith("claim"):
        number = coord.get("claim_no")
        return f"claim {number}" if number else "a claim"
    if kind == "abstract":
        return "abstract"
    if kind == "whole":
        return "the document as a whole"
    para = coord.get("para") or coord.get("paragraph") or coord.get("n")
    return f"paragraph {para}" if para else "description"


def search_corpus(query: str, *, top: int = SEARCH_TOP_DEFAULT,
                  attached: Sequence[str] = ()) -> dict[str, Any]:
    """The nearest publications to one query, each with the passage that put it there.

    Dense retrieval only, on purpose: it is the channel that answers in seconds and needs no
    planner, and a drafter asking "what is near this claim" wants the neighbourhood, not the
    whole attack. The Research panel is still there for the whole attack.
    """
    text = " ".join(str(query or "").split())[:SEARCH_QUERY_MAX_CHARS]
    if len(text) < 12:
        raise ToolError("Say what to search for: a claim, a paragraph of the description, or a "
                        "sentence describing the invention.")
    top = max(1, min(int(top or SEARCH_TOP_DEFAULT), SEARCH_TOP_MAX))
    import db
    import embed
    started = time.time()
    qvec = embed.embed_query(text)
    vector = embed._vec(qvec)
    pooled: dict[int, dict[str, Any]] = {}
    conn = db.connect(autocommit=True, readonly=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET hnsw.ef_search = 200")
            for sql, weight in ((_ALL_SQL, 1.0), (_BRIEF_SQL, _BRIEF_WEIGHT)):
                cur.execute(sql, [vector, vector, SEARCH_POOL])
                for row in cur.fetchall():
                    pid = int(row["publication_id"])
                    score = float(weight) * float(row["score"] or 0.0)
                    if score > pooled.get(pid, {}).get("score", -1.0):
                        pooled[pid] = {"score": score, "kind": row.get("kind") or "",
                                       "coord": row.get("coord"),
                                       "text": (row.get("text") or "").strip()}
        if not pooled:
            return {"query": text, "hits": [], "seconds": round(time.time() - started, 1)}
        ids = list(pooled)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, publication_number, kind_code, country, title, abstract, "
                "publication_date, earliest_priority_date, simple_family_id "
                "FROM publications WHERE id = ANY(%s)", (ids,))
            records = {int(row["id"]): dict(row) for row in cur.fetchall()}
    finally:
        conn.close()

    attached_keys = {draft_cite.normalize(item) for item in attached
                     if draft_cite.normalize(item)}
    ranked = sorted(pooled.items(), key=lambda kv: -kv[1]["score"])
    hits: list[dict[str, Any]] = []
    seen_families: set[str] = set()
    for pid, best in ranked:
        record = records.get(pid)
        if not record:
            continue
        family = str(record.get("simple_family_id") or f"pub-{pid}")
        if family in seen_families:
            continue
        seen_families.add(family)
        pub = str(record.get("publication_number") or "")
        canonical = draft_cite.normalize(pub) or pub
        hits.append({
            "pub": canonical,
            "title": (record.get("title") or "").strip()[:240],
            "publication_date": str(record.get("publication_date") or "")[:10],
            "priority_date": str(record.get("earliest_priority_date") or "")[:10],
            "country": record.get("country") or "",
            "score": round(best["score"], 4),
            "relevancy": relevancy(best["score"]),
            "matched": _passage_label(best["kind"], best["coord"]),
            "passage": best["text"][:600],
            "abstract": (record.get("abstract") or "").strip()[:500],
            "attached": canonical in attached_keys,
            "url": f"https://patents.google.com/patent/{canonical.replace('-', '')}/en",
        })
        if len(hits) >= top:
            break
    return {"query": text, "hits": hits, "seconds": round(time.time() - started, 1)}


def render_search(result: Mapping[str, Any]) -> str:
    hits = list(result.get("hits") or [])
    lines = [f"CORPUS SEARCH ({len(hits)} nearest publication(s), {result.get('seconds', 0)}s). "
             "Ranked by the passage nearest your text; nothing here has been read against your "
             "claims. Read a reference before you rely on it, and attach it before you cite it.",
             f"Query: {str(result.get('query') or '')[:300]}", ""]
    if not hits:
        lines.append("Nothing came back. Try a plainer description of the mechanism.")
    for index, hit in enumerate(hits, 1):
        flag = "  [already attached]" if hit.get("attached") else ""
        lines.append(f"{index:2d}. {hit['pub']}  relevancy {hit['relevancy']}  "
                     f"{hit.get('publication_date') or '????'}  {hit.get('title') or ''}{flag}")
        if hit.get("passage"):
            lines.append(f"     matched in {hit.get('matched')}: "
                         f"{' '.join(hit['passage'].split())[:300]}")
    lines += ["", "To attach: python3 tools/prior_art_search.py --attach PUB [PUB ...]  "
                  "(then it is in prior_art/ and may be cited as [REF:PUB])"]
    return "\n".join(lines)


# =============================================================================================
# Attaching what it found
# =============================================================================================
def snapshot_for(record: Mapping[str, Any]) -> dict[str, Any]:
    """The reference snapshot the workspace and the Sources tab read, from a resolved record."""
    return {"publication_number": record.get("publication_number") or "",
            "title": record.get("title") or "",
            "abstract": record.get("abstract") or "",
            "claims": record.get("claims") or "",
            "description": (record.get("description") or "")[:60_000],
            "publication_date": record.get("publication_date") or "",
            "filing_date": record.get("filing_date") or "",
            "priority_date": record.get("priority_date") or "",
            "assignee": record.get("assignee") or "",
            "source_url": record.get("url") or ""}


def attach(project_id: int, publications: Sequence[str], *, repository: Any,
           reason: str = "") -> list[dict[str, Any]]:
    """Resolve each publication against the corpus and add it to the project as agent-found art."""
    out: list[dict[str, Any]] = []
    why = ("Found by the drafting agent's own corpus search"
           + (f": {' '.join(str(reason).split())[:600]}" if reason else "") + ".")
    for raw in list(publications)[:ATTACH_MAX]:
        canonical = draft_cite.normalize(raw)
        if not canonical:
            out.append({"pub": str(raw)[:64], "attached": False,
                        "reason": "not a publication number this corpus can read"})
            continue
        record = draft_cite.resolve(canonical, with_text=True, allow_remote=False)
        if not record.get("found"):
            out.append({"pub": canonical, "attached": False,
                        "reason": record.get("reason") or "not in the local corpus"})
            continue
        repository.add_reference(
            int(project_id), publication_number=canonical, title=record.get("title") or "",
            source_url=record.get("url") or None, relevance_summary=why,
            snapshot=snapshot_for(record), origin="agent")
        out.append({"pub": canonical, "attached": True, "title": record.get("title") or ""})
    return out


# =============================================================================================
# Claims into elements
# =============================================================================================
_TRANSITION_RE = re.compile(
    r"\b(comprising|comprises|consisting essentially of|consisting of|including|includes|"
    r"characterized in that|characterised in that)\b\s*:?\s*", re.IGNORECASE)
_ELEMENT_SPLIT_RE = re.compile(r";\s*|\n+")
_WHEREIN_SPLIT_RE = re.compile(r",\s*(?=(?:and\s+)?wherein\b)", re.IGNORECASE)
_LEADING_JOIN_RE = re.compile(r"^(?:and|or)\s+", re.IGNORECASE)


def claim_elements(claim_text: str) -> dict[str, Any]:
    """The preamble and the elements of one claim, read the way a chart is built.

    Elements are the clauses the claim itself separates with semicolons or line breaks, after the
    transition. A claim written as one run-on sentence is split at its wherein clauses. The
    preamble is kept apart because "a clamp comprising" is disclosed by every clamp and counting
    it would put a floor under every score.
    """
    text = " ".join(str(claim_text or "").replace("\r", "\n").split(" "))
    text = re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", text.strip())
    match = _TRANSITION_RE.search(text)
    if match:
        preamble = text[:match.start()].strip()
        rest = text[match.end():]
    else:
        preamble, rest = "", text
    pieces = [piece for piece in _ELEMENT_SPLIT_RE.split(rest) if piece and piece.strip()]
    if len(pieces) <= 1:
        pieces = [piece for piece in _WHEREIN_SPLIT_RE.split(rest) if piece and piece.strip()]
    elements: list[str] = []
    for piece in pieces:
        clean = " ".join(piece.split()).strip(" ,.:")
        clean = _LEADING_JOIN_RE.sub("", clean).strip(" ,.:")
        if len(clean.split()) < 3:
            continue
        elements.append(clean[:400])
    return {"preamble": " ".join(preamble.split())[:300], "elements": elements}


def independent_claims(claims_text: str) -> list[dict[str, Any]]:
    """Every independent claim with its elements, in claim order, capped for the chart."""
    chart_map = draft_qa.claim_map(str(claims_text or ""))
    out = []
    for row in chart_map.get("claims") or []:
        if not row.get("independent"):
            continue
        parsed = claim_elements(row.get("text") or "")
        if not parsed["elements"]:
            continue
        out.append({"number": int(row["number"]), "text": str(row.get("text") or "")[:4000],
                    "preamble": parsed["preamble"], "elements": parsed["elements"]})
        if len(out) >= NOVELTY_MAX_CLAIMS:
            break
    return out


# =============================================================================================
# The references as chartable text
# =============================================================================================
def _split_paragraphs(text: str, *, limit: int, chars: int = 900) -> list[str]:
    out = []
    for block in re.split(r"\n\s*\n|\r\n\s*\r\n", str(text or "")):
        clean = " ".join(block.split())
        if len(clean) < 20:
            continue
        out.append(clean[:chars])
        if len(out) >= limit:
            break
    return out


def reference_passages(reference: Mapping[str, Any]) -> dict[str, Any]:
    """Citable units for one attached reference: the corpus rows when it has them, else its snapshot.

    A reference the search attached from an external API, or one the user uploaded, is not in the
    local corpus; charting it from nothing would be a chart of nothing. Its stored snapshot holds
    the abstract, claims and description the search or the upload delivered, and those are
    quoted with a label so a grounded quote still says where it came from.
    """
    pub = str(reference.get("publication_number") or "")
    try:
        import claim_chart
        loaded = claim_chart._load_reference(pub)
        if loaded.get("found") and loaded.get("passages"):
            loaded["source"] = "corpus"
            return loaded
    except Exception:                                          # noqa: BLE001 - fall back to the snapshot
        traceback.print_exc()
    snapshot = reference.get("snapshot") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except ValueError:
            snapshot = {}
    passages: list[dict[str, Any]] = []
    abstract = " ".join(str(snapshot.get("abstract") or "").split())
    if abstract:
        passages.append({"kind": "abstract", "coord": {}, "label": "abstract",
                         "text": abstract[:2000]})
    claims_text = str(snapshot.get("claims") or "")
    for index, claim in enumerate(re.split(r"\n\s*\n", claims_text), 1):
        clean = " ".join(claim.split())
        if len(clean) < 15:
            continue
        number_match = re.match(r"^(\d{1,3})\s*[.)]\s*", clean)
        number = int(number_match.group(1)) if number_match else index
        passages.append({"kind": "claim", "coord": {"claim_no": number},
                         "label": f"claim {number}", "text": clean[:1500]})
        if len(passages) >= 40:
            break
    room = max(0, 60 - len(passages))
    body = str(snapshot.get("description") or snapshot.get("body") or
               snapshot.get("prompt_context") or "")
    for index, paragraph in enumerate(_split_paragraphs(body, limit=room), 1):
        passages.append({"kind": "description", "coord": {"para": index},
                         "label": f"description paragraph {index}", "text": paragraph})
    return {"found": bool(passages), "pub": pub, "source": "snapshot",
            "title": str(reference.get("title") or snapshot.get("title") or ""),
            "passages": passages}


def document_as_reference(document: Mapping[str, Any], index: int) -> dict[str, Any]:
    """An uploaded prior-art document, keyed the way the workspace INDEX keys it."""
    key = draft_cite.normalize(document.get("publication_number")) or f"UPLOAD-{index:02d}"
    body = str(document.get("body") or "")
    passages = [{"kind": "description", "coord": {"para": n},
                 "label": f"upload paragraph {n}", "text": paragraph}
                for n, paragraph in enumerate(_split_paragraphs(body, limit=60), 1)]
    return {"publication_number": key,
            "title": str(document.get("title") or document.get("filename") or "")[:240],
            "passages_override": {"found": bool(passages), "pub": key, "source": "upload",
                                  "title": str(document.get("title") or "")[:240],
                                  "passages": passages}}


# =============================================================================================
# The chart
# =============================================================================================
def _chart_pair(elements: Sequence[str], pub: str, ref: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One reference against one claim's elements, in batches the chart will not truncate."""
    import claim_chart
    rows: list[dict[str, Any]] = []
    elements = list(elements)
    for start in range(0, len(elements), NOVELTY_ELEMENT_BATCH):
        batch = elements[start:start + NOVELTY_ELEMENT_BATCH]
        chart = claim_chart.build_chart(batch, pub, ref=dict(ref))
        by_element = {str(row.get("element") or ""): row for row in chart.get("rows") or []}
        for element in batch:
            row = by_element.get(element) or {"element": element, "verdict": "absent"}
            rows.append({"element": element,
                         "verdict": str(row.get("verdict") or "absent"),
                         "entailment": str(row.get("entailment") or ""),
                         "quote": str(row.get("quote") or "")[:200],
                         "location": str(row.get("location") or "")[:80],
                         "method": str(chart.get("method") or "")})
    return rows


def _disclosed(row: Mapping[str, Any]) -> bool:
    return row.get("verdict") == "disclosed" and row.get("entailment") != "refuted"


def novelty(*, claims_text: str, references: Sequence[Mapping[str, Any]],
            documents: Sequence[Mapping[str, Any]] = (), workers: int = NOVELTY_WORKERS,
            publications: Sequence[str] = (),
            progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Chart every independent claim against every attached reference and reduce it honestly."""
    claims = independent_claims(claims_text)
    if not claims:
        raise ToolError("No independent claim could be read from draft/09-claims.md. Number the "
                        "claims and separate the elements of each with semicolons.")
    wanted = {draft_cite.normalize(item) for item in publications if draft_cite.normalize(item)}
    targets: list[dict[str, Any]] = []
    for reference in references:
        pub = draft_cite.normalize(reference.get("publication_number")) or \
            str(reference.get("publication_number") or "")
        if wanted and pub not in wanted:
            continue
        targets.append({"pub": pub, "title": str(reference.get("title") or "")[:240],
                        "reference": reference})
    for index, document in enumerate(
            [d for d in documents if str(d.get("kind")) == "prior_art"], 1):
        entry = document_as_reference(document, index)
        if wanted and entry["publication_number"] not in wanted:
            continue
        targets.append({"pub": entry["publication_number"], "title": entry["title"],
                        "reference": {"publication_number": entry["publication_number"],
                                      "title": entry["title"]},
                        "passages": entry["passages_override"]})
    if not targets:
        raise ToolError("No prior art is attached to this draft, so there is nothing to chart "
                        "against. Run tools/prior_art_search.py --attach first.")
    targets = targets[:NOVELTY_MAX_REFERENCES]

    started = time.time()
    loaded: dict[str, dict[str, Any]] = {}
    for target in targets:
        loaded[target["pub"]] = target.get("passages") or reference_passages(target["reference"])

    pairs = [(claim, target) for claim in claims for target in targets]
    results: dict[tuple[int, str], list[dict[str, Any]]] = {}
    errors: list[str] = []
    done = 0

    def _one(pair):
        claim, target = pair
        ref = loaded[target["pub"]]
        if not ref.get("found") or not ref.get("passages"):
            return (claim["number"], target["pub"],
                    [{"element": e, "verdict": "absent", "entailment": "", "quote": "",
                      "location": "", "method": "no-text"} for e in claim["elements"]])
        return (claim["number"], target["pub"], _chart_pair(claim["elements"], target["pub"], ref))

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as pool:
        for future in [pool.submit(_one, pair) for pair in pairs]:
            try:
                number, pub, rows = future.result()
                results[(number, pub)] = rows
            except Exception as exc:                            # noqa: BLE001 - one pair, not the run
                traceback.print_exc()
                errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            done += 1
            if progress:
                progress(f"{done}/{len(pairs)} charted")

    per_claim: list[dict[str, Any]] = []
    for claim in claims:
        n = len(claim["elements"])
        columns = []
        disclosed_anywhere: set[int] = set()
        partial_anywhere: set[int] = set()
        for target in targets:
            rows = results.get((claim["number"], target["pub"])) or []
            disclosed = [i for i, row in enumerate(rows) if _disclosed(row)]
            partial = [i for i, row in enumerate(rows)
                       if row.get("verdict") in ("partial", "uncertain")]
            disclosed_anywhere.update(disclosed)
            partial_anywhere.update(partial)
            columns.append({
                "pub": target["pub"], "title": target["title"],
                "source": loaded[target["pub"]].get("source") or "",
                "disclosed": len(disclosed), "partial": len(partial),
                "coverage": round(len(disclosed) / n, 4) if n else 0.0,
                "rows": rows,
                "charted": bool(rows) and not all(r.get("method") == "no-text" for r in rows),
            })
        columns.sort(key=lambda item: (-item["coverage"], -item["partial"], item["pub"]))
        closest = columns[0] if columns else None
        uncovered = [claim["elements"][i] for i in range(n)
                     if i not in disclosed_anywhere and i not in partial_anywhere]
        weak = [claim["elements"][i] for i in range(n)
                if i not in disclosed_anywhere and i in partial_anywhere]
        per_claim.append({
            "number": claim["number"], "preamble": claim["preamble"],
            "elements": claim["elements"], "n_elements": n,
            "closest": ({"pub": closest["pub"], "title": closest["title"],
                         "disclosed": closest["disclosed"], "coverage": closest["coverage"]}
                        if closest else None),
            "uncovered": uncovered, "weak": weak,
            "combination": round(len(disclosed_anywhere) / n, 4) if n else 0.0,
            "references": columns,
        })
    headline = max((item["closest"]["coverage"] for item in per_claim if item["closest"]),
                   default=0.0)
    return {
        "ok": True,
        "claims": per_claim,
        "n_references": len(targets),
        "references": [{"pub": t["pub"], "title": t["title"],
                        "source": loaded[t["pub"]].get("source") or "",
                        "has_text": bool(loaded[t["pub"]].get("passages"))} for t in targets],
        "closest_coverage": round(float(headline), 4),
        "errors": errors[:10],
        "seconds": round(time.time() - started, 1),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": ("Charted by the product's grounded claim chart on the corpus text of each "
                 "reference. A measurement of what this reading found, not an opinion on "
                 "patentability."),
    }


def compact(reading: Mapping[str, Any]) -> dict[str, Any]:
    """The reading with the per-cell rows trimmed, for storage on the project and the page."""
    out = json.loads(json.dumps(reading, default=str))
    for claim in out.get("claims") or []:
        for column in claim.get("references") or []:
            column["rows"] = [{"element": r.get("element", "")[:200], "verdict": r.get("verdict"),
                               "quote": r.get("quote", "")[:160],
                               "location": r.get("location", "")}
                              for r in column.get("rows") or []]
    return out


def render_novelty(reading: Mapping[str, Any]) -> str:
    """The reading as the agent sees it in its terminal."""
    lines = [f"NOVELTY CHECK on the claims in draft/09-claims.md as they stand now "
             f"({reading.get('n_references')} reference(s) charted, {reading.get('seconds')}s).",
             "Grounded claim chart: a cell is DISCLOSED only when a quote from the reference was "
             "found in its text and survived a second pass arguing the other side. Novelty falls "
             "to ONE document, so the figure that matters is the nearest single reference.", ""]
    for claim in reading.get("claims") or []:
        n = claim["n_elements"]
        closest = claim.get("closest") or {}
        lines.append(f"CLAIM {claim['number']} ({n} elements after the preamble)")
        if closest:
            lines.append(f"  nearest single reference: {closest['pub']} discloses "
                         f"{closest['disclosed']} of {n} ({closest['coverage']:.0%})  "
                         f"{closest.get('title') or ''}")
        lines.append(f"  every attached reference together reaches {claim['combination']:.0%} "
                     f"of the elements (obviousness is a different question and is NOT folded "
                     f"into the figure above)")
        if claim.get("uncovered"):
            lines.append("  elements NO reference was found to disclose (the ground the claim "
                         "stands on):")
            lines += [f"    - {item}" for item in claim["uncovered"]]
        else:
            lines.append("  every element was found, at least partly, in some reference: the "
                         "claim needs a feature that is in the disclosure and in none of the art")
        if claim.get("weak"):
            lines.append("  elements met only partly or uncertainly (read these references "
                         "yourself before relying on the gap):")
            lines += [f"    - {item}" for item in claim["weak"]]
        lines.append("  per reference:")
        for column in claim.get("references") or []:
            lines.append(f"    {column['pub']:<22} {column['disclosed']}/{n} disclosed, "
                         f"{column['partial']} partial/uncertain"
                         + ("" if column.get("charted") else "  (no text to chart)"))
            for row in column.get("rows") or []:
                if _disclosed(row) or row.get("verdict") in ("partial", "uncertain"):
                    lines.append(f"      [{row.get('verdict')}] {row['element'][:90]}"
                                 f"{('  <- ' + row['location']) if row.get('location') else ''}")
        lines.append("")
    if reading.get("errors"):
        lines.append("Some pairs could not be charted: " + "; ".join(reading["errors"]))
    lines.append("Remember: a lower figure bought by narrowing is not progress. Move the claim "
                 "onto a disclosed feature the art lacks, keep it as broad as the disclosure "
                 "supports, and run this again.")
    return "\n".join(lines)


# =============================================================================================
# Budget and jobs
# =============================================================================================
def check_budget(project_id: int) -> None:
    now = time.time()
    with _RUNS_LOCK:
        stamps = [t for t in _RUNS.get(int(project_id), []) if now - t < 86400]
        if len(stamps) >= NOVELTY_RUNS_PER_DAY:
            raise ToolError(f"This draft has run {NOVELTY_RUNS_PER_DAY} novelty checks in the "
                            "last day, which is the ceiling. Draft first, measure after.")
        stamps.append(now)
        _RUNS[int(project_id)] = stamps
    try:
        from llm_spend_guard import SpendGuard
        SpendGuard(SPEND_APP).check(need_usd=0.5)
    except ImportError:
        pass


def record_spend(project_id: int, before: Mapping[str, Any], after: Mapping[str, Any],
                 *, source: str = "novelty") -> float:
    """Price the Vertex calls a chart made and put them on the draft's own ledger."""
    prompt = max(0, int(after.get("prompt_tokens") or 0) - int(before.get("prompt_tokens") or 0))
    completion = max(0, int(after.get("completion_tokens") or 0) -
                     int(before.get("completion_tokens") or 0))
    calls = max(0, int(after.get("calls") or 0) - int(before.get("calls") or 0))
    usd = prompt / 1e6 * _FLASH_USD_PER_M_IN + completion / 1e6 * _FLASH_USD_PER_M_OUT
    try:
        import draft_usage
        import llm
        draft_usage.record(int(project_id), source=source, model=llm.AGENT_MODEL,
                           tokens={"tokens_input": prompt, "tokens_output": completion},
                           usd=usd, calls=calls)
    except Exception:                                          # noqa: BLE001 - a counter never fails a run
        traceback.print_exc()
    try:
        from llm_spend_guard import SpendGuard
        import llm
        SpendGuard(SPEND_APP).record(
            usd=usd, model=llm.AGENT_MODEL,
            usage={"input_tokens": prompt, "output_tokens": completion},
            provider="vertex", route="api", detail=f"{source} p{int(project_id)}")
    except Exception:                                          # noqa: BLE001
        pass
    return round(usd, 4)


def start_job(project_id: int, work: Callable[[Callable[[str], None]], dict[str, Any]],
              on_done: Callable[[dict[str, Any]], None] | None = None) -> str:
    """Run a chart in the background and hand back a ticket the tool polls.

    A chart of four claims against ten references is forty model calls, which is a minute or
    two; the agent's shell tool gives up on a command at two minutes, and a request that dies in
    the agent's hands is a measurement it never sees. So the request returns at once, the tool
    polls, and a tool that runs out of patience prints the ticket to come back to.
    """
    job_id = uuid.uuid4().hex[:12]
    record = {"id": job_id, "project_id": int(project_id), "status": "running",
              "progress": "starting", "started": time.time(), "result": None, "error": ""}
    with _JOBS_LOCK:
        stale = [key for key, item in _JOBS.items()
                 if time.time() - float(item.get("started") or 0) > NOVELTY_JOB_TTL]
        for key in stale:
            _JOBS.pop(key, None)
        _JOBS[job_id] = record

    def _progress(text: str) -> None:
        record["progress"] = str(text)[:120]

    def _run() -> None:
        try:
            result = work(_progress)
            record["result"] = result
            record["status"] = "done"
            if on_done:
                try:
                    on_done(result)
                except Exception:                              # noqa: BLE001
                    traceback.print_exc()
        except ToolError as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
        except Exception as exc:                               # noqa: BLE001 - the agent reads this
            traceback.print_exc()
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    threading.Thread(target=_run, name=f"draft-novelty-{project_id}-{job_id}",
                     daemon=True).start()
    return job_id


def job(job_id: str, project_id: int) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        record = _JOBS.get(str(job_id))
    if not record or int(record.get("project_id") or 0) != int(project_id):
        return None
    return {"id": record["id"], "status": record["status"], "progress": record["progress"],
            "seconds": round(time.time() - float(record["started"]), 1),
            "result": record.get("result"), "error": record.get("error") or ""}


# =============================================================================================
# Proposals: what the agent may suggest without writing it into the application
# =============================================================================================
_PROPOSAL_HEADING_RE = re.compile(r"^##\s+(?:(\d{1,2})\s*[.):-]\s*)?(.+?)\s*#*\s*$", re.MULTILINE)


def parse_proposals(markdown: str) -> list[dict[str, Any]]:
    """``review/proposals.md`` as a list: one ``## heading`` per proposal, its text underneath."""
    text = str(markdown or "").replace("\r\n", "\n")
    marks = list(_PROPOSAL_HEADING_RE.finditer(text))
    out: list[dict[str, Any]] = []
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = text[match.end():end].strip()
        title = " ".join(match.group(2).split())[:200]
        if not title:
            continue
        out.append({"no": int(match.group(1)) if match.group(1) else None,
                    "title": title, "body": body[:6000]})
    return out


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def merge_proposals(existing: Sequence[Mapping[str, Any]], parsed: Sequence[Mapping[str, Any]],
                    *, version_no: int | None = None) -> list[dict[str, Any]]:
    """The stored list updated from the file: statuses kept, text refreshed, new ones numbered.

    The file is the agent's outbox and the store is the record. A proposal the agent rewrote
    keeps its number and its status; one it dropped stays in the record (an adopted proposal is
    part of the disclosure now and must not vanish because the agent tidied its file).
    """
    out = [dict(item) for item in existing]
    by_key = {_title_key(item.get("title")): item for item in out}
    next_no = max([int(item.get("no") or 0) for item in out] + [0]) + 1
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for item in parsed:
        key = _title_key(item.get("title"))
        if not key:
            continue
        found = by_key.get(key)
        if found:
            if found.get("status") == "open":
                found["body"] = item.get("body") or found.get("body") or ""
            continue
        entry = {"no": next_no, "title": item["title"], "body": item.get("body") or "",
                 "status": "open", "version_no": version_no, "created_at": stamp}
        next_no += 1
        out.append(entry)
        by_key[key] = entry
    return out


def render_proposals(proposals: Sequence[Mapping[str, Any]]) -> str:
    """The proposals file written back into a rebuilt workspace, with what became of each."""
    lines = ["# Proposals for the inventor", "",
             "Each proposal below is a feature that is NOT in the disclosure. It stays out of "
             "draft/ until the inventor adopts it on the page, at which point it becomes part of "
             "input/disclosure.md and you are told to work it in.", ""]
    for item in proposals:
        status = str(item.get("status") or "open")
        lines.append(f"## {item.get('no')}. {item.get('title')}")
        lines.append(f"Status: {status}")
        lines.append("")
        lines.append(str(item.get("body") or "").strip())
        lines.append("")
    if not proposals:
        lines.append("(none yet)")
    return "\n".join(lines)
