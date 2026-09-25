"""What is inside a package, and what is on the USPTO's file, readable on the docket itself.

ONE VIEWER FOR BOTH. A package is a zip of twenty files, and "which version of the concise
description went in" used to mean download, unzip and open PDFs one by one. The viewer lists every
file in a kept zip and shows any one of them in the page: PDFs in the browser's own viewer,
Markdown and Word documents rendered, text, CSV and JSON as text, images as images. Anything else
is a download.

THE USPTO FILE IS READ THROUGH HERE BECAUSE THE USPTO'S OWN PAGES STOPPED SHOWING IT. Measured
2026-09-25: patentcenter.uspto.gov/applications/<n> loads its frame, then every one of its own
public data calls answers 405 to a visitor who is not signed in, and it renders an empty page. The
Open Data Portal's web view has asked for a USPTO.gov login since 2026-06-18. The Open Data Portal
API still answers this app's key, so the file wrapper is listed and each document fetched here.
A document on a file wrapper never changes once it is there, so each PDF is fetched once and kept.

WHAT A PREVIEW MAY DO. A zip can hold HTML, and a rendered Markdown file is HTML we built from
somebody else's text. Both are served under a CSP `sandbox` with no scripts and no same-origin
access, so a file in a package can never act as the signed-in reader on this app. PDFs are the
exception: Chrome refuses to run its PDF viewer in a sandboxed document, and a PDF is not a page.
"""
from __future__ import annotations

import csv
import html
import io
import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from flask import Response

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
USPTO_CACHE = Path(os.environ.get("DOCKET_USPTO_CACHE",
                                  str(_HERE.parent / "data" / "observations" / "uspto-files")))
#  One member of a zip, or one USPTO document, larger than this is offered as a download only.
MAX_PREVIEW_BYTES = int(os.environ.get("DOCKET_MAX_PREVIEW_BYTES", str(80 * 1024 * 1024)))
#  Rows of a CSV rendered as a table before the rest is cut, and characters of text shown.
MAX_CSV_ROWS = 3000
MAX_TEXT_CHARS = 2_000_000
LIST_TTL = int(os.environ.get("DOCKET_USPTO_LIST_TTL", "1800"))

KIND_BY_EXT = {
    "pdf": "pdf",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image",
    "svg": "image", "bmp": "image", "tif": "download", "tiff": "download",
    "md": "markdown", "markdown": "markdown",
    "docx": "docx",
    "txt": "text", "log": "text", "py": "text", "css": "text", "js": "text", "xml": "text",
    "yaml": "text", "yml": "text", "ini": "text", "cfg": "text", "tex": "text", "rtf": "text",
    "csv": "csv", "tsv": "csv",
    "json": "json",
    "html": "html", "htm": "html",
}
#  What the file list says a kind is, so a reader knows before clicking.
KIND_LABEL = {"pdf": "PDF", "image": "Image", "markdown": "Markdown", "docx": "Word",
              "text": "Text", "csv": "Table", "json": "JSON", "html": "HTML", "download": "File"}

#  A preview document may style itself and show embedded images. Nothing else: no script, no
#  network, no form, no navigation of the page around it.
SANDBOX_CSP = ("sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
               "frame-ancestors 'self'")
PDF_CSP = "frame-ancestors 'self'"


