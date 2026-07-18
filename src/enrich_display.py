"""Display-oriented enrichment for the results page (spec Milestone 2 §Enrichment).

PROVEN pattern: one SerpApi `google_patents_details` call per ref returns the whole display
payload — pdf, images[], assignees, inventors, claims, classifications, legal_events,
patent_citations, cited_by, similar_documents, abstract, family_id, priority_date. So per ref:
  SerpApi once -> cache data/enriched/<pub>.json
              -> download images to data/figures/<pub>/
              -> download pdf to data/pdfs/<pub>.pdf, set publications.facsimile_path
Then serve everything from the local cache (images never break, fast render).

Graceful degradation: old DE/EP/WO (e.g. Probst DE-4327663-A1, 1993) return 0 images / no pdf ->
we mark facsimile_not_digitized and still surface claims/abstract/events + Espacenet/Google links.
Never returns a broken image URL (only locally-downloaded files are advertised).
"""
from __future__ import annotations
import os, re, json, time, hashlib, subprocess
from pathlib import Path
import requests
import db
from config import DATA
import enrich  # reuse fetch_details / gp_id / _safe_date

ENRICHED = DATA / "enriched"
FIGDIR = DATA / "figures"
PDFDIR = DATA / "pdfs"

_PUB_RE = re.compile(r"^[A-Za-z]{2}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")


def _pubkey(pub):
    """A publication number is the ONLY thing allowed to name a cache file / figure dir.
    Reject anything that isn't a clean pub number so a caller can never traverse the filesystem
    (defense-in-depth, independent of the web layer)."""
    if not pub or len(str(pub)) > 40 or not _PUB_RE.match(str(pub)):
        raise ValueError(f"unsafe publication key: {pub!r}")
    return str(pub)


for d in (ENRICHED, FIGDIR, PDFDIR):
    d.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (patent-pilot enrichment)"}
IMG_TIMEOUT = 40


def espacenet_url(pub):
    return f"https://worldwide.espacenet.com/patent/search?q={pub.replace('-','')}"


def google_patents_url(pub):
    return f"https://patents.google.com/patent/{pub.replace('-','')}/en"


_GPDF_RE = re.compile(r"https://patentimages\.storage\.googleapis\.com/[^\s\"'<>]+\.pdf")

def _scrape_google_pdf(pub):
    """Fallback PDF source: scrape the Google Patents page for its patentimages PDF link."""
    try:
        r = requests.get(google_patents_url(pub), headers=UA, timeout=30)
        if r.status_code == 200:
            m = _GPDF_RE.search(r.text)
            if m:
                return m.group(0)
    except requests.RequestException:
        pass
    return None


def _download(url, dest: Path, retries=2):
    if dest.exists() and dest.stat().st_size > 0:
        return True
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=IMG_TIMEOUT, stream=True)
            if r.status_code == 200:
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                if tmp.stat().st_size > 0:
                    tmp.rename(dest)
                    return True
                tmp.unlink(missing_ok=True)
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return False


def _fig_ext(url):
    m = re.search(r"\.(png|jpg|jpeg|gif|tif|tiff)(\?|$)", url, re.I)
    return "." + (m.group(1).lower() if m else "png")


def _pdftotext_len(pdf_path, page):
    try:
        r = subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), str(pdf_path), "-"],
                           capture_output=True, timeout=25)
        return len((r.stdout or b"").decode("utf-8", "ignore").strip())
    except Exception:
        return 0


