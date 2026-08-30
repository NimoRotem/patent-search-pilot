"""Make a PDF we did not write acceptable to Patent Center, without changing what it says.

WHY THIS EXISTS. `pdf_conform` checks the papers this system GENERATES, and they pass, because we
control how they are drawn. The other half of a 1.290 packet is not ours: 1.290(d)(3) requires a
legible copy of every non-US item, and those copies are pulled from Espacenet, the DPMA, the JPO
and Google, each of which produces whatever its own pipeline produces. Counsel's upload of the
tested packet bounced on four of them, on unembedded fonts, optional-content layers, embedded
attachments and a PDF version outside the accepted range.

Checking them was never going to be enough. A validation failure on somebody else's file is not
something the practitioner can fix either: the answer to "Espacenet gave you a PDF 1.7 with layers"
is not to go and ask Espacenet. So the file is rewritten.

WHAT REWRITING IS ALLOWED TO MEAN, because this is evidence going to an examiner:

  * The page images and the text must survive byte-for-byte in what they SAY. Ghostscript's
    pdfwrite device re-emits the page content stream, which is a faithful re-rendering, and this
    module then PROVES it: the extracted text of the output is compared against the input and a
    result that lost text is thrown away and the original kept. A copy that has quietly lost the
    passage it is cited for is far worse than one Patent Center refuses.
  * Nothing may be added. No stamps, no headers, no watermarks.
  * A failure is never fatal. Every path returns the original bytes, because a copy that might not
    upload beats no copy at all, and the audit already reports what did not conform.

WHAT IT FIXES, and each is one of the four that bounced:

    unembedded fonts     pdfwrite embeds every face it draws with, which is the whole reason this
                         is ghostscript and not qpdf: qpdf is a structural tool and will happily
                         copy an unembedded font reference through.
    optional content     layers are flattened to their default-visible state by the re-render.
    attachments          embedded files do not survive pdfwrite, and qpdf strips what is left.
    version              -dCompatibilityLevel pins it inside 1.1 to 1.6.
    encryption           qpdf --decrypt first, because pdfwrite refuses an encrypted input.

Linearising last (qpdf --linearize) is not required by the rule and is done because a linearised
file opens a page at a time in the examiner's viewer instead of after the whole download.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import traceback

#  Patent Center accepts 1.1 to 1.6. 1.5 rather than 1.6 because object streams arrived at 1.5 and
#  every viewer in the chain has had twenty years with them.
TARGET_VERSION = os.environ.get("PDF_NORMALISE_VERSION", "1.5")
TIMEOUT = float(os.environ.get("PDF_NORMALISE_TIMEOUT", "120"))
#  How much of the input's extracted text the output must still carry. Not 1.0: pdfwrite can
#  legitimately change spacing and drop a soft hyphen, and a copy is compared on what it SAYS.
MIN_TEXT_KEPT = float(os.environ.get("PDF_NORMALISE_MIN_TEXT", "0.98"))

GS = shutil.which("gs")
QPDF = shutil.which("qpdf")
#  Our own `cidfmap`, prepended to ghostscript's resource path with -I. The offices REFERENCE the
#  standard Adobe-Japan1 and Adobe-GB1 faces rather than embedding them, so without a substitution
#  ghostscript draws nothing for them, the text check sees the document vanish, and the rewrite is
#  correctly reverted. Measured on JP 2019-155534 A: 4% of the text survived without this.
CIDMAP_DIR = os.environ.get(
    "PDF_CIDMAP_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "gs"))


def available() -> bool:
    """Can anything be normalised on this box at all?"""
    return bool(GS)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, timeout=TIMEOUT, **kw)


def _text_of(path) -> str:
    """The extractable text, squashed. Used only to compare input against output."""
    try:
        r = _run(["pdftotext", "-q", path, "-"])
        return " ".join((r.stdout or b"").decode("utf-8", "replace").split())
    except Exception:                                                     # noqa: BLE001
        return ""


def _kept(before: str, after: str) -> float:
    """How much of `before` survived into `after`, by content characters. 1.0 when there was none.

    Deliberately crude and one-directional: the question is only "did the rewrite lose the
    document", never "is it prettier". A drawings-only copy has no text either way and scores 1.0,
    which is right: there is nothing there to lose.
    """
    if not before:
        return 1.0
    if not after:
        return 0.0
    return min(1.0, len(after) / float(len(before)))


#  Catalog entries Patent Center rejects and ghostscript does not always drop. pdfwrite flattens
#  the DRAWING of an optional-content group but keeps the /OCProperties dictionary that declares
#  it, and the validator reads the declaration, not the ink. Measured on KR 10-2055130 B1, which
#  came back from the re-emit still carrying layers.
_CATALOG_STRIP = ("/OCProperties", "/Names", "/EmbeddedFiles", "/AcroForm")


def _strip_catalog_keys(path) -> bool:
    """Remove the catalog entries that fail validation. -> True if the file was rewritten.

    Structural only: this deletes declarations, never page content. An /OCProperties dictionary
    with every group already flattened into the page describes layers that are not there any more,
    and /EmbeddedFiles on an office copy is a stray attachment nobody is filing on purpose.
    """
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        root = reader.trailer["/Root"]
        hit = [k for k in _CATALOG_STRIP if k in root]
        if not hit:
            return False
        writer = pypdf.PdfWriter()
        writer.append_pages_from_reader(reader)
        for k in _CATALOG_STRIP:
            try:
                del writer._root_object[k]
            except Exception:                                             # noqa: BLE001, PERF203
                pass
        with open(path, "wb") as fh:
            writer.write(fh)
        return True
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return False


def normalise(blob: bytes, why: str = "") -> tuple:
    """-> (bytes, note). The bytes are the original unless a rewrite provably preserved the text.

    `note` is one line for the audit, empty when nothing was done.
    """
    if not blob or not GS:
        return blob, ""
    tmp = tempfile.mkdtemp(prefix="pdfnorm-")
    src = os.path.join(tmp, "in.pdf")
    dec = os.path.join(tmp, "dec.pdf")
    mid = os.path.join(tmp, "mid.pdf")
    dst = os.path.join(tmp, "out.pdf")
    try:
        with open(src, "wb") as fh:
            fh.write(blob)
        before = _text_of(src)

        #  1. DECRYPT. pdfwrite refuses an encrypted input outright, and an office copy carrying
        #     owner-password permissions is common and harmless to remove: it is a printing
        #     restriction on a document the office publishes.
        stage_in = src
        if QPDF:
            r = _run([QPDF, "--decrypt", "--object-streams=disable", src, dec])
            if r.returncode in (0, 3) and os.path.exists(dec) and os.path.getsize(dec) > 0:
                stage_in = dec                       # 3 is qpdf's "warnings, output written"

        #  2. RE-EMIT. This is the step that embeds the fonts, flattens the layers and drops the
        #     attachments, because pdfwrite writes a new file from the page content it draws.
        gs_pre = [GS, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER"]
        if os.path.isfile(os.path.join(CIDMAP_DIR, "cidfmap")):
            #  -I puts our directory FIRST on the resource path, so this cidfmap is found before
            #  the distribution's empty one. -sFONTPATH lets it resolve the faces by name too.
            gs_pre += ["-I" + CIDMAP_DIR, "-sFONTPATH=/usr/share/fonts"]
        r = _run(gs_pre + ["-sDEVICE=pdfwrite",
                  "-dCompatibilityLevel=%s" % TARGET_VERSION,
                  "-dPDFSETTINGS=/prepress",
                  "-dEmbedAllFonts=true", "-dSubsetFonts=true",
                  "-dAutoRotatePages=/None",
                  "-dDetectDuplicateImages=true",
                            "-sOutputFile=%s" % mid, stage_in])
        if r.returncode != 0 or not os.path.exists(mid) or os.path.getsize(mid) == 0:
            return blob, ""

        #  3. TIDY. Linearise for page-at-a-time opening, and drop anything qpdf still sees.
        out_path = mid
        if QPDF:
            r = _run([QPDF, "--linearize", "--remove-unreferenced-resources=yes", mid, dst])
            if r.returncode in (0, 3) and os.path.exists(dst) and os.path.getsize(dst) > 0:
                out_path = dst

        #  3b. AND THE DECLARATIONS THE RE-EMIT KEPT. See `_strip_catalog_keys`.
        _strip_catalog_keys(out_path)

        #  4. PROVE IT DID NOT LOSE THE DOCUMENT. This is the check that makes rewriting somebody
        #     else's evidence defensible at all, and it is why a failure here keeps the original.
        after = _text_of(out_path)
        kept = _kept(before, after)
        if kept < MIN_TEXT_KEPT:
            print("[pdf_normalise] REVERTED %s: the rewrite kept only %.1f%% of the text, so the "
                  "original is filed unchanged" % (why or "a copy", kept * 100), flush=True)
            return blob, ("not normalised: rewriting it lost text, so the office's own file is "
                          "attached as it came")
        with open(out_path, "rb") as fh:
            out = fh.read()
        return out, ("normalised for Patent Center: fonts embedded, layers flattened, "
                     "attachments and encryption removed, written as PDF %s" % TARGET_VERSION)
    except subprocess.TimeoutExpired:
        return blob, ""
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return blob, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
