"""Will Patent Center accept this PDF? Asked of every paper, before the packet is called ready.

WHAT WENT WRONG WITHOUT IT. Two mechanical defects would have bounced a real 1.290 packet at
upload, and neither is visible by looking at the pages:

  * Seventeen of twenty-one papers carried unembedded base-14 fonts. reportlab writes the NAME of
    Times or Helvetica and not the font, a viewer is expected to have them, and Patent Center lists
    an unembedded font as a validation failure. `pdf_fonts` fixed the generator; this checks the
    ARTEFACT, because a style that names an embedded face and a table that quietly emits its own
    default are indistinguishable until somebody opens the file and looks at the font resources.
  * A Chinese inventor's name printed as two solid black squares. Asked for a glyph the current
    face does not have, reportlab substitutes ZapfDingbats silently, and "n" in ZapfDingbats is a
    filled square. That left a 1.290(e)(4) identification blank on the face of a filed paper.

So the gate is on the OUTPUT and not on the intention. Patent Center's stated requirements, read
2026-08-26: every font embedded, PDF version 1.1 to 1.6, US Letter or A4, no encryption, no
optional content groups (layers), no embedded file attachments.

NOTHING HERE REWRITES A FILE. A guard that silently repairs its input turns a true line false: the
audit says which paper failed which requirement, and the packet is not described as ready to file
until they pass. A copy fetched from a foreign office is reported separately, because that one is
the practitioner's to convert and not this generator's to fix.
"""
from __future__ import annotations

import io
import re
import traceback

#  Patent Center accepts 1.1 through 1.6. Written as a pair rather than a set so a future 1.7
#  allowance is one number.
MIN_VERSION, MAX_VERSION = (1, 1), (1, 6)

#  US Letter and A4 in points, either orientation, with the tolerance a millimetre of rounding
#  needs. A page that is neither is a validation failure whatever is on it.
LETTER = (612.0, 792.0)
A4 = (595.28, 841.89)
SIZE_TOLERANCE = 3.0


def _version(reader):
    header = str(getattr(reader, "pdf_header", "") or "")
    m = re.search(r"(\d+)\.(\d+)", header)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _page_size_ok(w, h):
    for pw, ph in (LETTER, A4):
        for a, b in ((pw, ph), (ph, pw)):
            if abs(w - a) <= SIZE_TOLERANCE and abs(h - b) <= SIZE_TOLERANCE:
                return True
    return False


_FONT_FILE_KEYS = ("/FontFile", "/FontFile2", "/FontFile3")


def _fonts_of(obj, out, seen):
    """Every font resource reachable from a page, with whether its program is embedded.

    A Type0 font carries no descriptor of its own: the program hangs off its descendant, which is
    where a CID face's embedding actually lives. Missing that reads a correctly embedded CJK font
    as an unembedded one.
    """
    try:
        res = obj.get("/Resources")
        res = res.get_object() if hasattr(res, "get_object") else res
        fonts = (res or {}).get("/Font")
        fonts = fonts.get_object() if hasattr(fonts, "get_object") else fonts
    except Exception:                                                     # noqa: BLE001
        return
    for _key, ref in (fonts or {}).items():
        try:
            f = ref.get_object() if hasattr(ref, "get_object") else ref
            name = str(f.get("/BaseFont") or "(unnamed)")
            if name in seen:
                continue
            seen.add(name)
            desc = f.get("/FontDescriptor")
            if desc is None:
                for d in (f.get("/DescendantFonts") or []):
                    d = d.get_object() if hasattr(d, "get_object") else d
                    desc = d.get("/FontDescriptor")
                    if desc is not None:
                        break
            desc = desc.get_object() if hasattr(desc, "get_object") else desc
            embedded = bool(desc) and any(k in desc for k in _FONT_FILE_KEYS)
            out.append({"name": name, "embedded": embedded,
                        "subtype": str(f.get("/Subtype") or "")})
        except Exception:                                                 # noqa: BLE001
            continue


def check(blob):
    """-> {"ok", "problems": [str], ...}. Never raises; an unreadable file is its own problem."""
    out = {"ok": False, "problems": [], "version": "", "pages": 0, "fonts": [],
           "unembedded": [], "encrypted": False, "layers": False, "attachments": False,
           "bad_pages": []}
    if not blob:
        out["problems"].append("the file is empty")
        return out
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(blob))
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        out["problems"].append("the file could not be opened as a PDF")
        return out
    try:
        if getattr(reader, "is_encrypted", False):
            out["encrypted"] = True
            out["problems"].append("it is encrypted, which Patent Center refuses")
            return out
        ver = _version(reader)
        out["version"] = "%d.%d" % ver if ver else ""
        if ver and not (MIN_VERSION <= ver <= MAX_VERSION):
            out["problems"].append(
                "it declares PDF %s and Patent Center accepts %d.%d to %d.%d"
                % (out["version"], MIN_VERSION[0], MIN_VERSION[1], MAX_VERSION[0], MAX_VERSION[1]))
        root = reader.trailer["/Root"]
        root = root.get_object() if hasattr(root, "get_object") else root
        if "/OCProperties" in root:
            out["layers"] = True
            out["problems"].append("it carries optional content groups (layers)")
        names = root.get("/Names")
        names = names.get_object() if hasattr(names, "get_object") else names
        if names and "/EmbeddedFiles" in names:
            out["attachments"] = True
            out["problems"].append("it carries embedded file attachments")
        seen = set()
        out["pages"] = len(reader.pages)
        for i, page in enumerate(reader.pages, 1):
            _fonts_of(page, out["fonts"], seen)
            try:
                box = page.mediabox
                w, h = float(box.width), float(box.height)
            except Exception:                                             # noqa: BLE001
                continue
            if not _page_size_ok(w, h):
                out["bad_pages"].append("page %d is %.0f x %.0f points" % (i, w, h))
        out["unembedded"] = sorted({f["name"] for f in out["fonts"] if not f["embedded"]})
        if out["unembedded"]:
            out["problems"].append("%d font%s not embedded: %s"
                                   % (len(out["unembedded"]),
                                      "" if len(out["unembedded"]) == 1 else "s",
                                      ", ".join(out["unembedded"][:6])))
        if out["bad_pages"]:
            out["problems"].append("%d page%s is neither US Letter nor A4: %s"
                                   % (len(out["bad_pages"]),
                                      "" if len(out["bad_pages"]) == 1 else "s",
                                      "; ".join(out["bad_pages"][:4])))
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        out["problems"].append("the file could not be inspected to the end")
    out["ok"] = not out["problems"]
    return out


def check_paths(paths):
    """-> {name: result} for every path that exists. A missing path is not an answer, so it is
    simply absent from the mapping rather than reported as a conforming file."""
    out = {}
    for p in paths or []:
        try:
            if not p.exists():
                continue
            out[p.name] = check(p.read_bytes())
        except Exception:                                                 # noqa: BLE001
            traceback.print_exc()
    return out


#  Papers this service generates, which a defect here is OURS to fix, against copies fetched from
#  an office, which are the practitioner's to convert. The distinction changes what the audit line
#  asks somebody to do, so it is drawn on the filename rather than guessed at.
_FETCHED = ("40_Copy_",)


def is_generated(name):
    return not str(name or "").startswith(_FETCHED)