def kind_of(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return KIND_BY_EXT.get(ext, "download")


# ---------------------------------------------------------------------------------------------
# zips
# ---------------------------------------------------------------------------------------------

def _skip(name):
    base = name.rsplit("/", 1)[-1]
    return (name.endswith("/") or name.startswith("__MACOSX/") or base in (".DS_Store", "Thumbs.db")
            or base.startswith("._"))


def list_zip(path):
    """Every file in a zip, in the zip's own order. -> [{name, size, kind, label}]"""
    out = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if _skip(info.filename):
                continue
            kind = kind_of(info.filename)
            if kind != "download" and info.file_size > MAX_PREVIEW_BYTES:
                kind = "download"
            out.append({"name": info.filename, "size": info.file_size, "kind": kind,
                        "label": KIND_LABEL.get(kind, "File")})
    #  The package's own documents first, then its folders: a build ships its working files in a
    #  subfolder, and the viewer opens on the first file in this list.
    out.sort(key=lambda f: ("/" in f["name"], f["name"].lower()))
    return out


def read_member(path, member):
    """One file out of a zip, by its exact name. None when there is no such member.

    The name is looked up in the zip's own list, never joined to a path, so "../" in a request
    reaches nothing.
    """
    with zipfile.ZipFile(path) as zf:
        try:
            info = zf.getinfo(member)
        except KeyError:
            return None
        if info.is_dir() or info.file_size > MAX_PREVIEW_BYTES * 2:
            return None
        return zf.read(info)


# ---------------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------------

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>
:root{{color-scheme:light dark;--ink:#1d2330;--muted:#5d6678;--line:#d9dde5;--s1:#f6f7f9}}
@media (prefers-color-scheme:dark){{:root{{--ink:#e6e8ee;--muted:#a1a8b8;--line:#3a4050;--s1:#23262f}}
body{{background:#181a20}}}}
body{{margin:0;padding:22px 26px 40px;font:14.5px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:#fff}}
main{{max-width:60rem}}
h1,h2,h3,h4,h5,h6{{line-height:1.3;margin:1.3em 0 .5em}} h1{{font-size:1.5em}} h2{{font-size:1.25em}}
h3{{font-size:1.08em}} p{{margin:.55em 0}} ul,ol{{padding-left:1.5em}} li{{margin:.15em 0}}
hr{{border:0;border-top:1px solid var(--line);margin:1.4em 0}}
blockquote{{margin:.8em 0;padding:.1em 1em;border-left:3px solid var(--line);color:var(--muted)}}
code{{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--s1);
padding:.05em .3em;border-radius:3px}}
pre{{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--s1);
padding:12px 14px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;word-break:break-word}}
pre code{{background:none;padding:0}}
table{{border-collapse:collapse;margin:.8em 0;font-size:13px}}
th,td{{border:1px solid var(--line);padding:5px 8px;vertical-align:top;text-align:left}}
th{{background:var(--s1)}} .note{{color:var(--muted);font-size:12.5px}}
.u{{text-decoration:underline}}
</style></head><body><main>{body}</main></body></html>"""


def page(title, body):
    return _PAGE.format(title=html.escape(title or ""), body=body)


def _inline(text):
    """Inline Markdown on text that is ALREADY escaped: code, bold, italic, links."""
    codes = []

    def keep(m):
        codes.append("<code>%s</code>" % m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)
    text = re.sub(r"`([^`]+)`", keep, text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[image: \1]", text)
    text = re.sub(r"\[([^\]]+)\]\(((?:https?://|mailto:|#)[^)\s]*)\)",
                  lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>' % (m.group(2), m.group(1)),
                  text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: "<b>%s</b>" % (m.group(1) or m.group(2)), text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])",
                  lambda m: "<i>%s</i>" % (m.group(1) or m.group(2)), text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def markdown_html(src):
    """Markdown as the packages write it: headings, lists, tables, code, quotes, rules.

    Escaped before anything is interpreted, so no tag in the source survives as a tag.
    """
    lines = html.escape(src.replace("\r\n", "\n").replace("\r", "\n"), quote=False).split("\n")
    out, i, para = [], 0, []

    def flush():
        if para:
            out.append("<p>%s</p>" % _inline(" ".join(s.strip() for s in para)))
            para.clear()
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            flush()
            fence, body = s[:3], []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            out.append("<pre><code>%s</code></pre>" % "\n".join(body))
            i += 1
            continue
        if not s:
            flush()
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*?)\s*#*$", s)
        if m:
            flush()
            n = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (n, _inline(m.group(2)), n))
            i += 1
            continue
        if re.match(r"^(\*\s*){3,}$|^(-\s*){3,}$|^(_\s*){3,}$", s):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if s.startswith("&gt;"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith("&gt;"):
                quote.append(re.sub(r"^\s*&gt;\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % markdown_html_escaped(quote))
            continue
        if "|" in s and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$",
                                                          lines[i + 1]):
            flush()
            head = _cells(s)
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            t = ["<table><thead><tr>%s</tr></thead><tbody>" % "".join("<th>%s</th>" % _inline(c) for c in head)]
            for r in rows:
                t.append("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in r))
            t.append("</tbody></table>")
            out.append("".join(t))
            continue
        if re.match(r"^\s*([-*+]|\d{1,3}[.)])\s+", line):
            flush()
            out.append(_list(lines, i))
            while i < len(lines) and (re.match(r"^\s*([-*+]|\d{1,3}[.)])\s+", lines[i])
                                      or (lines[i].startswith("  ") and lines[i].strip())):
                i += 1
            continue
        para.append(line)
        i += 1
    flush()
    return "\n".join(out)


def markdown_html_escaped(lines):
    """A nested block (a quote) whose lines are already escaped."""
    return markdown_html(html.unescape("\n".join(lines)))


def _list(lines, i):
    """A run of list items from line i, nested by indentation."""
    items = []
    while i < len(lines):
        m = re.match(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$", lines[i])
        if m:
            items.append([len(m.group(1).replace("\t", "    ")), m.group(2)[0].isdigit(), m.group(3)])
        elif lines[i].startswith("  ") and lines[i].strip() and items:
            items[-1][2] += " " + lines[i].strip()
        else:
            break
        i += 1
    out, stack = [], []
    for indent, ordered, text in items:
        while stack and indent < stack[-1][0]:
            out.append("</li></%s>" % stack.pop()[1])
        if not stack or indent > stack[-1][0]:
            tag = "ol" if ordered else "ul"
            stack.append((indent, tag))
            out.append("<%s><li>%s" % (tag, _inline(text)))
        else:
            out.append("</li><li>%s" % _inline(text))
    while stack:
        out.append("</li></%s>" % stack.pop()[1])
    return "".join(out)


def docx_html(data):
    """A Word document's text, headings, lists and tables, in document order."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = docx.Document(io.BytesIO(data))
    out, in_list = [], False

    def runs(p):
        parts = []
        for r in p.runs:
            t = html.escape(r.text or "")
            if not t:
                continue
            if r.bold:
                t = "<b>%s</b>" % t
            if r.italic:
                t = "<i>%s</i>" % t
            if r.underline:
                t = '<span class="u">%s</span>' % t
            parts.append(t)
        return "".join(parts) or html.escape(p.text or "")

    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            style = (p.style.name if p.style is not None else "") or ""
            text = runs(p)
            is_item = style.lower().startswith("list") or child.find(
                ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr") is not None
            if is_item and text.strip():
                if not in_list:
                    out.append("<ul>")
                    in_list = True
                out.append("<li>%s</li>" % text)
                continue
            if in_list:
                out.append("</ul>")
                in_list = False
            if not text.strip():
                continue
            m = re.match(r"heading\s*(\d)", style, re.I)
            if m:
                n = min(max(int(m.group(1)), 1), 6)
                out.append("<h%d>%s</h%d>" % (n, text, n))
            elif style.lower() == "title":
                out.append("<h1>%s</h1>" % text)
            else:
                out.append("<p>%s</p>" % text)
        elif tag == "tbl":
            if in_list:
                out.append("</ul>")
                in_list = False
            t = Table(child, doc)
            rows = []
            for row in t.rows:
                cells, seen = [], set()
                for cell in row.cells:
                    if id(cell._tc) in seen:        # a merged cell repeats itself
                        continue
                    seen.add(id(cell._tc))
                    cells.append("<td>%s</td>" % "<br>".join(html.escape(p.text) for p in cell.paragraphs
                                                             if p.text.strip()))
                rows.append("<tr>%s</tr>" % "".join(cells))
            out.append("<table>%s</table>" % "".join(rows))
    if in_list:
        out.append("</ul>")
    return "\n".join(out) or '<p class="note">This document has no text.</p>'


def _text(data):
    for enc in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def render(data, name):
    """A preview document for one file. -> (html, None) or (None, reason)."""
    kind = kind_of(name)
    title = name.rsplit("/", 1)[-1]
    if kind == "markdown":
        return page(title, markdown_html(_text(data)[:MAX_TEXT_CHARS])), None
    if kind == "docx":
        try:
            return page(title, docx_html(data)), None
        except Exception as exc:
            return None, "This Word file could not be read (%s)." % type(exc).__name__
    if kind == "csv":
        text = _text(data)
        delim = "\t" if name.lower().endswith(".tsv") else ","
        rows = []
        for n, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
            if n >= MAX_CSV_ROWS:
                break
            tag = "th" if n == 0 else "td"
            rows.append("<tr>%s</tr>" % "".join("<%s>%s</%s>" % (tag, html.escape(c), tag) for c in row))
        more = text.count("\n") - MAX_CSV_ROWS
        note = '<p class="note">First %d rows of about %d.</p>' % (MAX_CSV_ROWS, MAX_CSV_ROWS + more) \
            if more > 0 else ""
        return page(title, "<table>%s</table>%s" % ("".join(rows), note)), None
    if kind == "json":
        text = _text(data)
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
        return page(title, "<pre>%s</pre>" % html.escape(text[:MAX_TEXT_CHARS])), None
    if kind == "text":
        text = _text(data)
        cut = '<p class="note">Cut at %d characters.</p>' % MAX_TEXT_CHARS if len(text) > MAX_TEXT_CHARS else ""
        return page(title, "<pre>%s</pre>%s" % (html.escape(text[:MAX_TEXT_CHARS]), cut)), None
    return None, "No preview for this kind of file."


def respond(data, name, download=False):
    """The HTTP answer for one file: inline in the viewer, or as a download."""
    base = name.rsplit("/", 1)[-1]
    kind = kind_of(name)
    mime = mimetypes.guess_type(base)[0] or "application/octet-stream"
    quoted = base.replace('"', "").replace("\\", "")
    if download or kind == "download":
        resp = Response(data, mimetype=mime)
        resp.headers["Content-Disposition"] = "attachment; filename=\"%s\"" % quoted.encode(
            "ascii", "replace").decode()
        resp.headers["Content-Security-Policy"] = SANDBOX_CSP
        return resp
    if kind == "pdf":
        resp = Response(data, mimetype="application/pdf")
        resp.headers["Content-Disposition"] = "inline; filename=\"%s\"" % quoted.encode(
            "ascii", "replace").decode()
        resp.headers["Content-Security-Policy"] = PDF_CSP
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp
    if kind in ("image", "html"):
        resp = Response(data, mimetype=mime if kind == "image" else "text/html")
        resp.headers["Content-Security-Policy"] = SANDBOX_CSP
        return resp
    doc, why = render(data, name)
    if doc is None:
        doc = page(base, '<p class="note">%s</p>' % html.escape(why))
    resp = Response(doc, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = SANDBOX_CSP
    return resp


# ---------------------------------------------------------------------------------------------
# the USPTO file wrapper, through the Open Data Portal
# ---------------------------------------------------------------------------------------------

_LISTS = {}
_LISTS_LOCK = threading.Lock()
_DOC_OK = re.compile(r"^[A-Za-z0-9._-]{4,80}$")


def app_number(value):
    """A US application number as the office keys it: eight digits, or None."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if 6 <= len(digits) <= 9 else None


def _doc_url(doc):
    """The one PDF of the whole document, which the office numbers pages for."""
    opts = [o for o in (doc.get("downloadOptionBag") or []) if str(o.get("mimeTypeIdentifier")).upper() == "PDF"]
    whole = [o for o in opts if o.get("pageTotalQuantity")]
    pick = (whole or opts or [None])[0]
    return (pick or {}).get("downloadUrl") or "", (pick or {}).get("pageTotalQuantity")


def uspto_documents(app, odp=None, fresh=False):
    """Every document on one US file wrapper, newest first. -> [{id, code, description, date,
    direction, pages, has_pdf}]. Raises observation_refresh.OdpUnavailable when the office could
    not be asked, which the page must say rather than show an empty file."""
    if odp is None:
        import observation_refresh
        odp = observation_refresh._odp
    now = time.time()
    with _LISTS_LOCK:
        hit = _LISTS.get(app)
        if hit and not fresh and now - hit[0] < LIST_TTL:
            return [dict(d) for d in hit[1]]
    data = odp("patent/applications/%s/documents" % app) or {}
    docs = []
    for d in data.get("documentBag") or []:
        url, pages = _doc_url(d)
        docs.append({"id": str(d.get("documentIdentifier") or ""),
                     "code": str(d.get("documentCode") or ""),
                     "description": str(d.get("documentCodeDescriptionText") or ""),
                     "date": str(d.get("officialDate") or "")[:10],
                     "direction": str(d.get("directionCategory") or "").lower(),
                     "pages": pages, "has_pdf": bool(url), "_url": url})
    docs.sort(key=lambda d: d["date"], reverse=True)
    with _LISTS_LOCK:
        _LISTS[app] = (now, docs)
    return [dict(d) for d in docs]


def public_list(docs):
    return [{k: v for k, v in d.items() if not k.startswith("_")} for d in docs]


def uspto_pdf(app, doc_id, cache=None, odp=None, fetch=None):
    """One file-wrapper document as a PDF, from the disk cache or the office. -> bytes or None."""
    if not _DOC_OK.match(doc_id or ""):
        return None
    cache = Path(cache) if cache else USPTO_CACHE
    path = cache / app / ("%s.pdf" % doc_id)
    if path.is_file():
        return path.read_bytes()
    docs = uspto_documents(app, odp=odp)
    doc = next((d for d in docs if d["id"] == doc_id), None)
    if not doc or not doc["_url"]:
        return None
    body = (fetch or _fetch_pdf)(doc["_url"])
    if not body or not body.startswith(b"%PDF"):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pdf.tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    return body


def _fetch_pdf(url):
    import observation_refresh
    key = os.environ.get("USPTO_ODP_KEY", "") or os.environ.get("ODP_API_KEY", "")
    if not key:
        raise observation_refresh.OdpUnavailable("no USPTO_ODP_KEY in the environment")
    last = ""
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"X-API-KEY": key, "Accept": "application/pdf",
                                                   "User-Agent": "rotem-docket/1"})
        try:
            with observation_refresh._ODP_GATE:
                with urllib.request.urlopen(req, timeout=120) as fh:
                    return fh.read(MAX_PREVIEW_BYTES + 1)
        except urllib.error.HTTPError as exc:
            last = "HTTP %s" % exc.code
            if exc.code == 404:
                return None
            if exc.code not in observation_refresh.ODP_RETRY_CODES:
                break
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, str(exc)[:60])
        time.sleep(1.5 * (attempt + 1))
    raise observation_refresh.OdpUnavailable(last or "no answer")
