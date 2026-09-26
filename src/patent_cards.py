"""A front-page drawing and a short abstract for every patent on the actions docket.

A row that says "US20250332741A1, GRIPPING APPARATUS..." cannot be told from the one under it
without opening both. So each patent gets what a person skims by: its first drawing as a
thumbnail and two lines of its abstract, read once from its Google Patents page and kept.

WHERE FROM. The Google Patents page carries both: the abstract (in English for a German or
Chinese document, where the page translates it, and the first claim where a B publication has no
abstract) and a thumbnail of every drawing sheet on patentimages.storage.googleapis.com. It is
fetched directly first; when this box is refused it goes through ScrapingBee, which needs
custom_google for a google.com page and must NOT have it for googleapis.com (the wrong way round
is an HTTP 400 that reads like "nothing there").

WHEN GOOGLE HAS NO DRAWING. Measured 2026-09-26: the image store answers 403 for the newest US
publications, and a German or European B publication lists no drawing sheets at all. So a US
case falls back to the drawings document (DRW) in its USPTO file wrapper, and an EP, DE or WO
case to the EPO's own "Drawing" facsimile, page one, through ops.py, which counts every byte
against the weekly OPS allowance and caches the page forever. A page Google does not have yet
(a publication from this week) takes its abstract from the EPO where the EPO has one.

WHERE KEPT. The abstract in data/observations/patent_cards.json, one entry per publication. The
drawing as a small JPEG beside the design views in observation_marks.IMAGE_DIR, so the route that
already serves those, only for a publication on the reader's own docket, serves these too. A page
that could not be read is retried after RETRY_DAYS, not on every sweep.
"""
from __future__ import annotations

import datetime
import html
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import observation_marks

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
CARDS_FILE = Path(os.environ.get("PATENT_CARDS_FILE",
                                 str(_HERE.parent / "data" / "observations" / "patent_cards.json")))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")
RETRY_DAYS = int(os.environ.get("PATENT_CARDS_RETRY_DAYS", "7"))
THUMB_PX = 320
MAX_ABSTRACT = 2000
#  Bumped when what a card holds changes, so every stored card is read once more.
VERSION = 3

_LOCK = threading.Lock()
_CACHE = {"mtime": None, "data": {}}


# ---------------------------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------------------------