def extract_pdf_drawings(pdf_path, pub, cap=16, min_side=340):
    """Extract drawing images from a local PDF — for references that have a PDF but NO digitized
    figures (common for old EP/DE/WO). Pulls embedded rasters with pdfimages, drops tiny logos /
    barcodes / rule-lines and (for PDFs that carry a text layer) text-dense pages, keeping the
    figure sheets. Returns display-shaped image dicts (already saved under the figure dir)."""
    from PIL import Image
    try:
        figdir = FIGDIR / _pubkey(pub)
    except ValueError:
        return []
    figdir.mkdir(parents=True, exist_ok=True)
    tmp = figdir / "_pdfx"
    if tmp.exists():
        for x in tmp.glob("*"):
            x.unlink(missing_ok=True)
    tmp.mkdir(exist_ok=True)
    try:
        subprocess.run(["pdfimages", "-png", "-p", str(pdf_path), str(tmp / "p")],
                       timeout=180, capture_output=True)
    except Exception:
        return []
    cands = []                                     # (page, file, area)
    for f in sorted(tmp.glob("p-*.png")):
        m = re.match(r"p-(\d+)-\d+", f.stem)
        page = int(m.group(1)) if m else None
        try:
            with Image.open(f) as im:
                w, h = im.size
        except Exception:
            continue
        ar = w / max(1, h)
        if min(w, h) < min_side or ar < 0.2 or ar > 5:     # tiny / banner / rule-line
            continue
        cands.append((page, f, w * h))
    # if the PDF has a real text layer, drop the text-dense pages (keep figure sheets)
    ptext = {p: _pdftotext_len(pdf_path, p) for p in {c[0] for c in cands if c[0]}}
    if any(v > 200 for v in ptext.values()):
        cands = [c for c in cands if c[0] is None or ptext.get(c[0], 0) < 500]
    cands.sort(key=lambda t: (t[0] if t[0] is not None else 9999))
    imgs = []
    for i, (_, f, _) in enumerate(cands[:cap]):
        dest = figdir / f"pdf{i:03d}.png"
        try:
            f.replace(dest)
            imgs.append({"file": dest.name, "src_url": None, "from_pdf": True})
        except Exception:
            pass
    for x in tmp.glob("*"):
        x.unlink(missing_ok=True)
    try:
        tmp.rmdir()
    except OSError:
        pass
    return imgs


def cache_path(pub):
    return ENRICHED / f"{_pubkey(pub)}.json"


def load_cached(pub):
    try:
        p = cache_path(pub)
    except ValueError:
        return None                           # unsafe key -> no cache, no traversal
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _normalize(pub, raw):
    """Turn the raw SerpApi payload into a compact display dict + record local asset paths."""
    pub = _pubkey(pub)                       # never build a figure dir from an unvalidated key
    imgs = raw.get("images") or []
    figdir = FIGDIR / pub
    figdir.mkdir(parents=True, exist_ok=True)
    local_imgs = []
    for i, url in enumerate(imgs):
        dest = figdir / f"{i:03d}{_fig_ext(url)}"
        if _download(url, dest):
            local_imgs.append({"file": dest.name, "src_url": url})
    # PDF: SerpApi payload first, then scrape the Google Patents page for a patentimages PDF link.
    pdf_url = raw.get("pdf") or _scrape_google_pdf(pub)
    pdf_source = "serpapi" if raw.get("pdf") else ("google" if pdf_url else None)
    local_pdf = None
    if pdf_url:
        dest = PDFDIR / f"{pub}.pdf"
        if _download(pdf_url, dest):
            local_pdf = dest.name
    # Drawings missing but a PDF exists -> extract the figure sheets from the PDF.
    figs_from_pdf = False
    if not local_imgs and local_pdf:
        extracted = extract_pdf_drawings(PDFDIR / f"{pub}.pdf", pub)
        if extracted:
            local_imgs = extracted
            figs_from_pdf = True
    # classifications -> CPC chips
    cls = []
    for c in (raw.get("classifications") or []):
        if isinstance(c, dict) and c.get("code"):
            cls.append({"code": c["code"], "description": c.get("description"),
                        "first": bool(c.get("first_code")), "cpc": bool(c.get("is_cpc", True))})
    # citations (X/Y/A + origin) from patent_citations.original
    cites = []
    pc = raw.get("patent_citations") or {}
    for grp, origin in [("original", "applicant"), ("family_to_family", "search-report")]:
        for c in (pc.get(grp) or []):
            if isinstance(c, dict) and c.get("publication_number"):
                cites.append({"pub": c["publication_number"],
                              "category": c.get("type") or ("examiner" if c.get("examiner_cited") else None),
                              "origin": "examiner" if c.get("examiner_cited") else origin,
                              "date": c.get("priority_date")})
    inv = []
    for x in (raw.get("inventors") or []):
        inv.append(x.get("name") if isinstance(x, dict) else str(x))
    assignees = [a if isinstance(a, str) else a.get("name") for a in (raw.get("assignees") or [])]
    events = []
    for e in (raw.get("legal_events") or raw.get("events") or []):
        if isinstance(e, dict):
            events.append({"date": enrich._safe_date(e.get("date")),
                           "code": e.get("code") or e.get("type"),
                           "title": e.get("title")})
    similar = []
    for s in (raw.get("similar_documents") or [])[:12]:
        if isinstance(s, dict) and s.get("publication_number"):
            similar.append({"pub": s["publication_number"], "title": s.get("title")})
    return {
        "pub": pub,
        "title": raw.get("title"),
        "abstract": raw.get("abstract"),
        "assignees": [a for a in assignees if a],
        "inventors": inv,
        "priority_date": raw.get("priority_date"),
        "filing_date": raw.get("filing_date"),
        "publication_date": raw.get("publication_date"),
        "family_id": str(raw.get("family_id")) if raw.get("family_id") else None,
        "country": raw.get("country"),
        "type": raw.get("type"),
        "classifications": cls,
        "citations": cites[:60],
        "cited_by_count": len((raw.get("cited_by") or {}).get("original", []) or []),
        "similar": similar,
        "legal_events": events[:30],
        "claims": raw.get("claims") if isinstance(raw.get("claims"), list) else None,
        "images": local_imgs,
        "n_images": len(local_imgs),
        "pdf_local": local_pdf,
        "pdf_url": pdf_url,
        "pdf_source": pdf_source,
        "figs_from_pdf": figs_from_pdf,
        "facsimile_digitized": bool(local_imgs or local_pdf),
        "espacenet": espacenet_url(pub),
        "google_patents": google_patents_url(pub),
    }


