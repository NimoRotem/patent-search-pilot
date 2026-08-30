"""Drawing + PDF recovery from EPO OPS, for publications Google Patents has nothing for.

WHY
---
The display path (`enrich_display`) gets drawings from SerpApi's `google_patents_details`
`images[]`, falling back to the Google Patents PDF and extracting figure sheets from it.
For a large slice of the corpus Google has NEITHER: measured over a 418-publication
stratified sample of this corpus, Google Patents carries no drawing asset and no PDF for
69% of DE publications and 16% of EP, while EPO OPS has a facsimile for essentially all of
them. DE1286275B (1969) is the canonical case — zero `patentimages` assets on Google, a
1-page "Drawing" plus a 4-page "FullDocument" instance on OPS.

WHAT THIS DOES
--------------
For one publication, when asked (never speculatively, never in bulk):
  1. ask OPS which facsimile instances exist (tiny XML, cached forever by `ops.ops_fetch`);
  2. pull the pages of the best instance, one request each, capped and disk-cached;
  3. stitch them into `data/pdfs/<pub>.pdf` — which makes `/pdf/<pub>` and `/api/pdfs`
     start working for that publication with no change to the web layer, because both
     already read exactly that path;
  4. extract figure sheets into `data/figures/<pub>/` using the EXISTING `drawings.py`
     pipeline, so the recently-fixed inverted-stencil and ink-transition calibration are
     inherited rather than reimplemented;
  5. record provenance in `field_provenance` so the UI can say WHICH office the drawing
     came from. For a patent attorney that is substantive information, not decoration.

A "Drawing" instance is drawing sheets by construction, so its pages are cropped without
running the drawing-vs-text classifier over them (the classifier is a heuristic tuned on
rendered pages and will discard dense mechanical figures). A "FullDocument" facsimile is an
unknown mix of cover, description and drawing pages, so there the classifier does run.

QUOTA: OPS free tier is 4 GB/week and is SHARED with the federated app and with the
long-running `ops.py --backfill-core` job. Images are much larger than text. Everything
here goes through `ops._ops_get`, which enforces the persisted weekly byte budget and the
X-Throttling-Control backoff, and every byte is cached on disk permanently.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import ops
from config import DATA

PDFDIR = DATA / "pdfs"
FIGDIR = DATA / "figures"


def _stitch(pages: list[bytes], dest: Path) -> bool:
    """Combine the one-page PDFs OPS returns into a single document at `dest`."""
    if not pages:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(pages) == 1:
        try:
            dest.write_bytes(pages[0])
            return True
        except Exception:
            return False
    with tempfile.TemporaryDirectory() as td:
        parts = []
        for i, b in enumerate(pages):
            p = os.path.join(td, f"p{i:03d}.pdf")
            with open(p, "wb") as f:
                f.write(b)
            parts.append(p)
        out = os.path.join(td, "out.pdf")
        try:
            r = subprocess.run(["pdfunite", *parts, out], capture_output=True, timeout=120)
            if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                dest.write_bytes(Path(out).read_bytes())
                return True
        except Exception:
            pass
        # pdfunite missing or unhappy: a single-page document is still better than nothing.
        try:
            dest.write_bytes(pages[0])
            return True
        except Exception:
            return False


def _figures_from_pages(pages: list[bytes], desc: str, pub: str) -> list[dict]:
    """Figure PNGs from the facsimile pages, written into data/figures/<pub>/."""
    import drawings
    import image_utils
    from PIL import Image
    import io

    known_drawings = (desc or "").lower() == "drawing"
    try:
        figdir = FIGDIR / pub
        figdir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []

    out: list[dict] = []
    with tempfile.TemporaryDirectory() as td:
        for pi, blob in enumerate(pages):
            page_pdf = Path(td) / f"page{pi:03d}.pdf"
            page_pdf.write_bytes(blob)
            if not known_drawings:
                # Unknown mix of cover/description/drawing pages -> reuse the full
                # pipeline, which classifies and splits.
                figs = drawings.figures_from_pdf(page_pdf)
            else:
                figs = []
                sub = Path(td) / f"x{pi}"
                sub.mkdir(exist_ok=True)
                for args, prefix in (
                        (["pdfimages", "-png", str(page_pdf), str(sub / "img")], "img"),
                        (["pdftoppm", "-png", "-r", "150", str(page_pdf), str(sub / "pg")], "pg")):
                    try:
                        subprocess.run(args, capture_output=True, timeout=120)
                    except Exception:
                        continue
                    for fn in sorted(os.listdir(sub)):
                        if not fn.startswith(prefix):
                            continue
                        try:
                            with Image.open(sub / fn) as im:
                                if min(im.size) < drawings.MIN_DIM:
                                    continue
                                buf = io.BytesIO()
                                im.convert("RGB").save(buf, "PNG")
                                raw = buf.getvalue()
                            # Already known to be a drawing sheet -> crop only.
                            tight = image_utils.process(raw, drop_text=False)
                            if tight:
                                figs.append(tight)
                        except Exception:
                            continue
                    if figs:
                        break
            for f in figs:
                idx = len(out)
                if idx >= drawings.MAX_FIGS:
                    break
                name = f"ops{idx:03d}.png"
                try:
                    (figdir / name).write_bytes(f)
                    out.append({"file": name, "src_url": None, "from_ops": True})
                except Exception:
                    continue
    return out


def recover(pub: str, max_pages: int = ops.MAX_IMAGE_PAGES) -> dict:
    """Recover drawings + a PDF for one publication from EPO OPS.

    Returns {} when OPS has nothing (or credentials/budget are unavailable), else
    {images: [...], pdf_local, n_pages, instance, provenance}.
    """
    if not ops.have_creds():
        return {}
    try:
        fac = ops.fetch_facsimile(pub, max_pages=max_pages)
    except ops.OpsBudgetExceeded:
        return {}
    except Exception:
        return {}
    if not fac or not fac.get("pages"):
        return {}

    pdf_dest = PDFDIR / f"{pub}.pdf"
    has_pdf = _stitch(fac["pages"], pdf_dest)
    imgs = _figures_from_pages(fac["pages"], fac.get("desc", ""), pub)
    inst = fac.get("desc") or "facsimile"
    return {
        "images": imgs,
        "n_images": len(imgs),
        "pdf_local": pdf_dest.name if has_pdf else None,
        "n_pages": len(fac["pages"]),
        "total_pages": fac.get("total_pages"),
        "instance": inst,
        "bytes": fac.get("bytes", 0),
        "provenance": (
            f"drawings recovered from EPO OPS ({inst}, {len(fac['pages'])} page(s)) — "
            f"Google Patents has no drawing assets for this publication"),
    }


def note_provenance(pub: str, info: dict) -> None:
    """Record in `field_provenance` that this publication's drawings came from OPS.

    Best-effort: a provenance write must never be the reason a user fails to see a
    drawing we already successfully fetched.
    """
    if not info:
        return
    try:
        import db
        with db.cursor() as cur:
            cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
            row = cur.fetchone()
            if not row:
                return
            pid = row["id"]
            src = db.get_source_id("epo:ops", "3.2")
            cur.execute(
                "SELECT 1 FROM field_provenance WHERE entity='publication' AND entity_id=%s "
                "AND field='ops_drawings' LIMIT 1", (pid,))
            if cur.fetchone():
                return
            cur.execute(
                "INSERT INTO field_provenance(entity,entity_id,field,source_id,ocr_status) "
                "VALUES ('publication',%s,'ops_drawings',%s,'authoritative')", (pid, src))
    except Exception:
        pass


def espacenet_url(pub: str, family_id: str | None = None) -> str:
    """Espacenet deep link. Family-scoped when the DOCDB simple family is known.

    Espacenet zero-pads the family id to NINE digits in the path (OPS returns it
    unpadded: '07128644' for DE1286275B -> '/family/007128644/'), and the query is an
    exact `pn=` lookup rather than a bare full-text term.
    """
    p = (pub or "").replace("-", "").upper()
    q = f"?q=pn%3D{p}"
    fid = "".join(ch for ch in str(family_id or "") if ch.isdigit())
    if fid:
        return (f"https://worldwide.espacenet.com/patent/search/family/{fid.zfill(9)}"
                f"/publication/{p}{q}")
    return f"https://worldwide.espacenet.com/patent/search/publication/{p}{q}"


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:] or ["DE-1286275-B"]:
        r = recover(p)
        print(f"{p}: {json.dumps({k: v for k, v in r.items() if k != 'images'}, default=str)}"
              f" figures={r.get('n_images', 0)}")
