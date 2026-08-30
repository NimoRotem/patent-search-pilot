"""Embedded faces for every PDF this service files.

WHY THIS EXISTS. reportlab's default faces are the PDF base-14: Times, Helvetica, Courier,
Symbol, ZapfDingbats. A viewer is expected to have them, so reportlab writes the name and not the
font. Patent Center's PDF guidelines require every glyph to be embedded and list an unembedded
font as a validation failure, so a packet built on the base-14 bounces at upload. Measured
2026-08-24 on the packet for adhoc-efbf2979420b: seventeen of twenty-one papers, every one of
them a paper that gets filed.

The second failure is worse, because it is silent. Asked for a glyph the current face does not
have, reportlab falls back to ZapfDingbats, and a name in a script the face does not cover prints
as solid black squares. CN 216190291 U's first named inventor, 徐勇, went onto the face of a
document list as ■■, which leaves the 1.290(e)(4) identification blank on a filed paper.

So: Liberation Serif and Liberation Sans for Latin, metric-compatible with Times and Helvetica and
under the SIL Open Font License, and Droid Sans Fallback for everything they do not cover. The
right answer for a name is still usually to identify the party some other way, which
`concise_render.printable_party` does. This is the layer underneath that, for the passages,
titles and quotations where there is no other way to say it.
"""
from __future__ import annotations

import os
import re

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

#  Metric-compatible substitutes, so switching to them does not reflow a page. Liberation Serif is
#  Times metrics, Liberation Sans is Helvetica/Arial metrics.
SERIF = "FilingSerif"
SERIF_BOLD = "FilingSerif-Bold"
SERIF_ITALIC = "FilingSerif-Italic"
SERIF_BOLDITALIC = "FilingSerif-BoldItalic"
SANS = "FilingSans"
SANS_BOLD = "FilingSans-Bold"
FALLBACK = "FilingFallback"

_LIB = "/usr/share/fonts/truetype/liberation2"
_LIB1 = "/usr/share/fonts/truetype/liberation"
_DEJA = "/usr/share/fonts/truetype/dejavu"

