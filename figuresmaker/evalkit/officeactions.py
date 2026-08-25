"""Mining real drawing objections, and checking the validator against them.

An examiner's objection is the only labelled failure this problem has. Everything else is a
guess about what matters; an objection is a record of what actually got a case held up. So this
module pulls office actions from the USPTO Open Data Portal, finds the paragraphs that object to
the drawings, and sorts them into the rules they invoke.

What that is for is coverage, not accuracy. The question it answers is: of the drawing objections
examiners really write, which kinds could this checker have caught? A class of objection that
appears in the corpus and has no check behind it is a gap, and it is named as one. That is a more
honest use of the data than scoring against it, because the specification that was objected to is
not the specification that was granted, and the drawings that were objected to are not in the
file as data.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

ODP_BASE = "https://api.uspto.gov/api/v1/patent/applications"
PILOT_ENV = Path(os.environ.get("PILOT_ENV_FILE",
                                os.path.expanduser("~/patent-search-pilot/.env")))
TIMEOUT = 60.0
REJECTION_CODES = ("CTNF", "CTFR", "CTRS", "CTMS", "EXIN", "NOA")


class ODPUnavailable(RuntimeError):
    """The Open Data Portal could not be reached, or has no key. Said, not worked around."""


def _key() -> str:
    value = (os.environ.get("USPTO_ODP_KEY") or "").strip()
    if value:
        return value
    try:
        for line in PILOT_ENV.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("USPTO_ODP_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    raise ODPUnavailable(
        f"no USPTO_ODP_KEY in the environment or in {PILOT_ENV}. The office action miner needs "
        "one; the rest of the evaluation does not.")


def _get(url: str, params: Optional[dict] = None) -> requests.Response:
    response = requests.get(url, params=params or {}, timeout=TIMEOUT, headers={
        "X-API-KEY": _key(), "Accept": "application/json",
        "User-Agent": "iptorch-figuresmaker/1.0"})
    if response.status_code == 403:
        raise ODPUnavailable("the Open Data Portal rejected the key (403)")
    response.raise_for_status()
    return response


_CANONICAL = re.compile(r"^([A-Z]{2})([A-Z]*)(\d+)([A-Z]\d?)?$")


def patent_digits(patent_number: str) -> str:
    """The grant number alone.

    Stripping every non-digit is wrong: "US11000000B2" becomes "110000002", because the kind
    code's own digit comes along. The Open Data Portal then answers 404 for a number that does
    not exist, which reads as "no file wrapper" rather than as a parsing mistake.
    """
    from fm import ingest

    canonical = (ingest.normalise_patent_number(patent_number) or patent_number).upper()
    match = _CANONICAL.match(canonical.replace(" ", ""))
    if match:
        return match.group(3)
    return re.sub(r"[^0-9]", "", patent_number)


def application_for(patent_number: str) -> Optional[str]:
    """The application number a granted patent issued from."""
    digits = patent_digits(patent_number)
    if not digits:
        return None
    try:
        payload = _get(f"{ODP_BASE}/search",
                       {"q": f"applicationMetaData.patentNumber:{digits}", "limit": 1}).json()
    except requests.RequestException as exc:
        raise ODPUnavailable(f"search failed for {patent_number}: {exc}") from exc
    items = payload.get("patentFileWrapperDataBag") or payload.get("results") or []
    if not items:
        return None
    return str(items[0].get("applicationNumberText") or "") or None


def documents(application: str) -> list[dict[str, Any]]:
    try:
        payload = _get(f"{ODP_BASE}/{application}/documents").json()
    except requests.RequestException as exc:
        raise ODPUnavailable(f"documents failed for {application}: {exc}") from exc
    return payload.get("documentBag") or []


def _download_url(document: dict[str, Any], kind: str) -> str:
    for option in document.get("downloadOptionBag") or []:
        if str(option.get("mimeTypeIdentifier", "")).upper() == kind:
            return str(option.get("downloadUrl") or "")
    return ""


def _fetch(url: str) -> bytes:
    try:
        response = requests.get(url, timeout=TIMEOUT, headers={"X-API-KEY": _key()})
        response.raise_for_status()
        return response.content
    except requests.RequestException:
        return b""


def _strip_tags(markup: str) -> str:
    text = re.sub(r"<[^>]+>", " ", markup)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#160;", " ").replace("&nbsp;", " "))
    return re.sub(r"[ \t\xa0]+", " ", text)


def document_text(document: dict[str, Any]) -> str:
    """The words of an office action.

    The PDF is useless here and it is the obvious thing to reach for: the Office's own copy is a
    scan, and pypdf gets six characters out of a seven-page rejection. The text lives in the other
    two download options, which the same response already offers: XML, which arrives as a tar of
    one document, and MS_WORD, which is a docx. Trying the PDF first and stopping there is how
    this returned "no drawing objections" for every application in the corpus.
    """
    archive = _fetch(_download_url(document, "XML"))
    if archive:
        body = _from_tar(archive)
        if body:
            return body
    docx = _fetch(_download_url(document, "MS_WORD"))
    if docx and docx[:2] == b"PK":
        try:
            import zipfile
            with zipfile.ZipFile(io.BytesIO(docx)) as bundle:
                return _strip_tags(bundle.read("word/document.xml").decode("utf-8", "replace"))
        except Exception:
            pass
    pdf = _fetch(_download_url(document, "PDF"))
    if pdf:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""
    return ""


def _from_tar(blob: bytes) -> str:
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(blob)) as bundle:
            best = ""
            for member in bundle.getmembers():
                if not member.isfile() or not member.name.lower().endswith((".xml", ".txt")):
                    continue
                handle = bundle.extractfile(member)
                if handle is None:
                    continue
                body = _strip_tags(handle.read().decode("utf-8", "replace"))
                if len(body) > len(best):
                    best = body
            return best
    except Exception:
        return ""


# ------------------------------------------------------------------------------ the language

_OBJECTION = re.compile(
    r"(?:^|\n)[^\n]{0,120}?"
    r"(?:drawings?\s+(?:are|is)\s+objected\s+to"
    r"|objected\s+to\s+under\s+37\s*C\.?F\.?R\.?\s*1\.8[34]"
    r"|corrected\s+drawing\s+sheets?"
    r"|new\s+correc\w+\s+drawings?"
    r"|drawings?\s+(?:submitted|filed)[^\n]{0,60}(?:not\s+accept|objected))"
    r"[\s\S]{0,900}", re.I)

# Each pattern is the wording examiners actually use, mapped to the check that would have caught
# it. A class with no check is the point of the exercise, so the mapping is deliberately narrow.
CLASSES: tuple[tuple[str, str, str], ...] = (
    ("missing_claimed_feature",
     r"must\s+show\s+every\s+feature|not\s+shown\s+in\s+the\s+drawings?|"
     r"fails?\s+to\s+(?:show|illustrate)|1\.83\(a\)",
     "claim_element_not_depicted"),
    # Examiners write "reference number" at least as often as "reference character", and
    # "not labeled" as often as "not shown". Matching only the wording of the regulation put
    # three of the first twenty-four objections in the unclassified pile, all of them instances
    # of a check that already exists.
    ("character_not_in_description",
     r"reference\s+(?:char\w+|numbers?|numerals?)[^\n]{0,90}"
     r"not\s+(?:mentioned|found|described|recited)|"
     r"not\s+mentioned\s+in\s+the\s+(?:description|specification)",
     "numeral_not_in_registry"),
    ("character_not_in_drawing",
     r"reference\s+(?:char\w+|numbers?|numerals?)[^\n]{0,90}"
     r"not\s+(?:shown|appear|labell?ed|illustrated|indicated|included)|"
     r"(?:mentioned|described|recited)\s+in\s+the\s+(?:description|specification)"
     r"[^\n]{0,70}not\s+(?:shown|appear|labell?ed)|"
     r"not\s+labell?ed\s+in\s+(?:FIG|Fig)|"
     r"should\s+be\s+reference\s+(?:number|numeral|character)",
     "registry_numeral_undrawn"),
    ("element_without_a_character",
     r"(?:elements?|parts?|features?)\s+without\s+reference\s+(?:numbers?|numerals?|char\w+)|"
     r"arrows?\s+pointing\s+to[^\n]{0,60}without\s+reference",
     "element_unnumbered"),
    ("character_reused",
     r"same\s+reference\s+char\w+[^\n]{0,90}different\s+parts?|"
     r"used\s+to\s+designate\s+different",
     "numeral_reused"),
    ("lead_lines",
     r"lead\s+lines?|1\.84\(q\)",
     "leaders_cross"),
    ("character_size_or_legibility",
     r"1\.84\(p\)|characters?\s+(?:are\s+)?(?:too\s+small|not\s+legible)|0\.32\s*cm|"
     r"1/8\s*inch",
     "numeral_too_small"),
    ("hatching_or_section",
     r"1\.84\(h\)|hatch\w+|sectional\s+view[^\n]{0,60}(?:objected|not)",
     "section_without_hatching"),
    ("line_quality",
     r"1\.84\(l\)|lines?[^\n]{0,60}(?:not\s+)?(?:uniformly\s+thick|sufficiently\s+dense|"
     r"clean|durable)|poor\s+(?:line\s+)?quality|photograph",
     "line_too_thin"),
    ("margins_or_sheet",
     r"1\.84\(f\)|1\.84\(g\)|margins?|sheet\s+size|sight",
     "outside_margins"),
    ("view_numbering",
     r"1\.84\(u\)|views?\s+(?:are\s+)?(?:not\s+)?numbered|FIG\w*\.?\s*\d+[^\n]{0,40}"
     r"(?:missing|out\s+of\s+order)",
     "figures_not_sequential"),
    ("brief_description",
     r"brief\s+description\s+of\s+the\s+drawings?",
     "brief_description_mismatch"),
    ("shading_or_colour",
     r"1\.84\(m\)|shading|solid\s+black|colou?r\s+drawings?",
     "shading_present"),
    ("legends_or_text",
     r"1\.84\(o\)|legends?|descriptive\s+matter",
     "legend_used"),
)


@dataclass
class Objection:
    patent: str = ""
    application: str = ""
    document: str = ""
    code: str = ""
    date: str = ""
    passage: str = ""
    classes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify(passage: str) -> list[str]:
    out: list[str] = []
    for name, pattern, _check in CLASSES:
        if re.search(pattern, passage, re.I):
            out.append(name)
    return out


def find_objections(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _OBJECTION.finditer(text or ""):
        passage = re.sub(r"\s+", " ", match.group(0)).strip()
        if len(passage) < 40:
            continue
        key = passage[:160]
        if key in seen:
            continue
        seen.add(key)
        out.append(passage[:900])
    return out


def sample_applications(query: str, limit: int, *, page: int = 100) -> list[str]:
    """Application numbers matching an Open Data Portal query.

    Used to widen the sample. A corpus chosen for having a good brief description of the drawings
    is a corpus of well-prepared applications, and those are exactly the ones an examiner does
    not object to, so it says nothing about which objections this checker covers.
    """
    import urllib.parse

    out: list[str] = []
    offset = 0
    while len(out) < limit:
        url = (f"{ODP_BASE}/search?q={urllib.parse.quote(query)}"
               f"&limit={min(page, limit - len(out))}&offset={offset}")
        try:
            payload = _get(url).json()
        except requests.RequestException as exc:
            raise ODPUnavailable(f"search failed: {exc}") from exc
        bag = payload.get("patentFileWrapperDataBag") or []
        if not bag:
            break
        for item in bag:
            number = str(item.get("applicationNumberText") or "")
            if number and number.isdigit() and number not in out:
                out.append(number)
        offset += len(bag)
    return out[:limit]


def mine_application(application: str, *, patent: str = "", pause: float = 0.3,
                     max_actions: int = 4) -> tuple[list[Objection], int, int]:
    """Objections in one file wrapper, with how many actions were read and how many had text."""
    try:
        docs = documents(application)
    except ODPUnavailable:
        return ([], 0, 0)
    actions = [d for d in docs if str(d.get("documentCode", "")).upper() in REJECTION_CODES]
    found: list[Objection] = []
    with_text = 0
    for document in actions[:max_actions]:
        text = document_text(document)
        if not text or len(text) < 500:
            continue
        with_text += 1
        for passage in find_objections(text):
            found.append(Objection(
                patent=patent, application=application,
                document=str(document.get("documentIdentifier") or ""),
                code=str(document.get("documentCode") or ""),
                date=str(document.get("officialDate") or "")[:10],
                passage=passage, classes=classify(passage)))
        time.sleep(pause)
    return (found, len(actions), with_text)


def mine(patent_numbers: Iterable[str], out_dir: Path, *, pause: float = 0.3,
         verbose: bool = True) -> list[Objection]:
    out_dir.mkdir(parents=True, exist_ok=True)
    found: list[Objection] = []
    for number in patent_numbers:
        try:
            application = application_for(number)
        except ODPUnavailable as exc:
            if verbose:
                print(f"  {number:16s} {exc}")
            break
        if not application:
            if verbose:
                print(f"  {number:16s} no application found")
            continue
        hits, actions, with_text = mine_application(application, patent=number, pause=pause)
        found.extend(hits)
        if verbose:
            print(f"  {number:16s} app {application}  {actions} action(s), {with_text} with "
                  f"text  {len(hits)} drawing objection(s)")
    _write(out_dir, found)
    return found


def mine_sample(query: str, count: int, out_dir: Path, *, pause: float = 0.3,
                verbose: bool = True) -> list[Objection]:
    out_dir.mkdir(parents=True, exist_ok=True)
    applications = sample_applications(query, count)
    if verbose:
        print(f"  {len(applications)} application(s) matched")
    found: list[Objection] = []
    read = 0
    empty = 0
    for index, application in enumerate(applications, start=1):
        hits, actions, with_text = mine_application(application, pause=pause)
        read += with_text
        if actions and not with_text:
            empty += 1
        found.extend(hits)
        if verbose and (hits or index % 10 == 0):
            print(f"  [{index}/{len(applications)}] {application}  {actions} action(s), "
                  f"{with_text} with text, {len(hits)} objection(s), {len(found)} so far",
                  flush=True)
    if verbose:
        print(f"  read {read} action(s) with text; {empty} application(s) had actions but no "
              "readable text")
    _write(out_dir, found)
    return found


def _write(out_dir: Path, found: list[Objection]) -> None:
    (out_dir / "objections.json").write_text(
        json.dumps([o.as_dict() for o in found], indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------------- coverage


def coverage(objections: Iterable[Objection]) -> dict[str, Any]:
    """Which kinds of objection this checker could have caught, and which it could not."""
    from fm.validate import rules

    counts: dict[str, int] = {}
    unclassified = 0
    for objection in objections:
        # Classified afresh from the passage rather than trusting what was stored, so widening a
        # pattern re-scores the whole corpus without re-downloading it.
        classes = classify(objection.passage)
        if not classes:
            unclassified += 1
            continue
        for name in classes:
            counts[name] = counts.get(name, 0) + 1

    covered: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    for name, _pattern, check in CLASSES:
        seen = counts.get(name, 0)
        has_check = check in rules.RULES
        covered[name] = {"objections": seen, "check": check, "implemented": has_check,
                         "cite": rules.RULES[check].cite if has_check else ""}
        if seen and not has_check:
            gaps.append(name)
    return {
        "objections": sum(counts.values()),
        "unclassified": unclassified,
        "classes": covered,
        "gaps": gaps,
        "covered_fraction": round(
            sum(v["objections"] for v in covered.values() if v["implemented"])
            / max(1, sum(counts.values())), 3),
    }


def load(path: Path) -> list[Objection]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Objection(**item) for item in raw]


def main(argv: list[str]) -> int:
    import argparse

    from . import corpus

    parser = argparse.ArgumentParser(description="mine drawing objections from office actions")
    parser.add_argument("--dir", default="~/figuresmaker-eval/objections")
    parser.add_argument("--numbers", nargs="*", default=None)
    parser.add_argument("--sample", type=int, default=0,
                        help="mine this many applications matching --query instead")
    parser.add_argument("--query", default="applicationMetaData.filingDate:[2017-01-01 TO "
                                           "2019-12-31]",
                        help="an Open Data Portal search expression")
    parser.add_argument("--reclassify", action="store_true",
                        help="re-score the objections already on disk, downloading nothing")
    args = parser.parse_args(argv)

    out_dir = Path(args.dir).expanduser()
    if args.reclassify:
        found = load(out_dir / "objections.json")
        print(f"re-scoring {len(found)} objection(s) already on disk")
        return _report(found, out_dir)

    try:
        if args.sample:
            print(f"sampling {args.sample} application(s) for {args.query}")
            found = mine_sample(args.query, args.sample, out_dir)
        else:
            numbers = args.numbers or list(corpus.DEFAULT_NUMBERS)
            print(f"mining {len(numbers)} application file(s)")
            found = mine(numbers, out_dir)
    except ODPUnavailable as exc:
        print(f"\n{exc}")
        return 3
    return _report(found, out_dir)


def _report(found: list[Objection], out_dir: Path) -> int:
    report = coverage(found)
    print(f"\n{report['objections']} classified objection(s), "
          f"{report['unclassified']} unclassified")
    for name, item in sorted(report["classes"].items(), key=lambda kv: -kv[1]["objections"]):
        mark = "yes" if item["implemented"] else "NO CHECK"
        print(f"  {item['objections']:4d}  {name:32s} {mark:9s} {item['check']}")
    if report["gaps"]:
        print("\ngaps, objections this checker would not have caught:")
        for name in report["gaps"]:
            print(f"  {name}")
    (out_dir / "coverage.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nwritten to {out_dir}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
