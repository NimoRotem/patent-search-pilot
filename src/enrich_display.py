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
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
import requests
import db
from config import DATA
import enrich  # reuse fetch_details / gp_id / _safe_date
import pubnorm  # canonical pub key + Mongo/Google candidate spellings (dropped-zero fix)
import mongo_corpus  # lemad 39.4M-doc pre-built corpus — figures + full text in ONE call

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


def espacenet_url(pub, family_id=None):
    """Espacenet deep link for a publication.

    The old form (`/patent/search?q=<number>`) was a bare full-text SEARCH, which can land
    on a result list or the wrong document. This is an exact `pn=` publication lookup, and
    when the DOCDB simple family is known it uses the richer family-scoped path that opens
    the document with its family panel populated. Both forms are live; the family id is
    zero-padded to nine digits, which is what Espacenet's path expects.
    """
    p = pub.replace("-", "").upper()
    q = f"?q=pn%3D{p}"
    fid = "".join(ch for ch in str(family_id or "") if ch.isdigit())
    if fid:
        return (f"https://worldwide.espacenet.com/patent/search/family/{fid.zfill(9)}"
                f"/publication/{p}{q}")
    return f"https://worldwide.espacenet.com/patent/search/publication/{p}{q}"


def _simple_family_id(pub):
    """DOCDB simple family id from the corpus, for the family-scoped Espacenet link.
    Free — it is already a column on `publications`; costs no OPS request."""
    try:
        with db.cursor() as cur:
            cur.execute("SELECT simple_family_id FROM publications "
                        "WHERE publication_number=%s LIMIT 1", (pub,))
            row = cur.fetchone()
            return str(row["simple_family_id"]) if row and row.get("simple_family_id") else None
    except Exception:
        return None


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
        tmp = None
        try:
            r = requests.get(url, headers=UA, timeout=IMG_TIMEOUT, stream=True)
            if r.status_code == 200:
                # Several result-card warmers may discover the same uncached document at once.
                # A single `<dest>.tmp` lets one request rename the file while another is still
                # writing/statting it, turning an otherwise successful /api/ref into a 500. Give
                # every request its own same-directory temporary file, then atomically publish it.
                # Concurrent winners contain the same remote payload, so replacing the destination
                # is safe; no reader can observe a partial file.
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                        mode="wb", dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp",
                        delete=False) as f:
                    tmp = Path(f.name)
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                if tmp.stat().st_size > 0:
                    os.replace(tmp, dest)
                    tmp = None
                    return True
        except (requests.RequestException, OSError):
            time.sleep(1.5 * (i + 1))
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
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

    # LAST TIER — EPO OPS. Google Patents genuinely has NO drawing asset and NO PDF for a
    # large slice of the corpus (69% of sampled DE publications, 16% of EP), and OPS has a
    # facsimile for almost all of them. Fires only when every Google channel above came up
    # empty, so publications that already resolve never spend a request against the shared
    # 4 GB/week OPS quota. This also populates data/pdfs/<pub>.pdf, which is what /pdf/<pub>
    # and /api/pdfs read — so it converts dead PDF links into real documents for free.
    ops_info = {}
    if not local_imgs and not local_pdf:
        try:
            import ops_drawings
            ops_info = ops_drawings.recover(pub) or {}
        except Exception:
            ops_info = {}
        if ops_info:
            local_imgs = ops_info.get("images") or local_imgs
            local_pdf = ops_info.get("pdf_local") or local_pdf
            try:
                ops_drawings.note_provenance(pub, ops_info)
            except Exception:
                pass
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
        "pdf_source": "epo_ops" if (ops_info and not pdf_source) else pdf_source,
        "figs_from_pdf": figs_from_pdf,
        "facsimile_digitized": bool(local_imgs or local_pdf),
        # PROVENANCE — which office each asset actually came from. A patent attorney needs
        # to know that a drawing is the EPO's facsimile of the DE original rather than
        # something Google rendered.
        "drawings_source": ("epo_ops" if ops_info else
                            ("google_pdf" if figs_from_pdf else
                             ("google" if local_imgs else None))),
        "drawings_provenance": (ops_info or {}).get("provenance", ""),
        "ops_instance": (ops_info or {}).get("instance", ""),
        "ops_pages": (ops_info or {}).get("n_pages", 0),
        # BOTH office links, always. Neither source is a superset of the other: Google is
        # richer for US and has the citation tooling, Espacenet holds the original
        # facsimiles for the older and non-US publications Google simply does not carry.
        "espacenet": espacenet_url(pub, _simple_family_id(pub)),
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


