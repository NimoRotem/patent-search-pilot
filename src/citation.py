"""One grammar for a pinpoint citation, used by whatever writes one and by whatever checks one.

WHY IT IS ITS OWN MODULE. Counsel found three citations in one packet that do not resolve, and all
three are the same defect in different clothes:

  * Document 6's concise description cited every quotation to "Abstract". The copy filed for it is
    six pages of drawing sheets with no abstract on them at all.
  * A Schunk paragraph was cited as (p0047, p0053). Those paragraph numbers match neither the A1
    nor the B4 of that application; in the A1 the sentence is at [0068].
  * US 2022/0045594 A1 was quoted as saying a projection width falls "within approximately +/- 25%
    the thickness of the ferromagnetic workpiece". That sentence is not in the document. [0061]
    says the projection and gap "should approximately match the thickness", qualitatively, with no
    number in it.

The existence check verified that the TEXT appeared somewhere. It did not verify the LOCATION, and
it did not verify against the publication that would actually be filed. A citation is a triple:

    (publication INCLUDING its kind code, location, text)

and none of the three means anything without the other two. A1 and B4 of one German application
have 96 and 99 paragraphs, fourteen B4 paragraphs have no twin in the A1, the offsets between
matching text run -5, 0, +1, +2, +3, and the claim counts are 15 and 14. So a citation may never be
carried across kind codes, and a location may never be rendered from a passage that came out of a
different publication.

THE RULE WHEN A LOCATION CANNOT BE RESOLVED: emit the quotation with no pinpoint. An unpinpointed
quotation is a small inconvenience to an examiner. An invented one is a false statement in a paper
filed in a live examination.
"""
from __future__ import annotations

import json
import re

ABSTRACT, CLAIM, PARA, FIGURE, OTHER = "abstract", "claim", "para", "figure", "other"


def _coord_of(cell_or_passage):
    """The coordinate dict, however it was stored. Never raises."""
    coord = (cell_or_passage or {}).get("coord")
    if isinstance(coord, str):
        try:
            coord = json.loads(coord.replace("'", '"'))
        except Exception:                                                 # noqa: BLE001
            coord = {}
    return coord if isinstance(coord, dict) else {}


def _digits(v):
    m = re.search(r"(\d+)", str(v or ""))
    return str(int(m.group(1))) if m else ""


def key(kind="", coord=None, text=""):
    """A canonical (what, which) for one location, from a coordinate, a label or a rendered cite.

    -> ("claim", "7") / ("para", "47") / ("abstract", "") / ("figure", "3") / ("other", <text>)

    The point of the canonical form is that "Paragraph [0047]", "paragraph p0047", "para 47" and
    a coord of {"para_no": "p0047"} are ONE place, and that "Abstract" and "claim 7" are not.
    """
    coord = coord if isinstance(coord, dict) else {}
    k = str(kind or "").strip().lower()
    if coord:
        if coord.get("claim_no") is not None:
            return (CLAIM, _digits(coord["claim_no"]))
        for f in ("para_no", "paragraph", "para"):
            if coord.get(f) is not None:
                return (PARA, _digits(coord[f]))
        for f in ("fig_no", "figure", "figure_no"):
            if coord.get(f) is not None:
                return (FIGURE, _digits(coord[f]))
    blob = " ".join(x for x in (k, str(text or "")) if x).strip().lower()
    if not blob:
        return (OTHER, "")
    if blob.startswith("abstract"):
        return (ABSTRACT, "")
    m = re.match(r"^claims?\s*\[?\s*(\d+)", blob)
    if m:
        return (CLAIM, str(int(m.group(1))))
    #  The corpus labels a paragraph chunk "paragraph p0047" and its coord holds "p0047", so the
    #  number may carry its own `p` after the word. Missing that read the label as an unresolvable
    #  place and quietly took the pinpoint off every paragraph citation in the packet.
    m = re.match(r"^(?:para(?:graph)?s?|p)\s*\[?\s*p?0*(\d+)", blob)
    if m:
        return (PARA, str(int(m.group(1))))
    m = re.match(r"^(?:fig(?:ure)?s?)\.?\s*(\d+)", blob)
    if m:
        return (FIGURE, str(int(m.group(1))))
    #  A description chunk with no number at all is a place we cannot pinpoint. Deliberately NOT
    #  folded into "abstract" or "paragraph 1": an unresolvable location is its own answer.
    return (OTHER, blob[:60])


def render(k):
    """A canonical key -> the string a practitioner reads on a filed paper, or "".

    Paragraph numbers are zero-padded to four digits because that is how a US pre-grant
    publication prints them and how an examiner searches for one.
    """
    what, which = k
    if what == CLAIM and which:
        return "Claim %s" % which
    if what == PARA and which:
        return "Paragraph [%s]" % which.zfill(4)
    if what == FIGURE and which:
        return "FIG. %s" % which
    if what == ABSTRACT:
        return "Abstract"
    return ""


def of_passage(passage):
    """The canonical key of one `deep_analysis.full_text` passage."""
    return key(passage.get("kind"), _coord_of(passage), passage.get("label"))


def of_cell(cell):
    """The canonical key a reading cell claims for its quotation."""
    coord = _coord_of(cell)
    return key(cell.get("kind") or "", coord, cell.get("location") or "")


def of_text(cite):
    """The canonical key of an already-rendered citation string such as "Paragraph [0047]"."""
    return key("", {}, cite)


def resolves(k):
    """Is this a place an examiner can turn to? "" and an unnumbered chunk are not."""
    what, which = k
    return bool((what in (CLAIM, PARA, FIGURE) and which) or what == ABSTRACT)


def same_place(a, b):
    return resolves(a) and a == b


#  A publication is identified by its number AND its kind code, and the two are one string here so
#  no caller can compare half of it. US-11413727-B2 and US-2022012345-A1 are different documents;
#  DE-102019131000-A1 and DE-102019131000-B4 are different documents too, with different paragraph
#  numbering and a different claim count, which is the case that produced a citation that does not
#  resolve.
def pub_key(pub):
    return re.sub(r"[^A-Z0-9]", "", str(pub or "").upper())


def same_publication(a, b):
    """True only when the kind codes agree as well as the numbers.

    A bare number with no kind code matches a kinded one of the same number, because the corpus
    stores some rows without it and refusing there would drop every citation on those documents.
    Two DIFFERENT kind codes never match: that is the whole point.
    """
    ka, kb = pub_key(a), pub_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    ma, mb = re.match(r"^([A-Z]{2}\d+)([A-Z]\d?)?$", ka), re.match(r"^([A-Z]{2}\d+)([A-Z]\d?)?$", kb)
    if not (ma and mb) or ma.group(1) != mb.group(1):
        return False
    #  Same number, and at least one side did not say which publication of it. Not a mismatch.
    return not (ma.group(2) and mb.group(2))