def _merge_lens(pub, disp):
    """Fold Lens.org fields into the display when a LENS_TOKEN is configured (else a no-op): the
    authoritative INPADOC legal status, family jurisdictions, and any missing abstract/claims."""
    try:
        import lens
    except Exception:
        return
    if not lens.available():
        return
    try:
        L = lens.fetch(pub)
    except Exception:
        return
    if not L:
        return
    if L.get("legal_status"):
        disp["lens_status"] = L["legal_status"]
        disp["lens_term_date"] = L.get("anticipated_term_date")
    if L.get("family_members"):
        disp["lens_family"] = L["family_members"]
    if not disp.get("abstract") and L.get("abstract"):
        disp["abstract"] = L["abstract"]
    if not disp.get("claims") and L.get("claims"):
        disp["claims"] = L["claims"]


def enrich_for_display(pub, force=False):
    """Return the compact display dict for a publication, using the on-disk cache when present.
    Downloads drawings + PDF locally on first call. Never blocks on a broken image."""
    try:
        _pubkey(pub)                          # reject unsafe keys before any filesystem use
    except ValueError:
        return {"pub": str(pub)[:60], "facsimile_digitized": False, "images": [], "n_images": 0,
                "pdf_local": None, "pdf_url": None, "no_details": True,
                "espacenet": None, "google_patents": None}
    cached = None if force else load_cached(pub)
    if cached and "_display" in cached:
        return cached["_display"]
    raw = enrich.fetch_details(pub)
    if not raw:
        # graceful: no SerpApi payload — still give the user the external links
        disp = {"pub": pub, "facsimile_digitized": False, "images": [], "n_images": 0,
                "pdf_local": None, "pdf_url": None, "no_details": True,
                "espacenet": espacenet_url(pub), "google_patents": google_patents_url(pub)}
        cache_path(pub).write_text(json.dumps({"_display": disp}, indent=1))
        return disp
    disp = _normalize(pub, raw)
    _merge_lens(pub, disp)                     # authoritative legal status + family (if LENS_TOKEN set)
    # persist raw + normalized display together
    cache_path(pub).write_text(json.dumps({"raw": raw, "_display": disp}, default=str))
    # keep DB facsimile_path in sync (local pdf preferred)
    try:
        if disp.get("pdf_local") or disp.get("pdf_url"):
            with db.cursor() as cur:
                cur.execute("UPDATE publications SET facsimile_path=%s WHERE publication_number=%s",
                            (disp.get("pdf_url"), pub))
    except Exception:
        pass
    return disp


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:] or ["US-11999030-B2", "DE-202019005606-U1", "DE-4327663-A1"]:
        d = enrich_for_display(p)
        print(f"{p}: imgs={d.get('n_images')} pdf={bool(d.get('pdf_local'))} "
              f"digitized={d.get('facsimile_digitized')} claims={len(d.get('claims') or [])} "
              f"events={len(d.get('legal_events') or [])}")