# ===========================================================================================
# lemad Mongo corpus — figures + full text in ONE call (the iptorch approach)
# ===========================================================================================
# iptorch shows content instantly because it reads a PRE-BUILT corpus rather than recovering
# figures live. We do the same: one mongo_corpus.get_detail() returns the whole payload, and the
# figures are Google-CDN {full,thumbnail} URLs that render directly — no download, no OPS, no PDF
# raster. We only fall back to the (slow, flaky) live-recovery chain below on a genuine Mongo miss.

def _mongo_images(md):
    """Mongo figures -> display image dicts. `file` is None (nothing is downloaded); the card
    renders `thumbnail` and the lightbox opens `full`, both remote Google-CDN URLs."""
    out = []
    for f in (md.get("figures") or []):
        full = f.get("full")
        if not full:
            continue
        out.append({"file": None, "full": full,
                    "thumbnail": f.get("thumbnail") or full,
                    "src_url": full, "from_mongo": True})
    return out


def _display_from_mongo(pub, md):
    """Build the full display dict from a lemad Mongo doc alone — no SerpApi, no download.

    Same shape as _normalize() so every downstream consumer keeps working, with three additions:
    `description` (full body text Mongo carries), remote-URL image entries, and
    drawings_source='lemad_mongo'. Citations / legal events / similar are not in this corpus —
    they are populated lazily by ensure_raw() when the citation graph is opened."""
    imgs = _mongo_images(md)
    return {
        "pub": pub,
        "title": md.get("title"),
        "abstract": md.get("abstract"),
        "assignees": md.get("assignees") or [],
        "inventors": md.get("inventors") or [],
        "priority_date": None,
        "filing_date": None,
        "publication_date": md.get("publication_date"),
        "family_id": None,
        "country": md.get("country"),
        "type": None,
        "classifications": md.get("classifications") or [],
        "citations": [],
        "cited_by_count": 0,
        "similar": [],
        "legal_events": [],
        "claims": md.get("claims"),
        "description": md.get("description"),        # full body text, straight from the corpus
        "images": imgs,
        "n_images": len(imgs),
        "pdf_local": None,                           # remote CDN pdf; nothing downloaded
        "pdf_url": md.get("pdf_url"),
        "pdf_source": "lemad_mongo" if md.get("pdf_url") else None,
        "figs_from_pdf": False,
        "facsimile_digitized": bool(imgs or md.get("pdf_url")),
        "drawings_source": "lemad_mongo" if imgs else None,
        "drawings_provenance": ("figures from the lemad patent corpus (Google-CDN facsimile)"
                                if imgs else ""),
        "ops_instance": "",
        "ops_pages": 0,
        "mongo_key": md.get("mongo_key"),
        "source": "lemad_mongo",
        "espacenet": espacenet_url(pub, _simple_family_id(pub)),
        "google_patents": google_patents_url(pub),
    }


def _merge_mongo_text(disp, md):
    """Overlay Mongo's authoritative full text onto a display built by the recovery path.

    Used when Mongo has the document but NO figures (a partial stub): we keep the recovered
    figures/pdf, but Mongo's title/abstract/claims/description/classifications/parties are more
    complete than what a bare recovery yields, so they fill any gap. Mongo never blanks a field
    the recovery already populated."""
    if not md:
        return
    for k in ("title", "abstract", "publication_date", "country"):
        if md.get(k) and not disp.get(k):
            disp[k] = md[k]
    for k in ("claims", "description", "classifications", "assignees", "inventors"):
        if md.get(k) and not disp.get(k):
            disp[k] = md[k]
    if md.get("mongo_key"):
        disp["mongo_key"] = md["mongo_key"]
    disp.setdefault("source", "lemad_mongo+recovery")


def remote_thumbs(pub):
    """[{full, thumbnail}] for a pub if its cached display carries Mongo (remote) figures, else [].

    For Integrate: the batch thumbnail endpoint (/api/figs) reads the figure DIRECTORY only and so
    cannot see Mongo's remote URLs. Call this to union remote thumbnails into that manifest so the
    results list can render a thumbnail with no download and no round-trip through recovery."""
    pub = pubnorm.canonical(pub) or pub       # accept either stored spelling
    cached = load_cached(pub) or {}
    disp = cached.get("_display") or {}
    out = []
    for im in (disp.get("images") or []):
        if im.get("from_mongo") and im.get("full"):
            out.append({"full": im["full"], "thumbnail": im.get("thumbnail") or im["full"]})
    return out