def load(path=None):
    """Every card, by publication. Re-read only when the file changed."""
    path = Path(path) if path else CARDS_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if path == CARDS_FILE and _CACHE["mtime"] == mtime:
        return _CACHE["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if path == CARDS_FILE:
        _CACHE.update(mtime=mtime, data=data)
    return data


def _save(data, path=None):
    path = Path(path) if path else CARDS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def needs(card, today=None):
    """True when a publication has no card, a card missing its drawing or abstract that was
    last tried long enough ago, or one never tried against every source."""
    if not card or card.get("v") != VERSION:
        return True
    if card.get("abstract") and card.get("image"):
        return False
    if not card.get("tried"):
        return True                         # a card from before the fallbacks existed
    today = today or datetime.date.today()
    try:
        when = datetime.date.fromisoformat(str(card.get("tried"))[:10])
    except ValueError:
        return True
    return (today - when).days >= RETRY_DAYS


# ---------------------------------------------------------------------------------------------
# reading a page
# ---------------------------------------------------------------------------------------------

def gp_url(publication):
    return "https://patents.google.com/patent/%s/en" % urllib.parse.quote(
        re.sub(r"[^A-Za-z0-9]", "", str(publication or "")))


def _direct(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _bee(url, timeout=90):
    key = os.environ.get("SCRAPINGBEE_API_KEY", "")
    if not key:
        raise RuntimeError("no SCRAPINGBEE_API_KEY")
    q = {"api_key": key, "url": url, "render_js": "false"}
    if urllib.parse.urlparse(url).hostname.endswith("google.com"):
        q["custom_google"] = "true"
    req = urllib.request.Request("https://app.scrapingbee.com/api/v1/?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def fetch(url):
    """Direct first; ScrapingBee when this box is refused. -> bytes, or raises. A 404 is final."""
    try:
        st, body = _direct(url)
        if st == 200 and body:
            return body
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise
    except Exception:
        pass
    st, body = _bee(url)
    if st != 200:
        raise RuntimeError("HTTP %s" % st)
    return body


def fetch_image(url):
    """A drawing from Google's image store, directly and only directly. When the store refuses
    (403 for a publication it has not filled yet), ScrapingBee is refused too, slowly; the
    office's own drawing is the next source, not a second try here."""
    st, body = _direct(url, timeout=30)
    if st != 200 or not body:
        raise RuntimeError("HTTP %s" % st)
    return body


_NUMERALS = re.compile(r"\s*\((?:\d+[a-z']?(?:\s*[-,.]\s*\d+[a-z']?)*)\)")


_SRC_TEXT = re.compile(r'<span class="google-src-text">[\s\S]*?</span>')


def _text(fragment):
    """Visible English text. The /en page puts a foreign original in google-src-text spans
    beside Google's translation; keeping both printed German and English run together."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", _SRC_TEXT.sub(" ", fragment or ""))).split())


def _clip(text, limit=MAX_ABSTRACT):
    """At most `limit` characters, cut at the last full sentence when it has to be cut."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = cut.rfind(". ")
    return (cut[:end + 1] if end > limit // 2 else cut.rstrip() + "...")


def parse(page):
    """-> (abstract, [drawing image URLs, thumbnails first]) from a Google Patents page."""
    s = page.decode("utf-8", "replace") if isinstance(page, bytes) else str(page or "")
    abstract = ""
    sec = re.search(r'<section itemprop="abstract"[\s\S]*?</section>', s)
    if sec:
        divs = re.findall(r'<div class="abstract"[^>]*>([\s\S]*?)</div>', sec.group(0))
        abstract = _text(divs[0]) if divs else ""
    if not abstract:
        m = re.search(r'<meta name="description" content="([^"]*)"', s)
        abstract = _text(m.group(1)) if m else ""
    if not abstract:
        #  A B publication has no abstract; its first claim, in Google's English, says what it
        #  covers just as well.
        claims = re.search(r'<section itemprop="claims"[\s\S]*?</section>', s)
        first = re.search(r'<div[^>]*class="claim-text"[^>]*>([\s\S]*?)</div>', claims.group(0)) if claims else None
        abstract = _text(first.group(1)) if first else ""
    #  Reference numerals help a claim chart and nobody skimming a list.
    abstract = _clip(_NUMERALS.sub("", abstract).strip())
    urls = re.findall(r'https://patentimages\.storage\.googleapis\.com/[^"\s]+?\.(?:png|jpg|gif)', s)
    seen, ordered = set(), []
    for u in sorted(urls, key=lambda u: ("/thumbnails/" not in u, "D00000" not in u)):
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return abstract, ordered


def thumbnail(data, px=THUMB_PX):
    """Image bytes -> a small JPEG, drawings kept black on white."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    im.thumbnail((px, px))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------------------------------------
# filling
# ---------------------------------------------------------------------------------------------

def _render_first_page(pdf, px=THUMB_PX):
    """A one-page PDF (a drawing sheet) -> JPEG bytes, via pdftoppm as the design views are."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "p.pdf"
        src.write_bytes(pdf)
        subprocess.run(["pdftoppm", "-png", "-f", "1", "-l", "1", "-scale-to", str(px * 2),
                        "-singlefile", str(src), str(Path(tmp) / "p")], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return thumbnail((Path(tmp) / "p.png").read_bytes(), px)


def ops_drawing(publication):
    """Page one of the EPO's "Drawing" facsimile for an EP, DE or WO publication. -> JPEG or None."""
    import ops
    if not ops.have_creds():
        return None                         # never the bundled sample drawings
    data = ops.ops_fetch(publication, want=("images",))
    insts = [i for i in (data or {}).get("images") or [] if (i.get("desc") or "").lower() == "drawing"]
    if not insts:
        return None
    pdf = ops.fetch_image_page(insts[0]["link"], 1)
    return _render_first_page(pdf) if pdf else None


def ops_abstract(publication):
    """The EPO's abstract for a publication, English first. -> str ("" when there is none)."""
    import observation_refresh as R
    st, j = R._ops_json("published-data/publication/epodoc/%s/abstract"
                        % urllib.parse.quote(R._epodoc(publication)))
    if st != 200:
        return ""
    docs = R._aslist(R._first(j, "ops:world-patent-data", "exchange-documents", "exchange-document"))
    blocks = []
    for d in docs:
        for a in R._aslist((d or {}).get("abstract")):
            if isinstance(a, dict):
                ps = [p.get("$") if isinstance(p, dict) else p for p in R._aslist(a.get("p"))]
                blocks.append((str(a.get("@lang") or "").lower(), " ".join(str(p or "") for p in ps)))
    blocks.sort(key=lambda b: b[0] != "en")
    return _clip(_NUMERALS.sub("", " ".join(blocks[0][1].split())).strip()) if blocks else ""


def _odp_pdf(app, code):
    """The newest document with this code on a US file wrapper, as PDF bytes, or None. The portal
    answers with a 302 to a signed URL that must be fetched WITHOUT the key header."""
    import observation_refresh as R
    d = R._odp("patent/applications/%s/documents" % app) or {}
    docs = [x for x in d.get("documentBag") or [] if str(x.get("documentCode") or "") == code]
    docs.sort(key=lambda x: str(x.get("officialDate") or ""), reverse=True)
    url = next((o.get("downloadUrl") for x in docs for o in x.get("downloadOptionBag") or []
                if o.get("mimeTypeIdentifier") == "PDF" and o.get("downloadUrl")), None)
    if not url:
        return None
    key = os.environ.get("USPTO_ODP_KEY", "") or os.environ.get("ODP_API_KEY", "")

    class _Stop(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        with urllib.request.build_opener(_Stop).open(
                urllib.request.Request(url, headers={"X-API-KEY": key}), timeout=60) as fh:
            location, body = fh.headers.get("Location"), fh.read()
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307):
            raise
        location, body = exc.headers.get("Location"), b""
    if location:
        with urllib.request.urlopen(location, timeout=60) as fh:
            body = fh.read()
    return body if body.startswith(b"%PDF") else None


def us_abstract(row):
    """The abstract document (ABST) of a US file wrapper, as text. -> str ("" when unreadable)."""
    import subprocess
    import tempfile
    app = re.sub(r"\D", "", str((row or {}).get("application") or ""))
    pdf = _odp_pdf(app, "ABST") if app else None
    if not pdf:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.pdf"
        src.write_bytes(pdf)
        out = subprocess.run(["pdftotext", "-layout", str(src), "-"], capture_output=True, timeout=60)
    text = " ".join(out.stdout.decode("utf-8", "replace").split())
    text = re.sub(r"^.*?\bABSTRACT\b(?: OF THE DISCLOSURE)?\s*", "", text, count=1, flags=re.I)
    return _clip(_NUMERALS.sub("", text).strip()) if len(text) > 40 else ""


def us_drawing(row):
    """The first drawing sheet in a US case's file wrapper, via the design-view code. -> file name"""
    path = observation_marks.fetch_uspto_drawing(row)
    return path.name if path else None


def card_for(publication, fetcher=None, image_dir=None, row=None, us=None, ops_img=None,
             ops_abs=None, old=None, image_fetcher=None, us_abs=None):
    """Read one publication's page and keep its drawing, from the next source when Google has
    none. -> card dict (never raises)."""
    image_fetcher = image_fetcher or (fetcher if fetcher else fetch_image)
    fetcher = fetcher or fetch
    image_dir = Path(image_dir) if image_dir else observation_marks.IMAGE_DIR
    today = datetime.date.today().isoformat()
    card = {"v": VERSION, "fetched": today, "tried": today, "abstract": "",
            "image": "", "error": "", "source": ""}
    urls = []
    try:
        abstract, urls = parse(fetcher(gp_url(publication)))
        card["abstract"] = abstract or card["abstract"]
    except urllib.error.HTTPError as exc:
        card["error"] = "Google Patents HTTP %s" % exc.code
    except Exception as exc:
        card["error"] = "Google Patents %s: %s" % (type(exc).__name__, str(exc)[:100])
    name = "%s.jpg" % observation_marks.image_key(publication)
    for url in urls[:3]:
        try:
            small = thumbnail(image_fetcher(url))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                break                       # the store has none of this publication's sheets
            continue
        except Exception:
            continue
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / name).write_bytes(small)
        card.update(image=name, source="google")
        break
    cc = str(publication or "")[:2].upper()
    if not card["image"]:
        try:
            if cc == "US" and (row or {}).get("application"):
                got = (us or us_drawing)({"application": row["application"], "publication": publication})
                if got:
                    card.update(image=got, source="uspto")
            elif cc in ("EP", "DE", "WO"):
                small = (ops_img or ops_drawing)(publication)
                if small:
                    image_dir.mkdir(parents=True, exist_ok=True)
                    (image_dir / name).write_bytes(small)
                    card.update(image=name, source="epo")
        except Exception as exc:
            card["error"] = (card["error"] + "; " if card["error"] else "") + "drawing: %s" % str(exc)[:100]
    if not card["abstract"]:
        try:
            if cc in ("EP", "DE", "WO"):
                card["abstract"] = (ops_abs or ops_abstract)(publication)
            elif cc == "US" and (row or {}).get("application"):
                #  A US publication Google has not indexed yet: its own abstract document.
                card["abstract"] = (us_abs or us_abstract)(row)
        except Exception:
            pass
    if card["abstract"] and card["image"]:
        card["error"] = ""
    return card


def _rows(items):
    """Publication numbers or docket rows -> {publication: row}, first one wins."""
    out = {}
    for it in items or []:
        row = it if isinstance(it, dict) else {"publication": it}
        pub = row.get("publication")
        if pub and pub not in out:
            out[pub] = row
    return out


def fill(items, limit=None, fetcher=None, path=None, image_dir=None, pause=0.5, **sources):
    """Give every publication that needs one a card. `items` are publication numbers or docket
    rows (a row's application number lets a US case use its file wrapper). Safe to call from two
    threads: one fills at a time and the other returns at once.
    -> {"done": n, "images": n, "errors": n}"""
    if not _LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        data = dict(load(path))
        rows = _rows(items)
        todo = [p for p in rows if needs(data.get(p))]
        if limit:
            todo = todo[:limit]
        summary = {"done": 0, "images": 0, "errors": 0, "todo": len(todo)}
        for i, pub in enumerate(todo):
            card = card_for(pub, fetcher=fetcher, image_dir=image_dir, row=rows[pub],
                            old=data.get(pub), **sources)
            data[pub] = card
            summary["done"] += 1
            summary["images"] += bool(card["image"])
            summary["errors"] += bool(card["error"] and not card["abstract"])
            if (i + 1) % 10 == 0:
                _save(data, path)
            if pause:
                time.sleep(pause)
        if todo:
            _save(data, path)
        return summary
    finally:
        _LOCK.release()


def kick(items, limit=40):
    """Fill the missing cards of the docket on screen, in the background."""
    cards = load()
    missing = [r for p, r in _rows(items).items() if needs(cards.get(p))]
    if not missing or _LOCK.locked():
        return False
    threading.Thread(target=fill, args=(missing,), kwargs={"limit": limit},
                     name="patent-cards", daemon=True).start()
    return True
