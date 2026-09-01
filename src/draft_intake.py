"""What the drafting intake was given, and what it is.

TWO THINGS, AND THE FIRST ONE IS THE POINT OF THE SECOND
    The intake used to open with a choice: "an invention to describe" or "a draft I already
    have". Nobody needs to be asked that. A patent application announces itself in half a dozen
    ways at once, and a paragraph about a vacuum lifter announces itself by having none of them.
    The search page reached the same conclusion about its own box and says so in as many words:
    three controls asked the user to classify their own input before the page would take it, and
    the page can tell the difference perfectly well by itself.

    So `recognise` decides, and it decides with named, countable signals rather than a model,
    because the answer changes what the drafting agent is told to do and an unexplainable answer
    to that is worse than a radio button. The page prints what it found. If it says "numbered
    claims, five headings, reference numerals in the prose" and the reader disagrees, they can
    see exactly which of those is wrong.

WHY IT MATTERS THAT IT IS RIGHT
    A description becomes "write the first draft of this application". An existing draft becomes
    "take the draft in input/ and improve it, do not discard the user's own text", and the text
    is additionally stored as a source document. Guess the first when the truth is the second and
    an agent rewrites somebody's application from scratch. So the bar is deliberately asymmetric:
    it takes real structure to be called an application, and the threshold is stated here rather
    than tuned in silence.

AND THE MATERIAL ITSELF
    `material_from_extract` turns what `ingest_input` reads out of a PDF or a patent link into
    what a drafting project needs, which is NOT what a search needs. A search runs on a condensed
    brief and bounded vectors; a draft must start from the applicant's verbatim text, because
    every word the agent is allowed to claim has to trace to something the user actually supplied.
    The drawings come with it: starting from an existing patent and moving it to a neighbouring
    invention is the whole use, and that is not possible from text alone.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

#  Where an intake extraction waits between the upload and the form submit. Its own store, in
#  this app's data directory: the search app's document stash lives in another process with
#  another disk path, and it deliberately keeps full text only for an UPLOAD, never for a link.
#  A draft started from a patent link needs that text more than a search does.
STASH = Path(os.environ.get(
    "DRAFT_INTAKE_STASH",
    Path(__file__).resolve().parents[1] / "data" / "intake"))
STASH_TTL_SECONDS = float(os.environ.get("DRAFT_INTAKE_TTL", str(6 * 3600)))
MAX_FIGURES = int(os.environ.get("DRAFT_INTAKE_FIGURES", "24"))
MAX_TEXT_CHARS = 240_000


# =============================================================================================
# Is this an application, or a description of an invention?
# =============================================================================================
#  Every heading a US application carries, in the forms offices and drafters actually write them.
#  Matched at a line start so a sentence mentioning "the background of the problem" is not a
#  heading, which is the difference between a description that talks ABOUT an application and one
#  that IS one.
_HEADINGS = (
    r"CROSS[- ]REFERENCE TO RELATED APPLICATIONS?",
    r"STATEMENT REGARDING FEDERALLY SPONSORED",
    r"FIELD OF (?:THE )?(?:THE )?(?:INVENTION|DISCLOSURE|TECHNOLOGY)",
    r"TECHNICAL FIELD",
    r"BACKGROUND(?: OF THE (?:INVENTION|DISCLOSURE))?",
    r"(?:BRIEF )?SUMMARY(?: OF THE (?:INVENTION|DISCLOSURE))?",
    r"BRIEF DESCRIPTION OF THE (?:DRAWINGS?|FIGURES?|SEVERAL VIEWS)",
    r"DETAILED DESCRIPTION(?: OF THE (?:INVENTION|DISCLOSURE|EMBODIMENTS?|PREFERRED))?",
    r"DESCRIPTION OF THE (?:PREFERRED )?EMBODIMENTS?",
    r"WHAT IS CLAIMED IS",
    r"(?:THE INVENTION )?CLAIMS?(?: IS| ARE)?",
    r"ABSTRACT(?: OF THE DISCLOSURE)?",
    r"INDUSTRIAL APPLICABILITY",
)
_HEADING_RE = re.compile(
    r"^[ \t]*(?:\d+[.)]?[ \t]*)?(?:" + "|".join(_HEADINGS) + r")[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE)

#  "What is claimed is:", "I claim:", "The invention claimed is:". A claim set almost always has
#  one of these, and a description never does.
_CLAIM_PREAMBLE_RE = re.compile(
    r"(?:what\s+is\s+claimed\s+is|the\s+invention\s+claimed\s+is|i\s+claim|we\s+claim|"
    r"claims?\s+what\s+is\s+claimed)\s*[:.]", re.IGNORECASE)

#  A numbered claim in its own right: "1. A device comprising:" / "12. The apparatus of claim 1,"
#  The transitional phrase is what separates a claim from a numbered list item.
_CLAIM_RE = re.compile(
    r"(?:^|\n)\s*(\d{1,3})\s*[.)]\s+(?:A|An|The)\b[^\n]{0,400}?"
    r"\b(?:compris\w+|consist\w+|includ\w+|having|wherein|according to|of claim\s*\d)",
    re.IGNORECASE)

#  A reference numeral in prose: a noun followed by a bare number, the way a detailed description
#  is written and the way ordinary writing never is. "the housing 12", "a sealing lip 34a".
_NUMERAL_RE = re.compile(r"\b[a-z][a-z-]{2,}\s+(\d{1,3}[a-d]?)\b(?!\s*(?:%|mm|cm|m\b|kg|s\b))")

_FIGURE_RE = re.compile(r"\bFIGS?\.?\s*\d", re.IGNORECASE)

_BOILERPLATE = (
    "the present invention relates to",
    "the present disclosure relates to",
    "in one embodiment",
    "in another embodiment",
    "according to an aspect",
    "in accordance with the present invention",
    "it is an object of the invention",
    "the foregoing and other objects",
)

#  HOW MUCH STRUCTURE MAKES AN APPLICATION. Three independent signals, deliberately: any one of
#  them alone is reachable by accident. A person pasting notes may head them "Background", may
#  mention FIG. 1, may even write a claim. Three at once is not an accident, and a real
#  application clears it several times over.
RECOGNISE_THRESHOLD = 3


def recognise(text: str) -> dict[str, Any]:
    """Decide whether this is an existing application or a description of an invention.

    Returns ``{kind, signals, evidence, confident}``. ``signals`` is the list a human reads, in
    the order they carry weight; ``evidence`` is the raw counts, so a test can assert on a number
    rather than on a sentence.
    """
    body = str(text or "")
    evidence = {
        "headings": len(set(match.group(0).strip().upper()
                            for match in _HEADING_RE.finditer(body))),
        "claims": len(_CLAIM_RE.findall(body)),
        "claim_preamble": 1 if _CLAIM_PREAMBLE_RE.search(body) else 0,
        "numerals": len(set(_NUMERAL_RE.findall(body))),
        "figures": len(_FIGURE_RE.findall(body)),
        "boilerplate": sum(1 for phrase in _BOILERPLATE if phrase in body.lower()),
        "chars": len(body),
    }

    signals: list[str] = []
    score = 0
    if evidence["claims"] >= 2:
        score += 2
        signals.append(f"{evidence['claims']} numbered claims")
    elif evidence["claims"] == 1:
        score += 1
        signals.append("a numbered claim")
    if evidence["claim_preamble"]:
        score += 1
        signals.append("a claim preamble")
    if evidence["headings"] >= 3:
        score += 2
        signals.append(f"{evidence['headings']} application headings")
    elif evidence["headings"] >= 1:
        score += 1
        signals.append(f"{evidence['headings']} application heading"
                       + ("s" if evidence["headings"] > 1 else ""))
    #  Reference numerals are the single most telling feature of a detailed description and the
    #  one thing nobody writes by accident, so the bar is a real scattering of them rather than
    #  the one or two a stray measurement can produce.
    if evidence["numerals"] >= 8:
        score += 2
        signals.append("reference numerals through the prose")
    elif evidence["numerals"] >= 3:
        score += 1
        signals.append("some reference numerals")
    if evidence["figures"] >= 2:
        score += 1
        signals.append(f"{evidence['figures']} figure references")
    if evidence["boilerplate"] >= 2:
        score += 1
        signals.append("drafting boilerplate")

    kind = "existing_draft" if score >= RECOGNISE_THRESHOLD else "description"
    return {
        "kind": kind,
        "score": score,
        "signals": signals,
        "evidence": evidence,
        #  Right at the line is worth saying. A page that prints "recognised as an application"
        #  with the same certainty at score 3 and at score 9 is overstating one of them.
        "confident": score >= RECOGNISE_THRESHOLD + 2 or score == 0,
    }


def describe(found: Mapping[str, Any]) -> str:
    """One sentence for the page: what it is, and what that means for the agent."""
    signals = list(found.get("signals") or [])
    if found.get("kind") == "existing_draft":
        head = "Read as a patent application you already have"
        tail = ("The agent will keep your text and subject matter and improve the drafting "
                "around it.")
    else:
        head = "Read as a description of an invention"
        tail = "The agent will write the application from it."
    if signals:
        return f"{head}: {', '.join(signals[:4])}. {tail}"
    return f"{head}. {tail}"


# =============================================================================================
# What a drafting project needs out of an extraction
# =============================================================================================
def material_from_extract(result: Mapping[str, Any]) -> dict[str, Any]:
    """Turn an ``ingest_input`` extraction into what a DRAFT starts from.

    The difference from what a search takes is the whole reason this exists: a search runs on the
    condensed brief, and a draft must start from the verbatim document. `full_text` is the
    applicant's own words; the brief is a model's summary of them, and an application drafted from
    a summary has no support for anything the summary dropped.
    """
    text = str(result.get("full_text") or "").strip()
    if not text:
        #  A link whose full text nobody could fetch still has an abstract and claims, and those
        #  are worth drafting from. Assembled in reading order so the result is a document rather
        #  than a bag of fields.
        parts = []
        if result.get("title"):
            parts.append(str(result["title"]).strip())
        if result.get("abstract"):
            parts.append("ABSTRACT\n" + str(result["abstract"]).strip())
        if result.get("summary_brief"):
            parts.append(str(result["summary_brief"]).strip())
        claims = [c for c in (result.get("claims") or []) if str(c.get("text") or "").strip()]
        if claims:
            parts.append("CLAIMS\n" + "\n\n".join(
                f"{c.get('claim_no') or n + 1}. {str(c['text']).strip()}"
                for n, c in enumerate(claims)))
        text = "\n\n".join(parts).strip()

    figures: list[bytes] = []
    for image in list(result.get("figure_images") or [])[:MAX_FIGURES]:
        blob = image.get("b64")
        if not blob:
            continue
        try:
            figures.append(base64.b64decode(blob))
        except Exception:                                      # noqa: BLE001 - skip a bad frame
            continue

    found = recognise(text)
    return {
        "title": str(result.get("title") or "").strip()[:240],
        "text": text[:MAX_TEXT_CHARS],
        "abstract": str(result.get("abstract") or "").strip(),
        "claims": [{"claim_no": c.get("claim_no"), "text": str(c.get("text") or "")}
                   for c in (result.get("claims") or [])],
        "publication_number": str(result.get("publication_number")
                                  or result.get("pub") or "").strip(),
        "source": str(result.get("source") or ""),
        "label": str(result.get("label") or ""),
        "figures": figures,
        "figure_descriptions": str(result.get("figure_descriptions")
                                   or result.get("vision") or "").strip(),
        "drawings_source": str(result.get("drawings_source") or ""),
        "notes": list(result.get("notes") or []),
        "verified": bool(result.get("verified", True)),
        "recognised": found,
    }


# =============================================================================================
# A publication we already hold is worth more than the link to it
# =============================================================================================
#  WHY THIS STAGE EXISTS. `ingest_input.extract_link` is built for a SEARCH, and a search runs on
#  the brief, so for a publication it returns the abstract and stops. Measured on US-9108319-B2,
#  which this corpus holds in full: 535 characters, no claims, no drawings. Drafting from that is
#  drafting from an abstract. The corpus has the claims, the description and the sheets, and
#  "start from an existing patent and move it to a neighbouring invention" is the whole use, so
#  the intake goes and gets them.
#
#  Upload is untouched: a PDF already arrives with its text and its drawings extracted.
MIN_LINK_CHARS = 2_000


def _document_from_passages(title: str, passages: Sequence[Mapping[str, Any]]) -> str:
    """The corpus's own passages, assembled in the order an application is written in.

    Not the order `full_text` returns them (abstract, claims, description), which is the order a
    reader charting claims wants. A drafting agent is handed this as the document to improve, so
    it has to READ as one.
    """
    abstract, claims, body = [], [], []
    for item in passages:
        kind = str(item.get("kind") or "")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if kind == "abstract":
            abstract.append(text)
        elif kind.startswith("claim"):
            label = str(item.get("label") or "").replace("claim ", "").strip()
            claims.append(f"{label}. {text}" if label and not text[:4].strip(". ").isdigit()
                          else text)
        else:
            body.append(text)
    parts = []
    if title:
        parts.append(title.upper())
    if abstract:
        parts.append("ABSTRACT\n\n" + "\n\n".join(abstract))
    if body:
        parts.append("DETAILED DESCRIPTION\n\n" + "\n\n".join(body))
    if claims:
        parts.append("WHAT IS CLAIMED IS:\n\n" + "\n\n".join(claims))
    return "\n\n".join(parts).strip()


def _dedupe(blobs: Sequence[bytes]) -> list[bytes]:
    """Same bytes, same sheet. The lead drawing is often on disk twice under two names, once as
    the card thumbnail the results list downloaded and once from the CDN set, and two identical
    FIG. 1s in a draft is a drawing objection nobody caused."""
    seen: set[str] = set()
    out: list[bytes] = []
    for blob in blobs:
        digest = hashlib.sha256(blob).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        out.append(blob)
    return out


def corpus_figures(publication: str, cap: int = MAX_FIGURES) -> list[bytes]:
    """The sheets we hold for a publication, downloading the remote ones once.

    Two stores, because the corpus has two kinds of figure: files already recovered onto disk, and
    Google-CDN URLs carried on a cached display record with nothing downloaded. A drawing has to
    become BYTES to be attached to a draft, so a remote one is fetched, and it is fetched into the
    shared figure directory so the search product's own cards get it for free next time.
    """
    blobs: list[bytes] = []
    try:
        import enrich_display
        import webview
    except Exception:                                          # noqa: BLE001 - no corpus here
        return blobs
    try:
        folder = enrich_display.FIGDIR / enrich_display._canonical_pubkey(publication)
    except Exception:                                          # noqa: BLE001 - odd number
        return blobs
    try:
        local = webview._cached_images(publication)
    except Exception:                                          # noqa: BLE001
        local = []
    try:
        remote = enrich_display.remote_thumbs(publication)
    except Exception:                                          # noqa: BLE001
        remote = []
    #  WHICHEVER STORE HAS MORE SHEETS, not whichever is cheaper. A card thumbnail downloads the
    #  LEAD drawing and nothing else, so a publication the results list has merely been scrolled
    #  past has exactly one file on disk. Preferring local because it is there took that one file
    #  and hid the other nine: measured on US-9108319-B2, 1 local against 10 on the CDN. A draft
    #  that is missing FIG. 2 through FIG. 10 has a detailed description referring to sheets
    #  nobody has.
    if local and len(local) >= len(remote):
        for item in local[:cap]:
            path = folder / str(item.get("file") or "")
            try:
                if path.exists():
                    blobs.append(path.read_bytes())
            except OSError:
                continue
        if blobs:
            return _dedupe(blobs)
    for index, item in enumerate(remote[:cap]):
        url = item.get("full") or item.get("thumbnail")
        if not url:
            continue
        target = folder / f"mongo-{index:02d}{enrich_display._fig_ext(url)}"
        try:
            if enrich_display._download(url, target) and target.exists():
                blobs.append(target.read_bytes())
        except Exception:                                      # noqa: BLE001 - one bad sheet
            continue
    return _dedupe(blobs)


def enrich_from_corpus(material: dict[str, Any]) -> dict[str, Any]:
    """Replace a thin link extraction with the document this corpus actually holds.

    Never destructive: it only fires when the publication is known AND what arrived is thinner
    than the corpus copy, so an upload, and a link that did come back whole, pass through
    untouched.
    """
    publication = str(material.get("publication_number") or "").strip()
    if not publication or material.get("source") == "upload":
        return material
    if len(material.get("text") or "") >= MIN_LINK_CHARS and material.get("figures"):
        return material
    try:
        import deep_analysis
    except Exception:                                          # noqa: BLE001
        return material

    def read():
        try:
            return deep_analysis.full_text(publication, max_chars=MAX_TEXT_CHARS) or {}
        except Exception:                                      # noqa: BLE001
            return {}

    body = read()
    thin = not (int(body.get("n_claims") or 0) or int(body.get("n_paragraphs") or 0))
    if thin:
        #  The same recovery the search path uses for a text-less reference: into the scratch
        #  store, which `full_text` reads back, rather than into the live corpus.
        try:
            import enrich
            if enrich.recovery_available():
                enrich.stash_full_text(publication, reason="drafting intake")
                body = read() or body
        except Exception:                                      # noqa: BLE001
            pass

    document = _document_from_passages(
        str(body.get("title") or material.get("title") or ""), body.get("passages") or [])
    if len(document) > len(material.get("text") or ""):
        material["text"] = document[:MAX_TEXT_CHARS]
        #  The claim COUNT on the page comes from here, and the link gave none. A panel saying
        #  "0 claims" beside a sentence saying "18 numbered claims" is the page disagreeing with
        #  itself about the document in front of it.
        material["claims"] = [
            {"claim_no": index + 1, "text": str(item.get("text") or "")}
            for index, item in enumerate(body.get("passages") or [])
            if str(item.get("kind") or "").startswith("claim")]
        material["notes"] = list(material.get("notes") or []) + [
            f"{body.get('n_claims') or 0} claim(s) and {body.get('n_paragraphs') or 0} "
            f"paragraph(s) taken from the corpus copy of {publication}"]
        material["recognised"] = recognise(material["text"])
    if not material.get("figures"):
        figures = corpus_figures(publication)
        if figures:
            material["figures"] = figures
            material["notes"] = list(material.get("notes") or []) + [
                f"{len(figures)} drawing(s) taken from the corpus copy of {publication}"]
            material["drawings_source"] = material.get("drawings_source") or "corpus"
    return material


# =============================================================================================
# Holding it between the upload and the submit
# =============================================================================================
def _dir(token: str) -> Path:
    return STASH / re.sub(r"[^0-9a-f]", "", str(token))[:64]


def stash(material: Mapping[str, Any]) -> str:
    """Keep an extraction until the form is submitted. -> the token.

    Figures are written as files rather than base64 in the JSON: a twenty-sheet grant is several
    megabytes of PNG, and the page wants to show them one at a time.
    """
    token = uuid.uuid4().hex
    folder = _dir(token)
    folder.mkdir(parents=True, exist_ok=True)
    figures = list(material.get("figures") or [])
    for index, blob in enumerate(figures):
        (folder / f"fig-{index:02d}.png").write_bytes(blob)
    record = {key: value for key, value in material.items() if key != "figures"}
    record["n_figures"] = len(figures)
    record["t"] = time.time()
    (folder / "material.json").write_text(json.dumps(record, ensure_ascii=False, default=str))
    sweep()
    return token


def load(token: str, *, with_figures: bool = True) -> dict[str, Any] | None:
    folder = _dir(token)
    path = folder / "material.json"
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return None
    if with_figures:
        record["figures"] = [
            (folder / f"fig-{index:02d}.png").read_bytes()
            for index in range(int(record.get("n_figures") or 0))
            if (folder / f"fig-{index:02d}.png").exists()]
    return record


def figure_path(token: str, index: int) -> Path | None:
    path = _dir(token) / f"fig-{int(index):02d}.png"
    return path if path.exists() else None


def sweep(now: float | None = None) -> int:
    """Drop extractions nobody submitted. A dropped PDF that never became a project is megabytes."""
    now = time.time() if now is None else now
    dropped = 0
    if not STASH.exists():
        return 0
    for folder in STASH.iterdir():
        try:
            if not folder.is_dir():
                continue
            record = folder / "material.json"
            age = now - (record.stat().st_mtime if record.exists() else folder.stat().st_mtime)
            if age > STASH_TTL_SECONDS:
                for item in folder.iterdir():
                    item.unlink()
                folder.rmdir()
                dropped += 1
        except OSError:
            continue
    return dropped


def public(material: Mapping[str, Any], token: str, figure_url) -> dict[str, Any]:
    """What the intake page is told about an extraction it has just made."""
    found = dict(material.get("recognised") or recognise(material.get("text") or ""))
    n_figures = int(material.get("n_figures") or len(material.get("figures") or []))
    return {
        "token": token,
        "title": material.get("title") or "",
        "text": material.get("text") or "",
        "chars": len(material.get("text") or ""),
        "publication_number": material.get("publication_number") or "",
        "label": material.get("label") or "",
        "source": material.get("source") or "",
        "n_claims": len([c for c in (material.get("claims") or []) if c.get("text")]),
        "n_figures": n_figures,
        "figures": [figure_url(token, index) for index in range(n_figures)],
        "drawings_source": material.get("drawings_source") or "",
        "verified": bool(material.get("verified", True)),
        "notes": list(material.get("notes") or [])[:8],
        "kind": found.get("kind"),
        "recognised": describe(found),
        "signals": found.get("signals") or [],
    }


def figure_labels(count: int) -> list[str]:
    """FIG. 1 upward. The drawings of an existing application are numbered, and keeping that
    numbering is what lets the agent's drawing descriptions line up with the sheets."""
    return [f"FIG. {index + 1}" for index in range(int(count))]