def ensure_raw(pub):
    """Lazily fetch + cache the SerpApi `raw` payload (citations / cited_by / similar_documents)
    for a pub that was served from Mongo (which has none of those).

    For Integrate: /api/graph reads `load_cached(pub)['raw']`. A Mongo-served pub has no `raw`, so
    the citation graph would come up empty. Have /api/graph call `enrich_display.ensure_raw(pub)`
    (instead of re-running enrich_for_display) to populate it on demand. Returns the raw dict or
    None; never raises."""
    try:
        canon = pubnorm.canonical(pub) or pub
        _pubkey(canon)
    except Exception:
        return None
    pub = canon
    cached = load_cached(pub) or {}
    if cached.get("raw"):
        return cached["raw"]
    try:
        raw = enrich.fetch_details(pub)
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        cached["raw"] = raw
        cache_path(pub).write_text(json.dumps(cached, default=str))
    except Exception:
        pass
    return raw


def enrich_for_display(pub, force=False):
    """Return the compact display dict for a publication, using the on-disk cache when present.

    MONGO FIRST: try the pre-built lemad corpus. If it has the figures, we render them straight
    from Google-CDN URLs — one call, no download, no OPS, no PDF raster (this is what makes
    content appear instantly, as iptorch does, and it fixes the dropped-leading-zero bug that hid
    US-2019168875-A1's four sketches). Only on a Mongo miss OR a figure-less Mongo stub do we fall
    back to the existing live recovery (SerpApi images -> Google PDF scrape -> EPO OPS facsimile);
    a figure-less stub still keeps Mongo's richer full text. Never blocks on a broken image."""
    # Canonicalize FIRST so both stored spellings — the hyphenated corpus key 'US-2019168875-A1'
    # and the concatenated report key 'US2019168875A1' — resolve to the same safe filesystem key.
    canon = pubnorm.canonical(pub) or pub
    try:
        _pubkey(canon)                        # reject unsafe keys before any filesystem use
    except ValueError:
        return {"pub": str(pub)[:60], "facsimile_digitized": False, "images": [], "n_images": 0,
                "pdf_local": None, "pdf_url": None, "no_details": True,
                "espacenet": None, "google_patents": None}
    pub = canon
    cached = None if force else load_cached(pub)
    if cached and "_display" in cached:
        return cached["_display"]

    # ---- MONGO FAST PATH: figures + full text in one call, nothing downloaded ----------------
    md = None
    try:
        md = mongo_corpus.get_detail(pub)
    except Exception:
        md = None
    if md and md.get("figures"):
        disp = _display_from_mongo(pub, md)
        _merge_lens(pub, disp)                # authoritative legal status/family if LENS_TOKEN set
        cache_path(pub).write_text(json.dumps({"mongo": md, "_display": disp}, default=str))
        try:
            if disp.get("pdf_url"):
                with db.cursor() as cur:
                    cur.execute("UPDATE publications SET facsimile_path=%s "
                                "WHERE publication_number=%s", (disp.get("pdf_url"), pub))
        except Exception:
            pass
        return disp

    # ---- FALLBACK: existing live recovery (Mongo missed, or has the doc but no figures) -------
    raw = enrich.fetch_details(pub)
    if not raw:
        # No SerpApi payload at all. This is NOT a rare edge case — it is the normal
        # outcome for exactly the publications this whole recovery path exists for (old DE
        # / EP nationals), so it must try OPS rather than give up. Previously it returned
        # bare links and the user saw no drawings even though the EPO had them.
        ops_info = {}
        try:
            import ops_drawings
            ops_info = ops_drawings.recover(pub) or {}
            if ops_info:
                ops_drawings.note_provenance(pub, ops_info)
        except Exception:
            ops_info = {}
        imgs = ops_info.get("images") or []
        disp = {"pub": pub,
                "facsimile_digitized": bool(imgs or ops_info.get("pdf_local")),
                "images": imgs, "n_images": len(imgs),
                "pdf_local": ops_info.get("pdf_local"), "pdf_url": None,
                "no_details": True,
                "drawings_source": "epo_ops" if ops_info else None,
                "drawings_provenance": ops_info.get("provenance", ""),
                "ops_instance": ops_info.get("instance", ""),
                "ops_pages": ops_info.get("n_pages", 0),
                "espacenet": espacenet_url(pub, _simple_family_id(pub)),
                "google_patents": google_patents_url(pub)}
        _merge_mongo_text(disp, md)            # figure-less Mongo stub still gives us full text
        cache_path(pub).write_text(json.dumps({"mongo": md, "_display": disp}, default=str))
        return disp
    disp = _normalize(pub, raw)
    _merge_mongo_text(disp, md)                # Mongo's fuller claims/description/CPC when present
    _merge_lens(pub, disp)                     # authoritative legal status + family (if LENS_TOKEN set)
    # persist raw + normalized display together
    cache_path(pub).write_text(json.dumps({"raw": raw, "mongo": md, "_display": disp}, default=str))
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