#  (name, [candidate paths, best first]). A missing file is not fatal on its own: the registration
#  falls through to the next candidate, and `missing()` reports what never resolved.
_FACES = [
    (SERIF, ["%s/LiberationSerif-Regular.ttf" % _LIB, "%s/LiberationSerif-Regular.ttf" % _LIB1,
             "%s/DejaVuSerif.ttf" % _DEJA]),
    (SERIF_BOLD, ["%s/LiberationSerif-Bold.ttf" % _LIB, "%s/LiberationSerif-Bold.ttf" % _LIB1,
                  "%s/DejaVuSerif-Bold.ttf" % _DEJA]),
    (SERIF_ITALIC, ["%s/LiberationSerif-Italic.ttf" % _LIB,
                    "%s/LiberationSerif-Italic.ttf" % _LIB1, "%s/DejaVuSerif.ttf" % _DEJA]),
    (SERIF_BOLDITALIC, ["%s/LiberationSerif-BoldItalic.ttf" % _LIB,
                        "%s/LiberationSerif-BoldItalic.ttf" % _LIB1,
                        "%s/DejaVuSerif-Bold.ttf" % _DEJA]),
    (SANS, ["%s/LiberationSans-Regular.ttf" % _LIB, "%s/LiberationSans-Regular.ttf" % _LIB1,
            "%s/DejaVuSans.ttf" % _DEJA]),
    (SANS_BOLD, ["%s/LiberationSans-Bold.ttf" % _LIB, "%s/LiberationSans-Bold.ttf" % _LIB1,
                 "%s/DejaVuSans-Bold.ttf" % _DEJA]),
    (FALLBACK, ["/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]),
]

_registered = {}
_ready = False


def _register():
    global _ready
    if _ready:
        return
    for name, paths in _FACES:
        for p in paths:
            if not os.path.exists(p):
                continue
            try:
                pdfmetrics.registerFont(TTFont(name, p))
            except Exception:                                             # noqa: BLE001
                continue
            _registered[name] = p
            break
    #  The family mapping is what makes <b> and <i> inside a Paragraph pick the right FILE rather
    #  than reportlab synthesising a slant over the regular face.
    if {SERIF, SERIF_BOLD, SERIF_ITALIC, SERIF_BOLDITALIC} <= set(_registered):
        pdfmetrics.registerFontFamily(SERIF, normal=SERIF, bold=SERIF_BOLD,
                                      italic=SERIF_ITALIC, boldItalic=SERIF_BOLDITALIC)
    if {SANS, SANS_BOLD} <= set(_registered):
        pdfmetrics.registerFontFamily(SANS, normal=SANS, bold=SANS_BOLD,
                                      italic=SANS, boldItalic=SANS_BOLD)
    #  THE CANVAS'S OWN BASE FONT. A reportlab canvas starts in Helvetica and writes that resource
    #  onto every page whether anything is drawn in it or not, so a document whose every style
    #  named an embedded face still shipped one unembedded font. This is the last one.
    if SERIF in _registered:
        from reportlab import rl_config
        rl_config.canvas_basefontname = SERIF
    _ready = True


def ready():
    """Register on first use and report which faces resolved. -> {name: path}"""
    _register()
    return dict(_registered)


def missing():
    """Faces that resolved to no file on this host. Empty is the only acceptable answer on a box
    that renders filings, so a health check and a test both ask."""
    _register()
    return [name for name, _paths in _FACES if name not in _registered]


def font(name, default=None):
    """The registered face, or the base-14 name if this host has no font files at all.

    Falling back to the base-14 keeps a development box rendering rather than crashing. It is a
    validation failure at Patent Center, which is why `missing()` exists and is asserted.
    """
    _register()
    if name in _registered:
        return name
    return default or {SERIF: "Times-Roman", SERIF_BOLD: "Times-Bold",
                       SERIF_ITALIC: "Times-Italic", SERIF_BOLDITALIC: "Times-BoldItalic",
                       SANS: "Helvetica", SANS_BOLD: "Helvetica-Bold",
                       FALLBACK: "Times-Roman"}[name]


#  The blocks a Liberation face can draw: Latin and its supplements and extended ranges, Greek,
#  Cyrillic, the general punctuation, currency, arrows, maths and the Latin ligatures. Named by
#  codepoint rather than typed as literal characters, because typing them put a control byte in
#  this file. Anything outside goes to the fallback face rather than to ZapfDingbats.
_COVERED = re.compile(
    "["
    "\u0020-\u024f"          # Latin, its supplement and both extended blocks
    "\u0250-\u02af"          # IPA extensions, used in transliterations
    "\u0300-\u036f"          # combining diacritics
    "\u0370-\u03ff"          # Greek
    "\u0400-\u04ff"          # Cyrillic
    "\u1e00-\u1eff"          # Latin extended additional, the Vietnamese range
    "\u2000-\u206f"          # general punctuation, including the quotes and the dashes
    "\u20a0-\u20bf"          # currency
    "\u2100-\u214f"          # letterlike symbols
    "\u2190-\u21ff\u2200-\u22ff"   # arrows and maths
    "\u25a0-\u25ff\u2600-\u26ff"   # geometric shapes and miscellaneous symbols
    "\ufb00-\ufb06"          # the Latin ligatures
    "\n\r\t"
    "]")


def covers_serif(text):
    """Is every character in `text` one the Latin face can draw?"""
    return all(_COVERED.match(ch) for ch in str(text or ""))


def with_fallback(escaped):
    """Wrap the runs a Latin face cannot draw in a <font> span pointing at the fallback face.

    Takes text that is ALREADY XML-escaped, because that is what every caller has by the time it
    reaches a Paragraph, and because inserting tags before escaping would escape the tags. Returns
    it unchanged when nothing needs the fallback, which is the overwhelming majority of the time.
    """
    s = str(escaped or "")
    if not s or covers_serif(s):
        return s
    _register()
    if FALLBACK not in _registered:
        return s                                    # nothing better to offer; `missing()` says so
    out, run = [], []

    def flush(cjk):
        if not run:
            return
        chunk = "".join(run)
        out.append('<font face="%s">%s</font>' % (FALLBACK, chunk) if cjk else chunk)
        run.clear()

    inside_tag = False
    prev_cjk = None
    for ch in s:
        #  Step over the entities and tags the caller already put in, so a `&amp;` is not split.
        if ch == "<":
            inside_tag = True
        if inside_tag:
            flush(prev_cjk)
            prev_cjk = None
            out.append(ch)
            if ch == ">":
                inside_tag = False
            continue
        cjk = not _COVERED.match(ch)
        if prev_cjk is not None and cjk != prev_cjk:
            flush(prev_cjk)
        prev_cjk = cjk
        run.append(ch)
    flush(prev_cjk)
    return "".join(out)
