"""The docket's viewer: what a package zip holds, each file rendered safely, and the USPTO file.

No network and no database: zips are built in temp directories, the Open Data Portal is a stub.
"""
import io
import json
import zipfile

import pytest

import docket_files as df


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return path


# ---------------------------------------------------------------------------------------------
# zips
# ---------------------------------------------------------------------------------------------

def test_a_zip_lists_its_files_with_a_kind_and_skips_the_debris(tmp_path):
    z = _zip(tmp_path / "p.zip", {"00_QA_REPORT.md": "# QA", "docs/10_Statement.pdf": b"%PDF-1.4",
                                  "__MACOSX/._x": b"", "docs/": b"", "fonts/a.ttf": b"\0",
                                  "list.docx": b"PK", "refs.csv": "a,b"})
    got = {f["name"]: f["kind"] for f in df.list_zip(z)}
    assert got == {"00_QA_REPORT.md": "markdown", "docs/10_Statement.pdf": "pdf",
                   "fonts/a.ttf": "download", "list.docx": "docx", "refs.csv": "csv"}


def test_a_member_is_found_by_its_exact_name_only(tmp_path):
    z = _zip(tmp_path / "p.zip", {"a/b.txt": "hello"})
    assert df.read_member(z, "a/b.txt") == b"hello"
    assert df.read_member(z, "../a/b.txt") is None
    assert df.read_member(z, "a/../a/b.txt") is None
    assert df.read_member(z, "b.txt") is None


# ---------------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------------

def test_markdown_renders_and_no_tag_in_the_source_survives():
    out = df.markdown_html("# Title\n\nSome **bold** and `code`.\n\n<script>alert(1)</script>\n\n"
                           "| a | b |\n|---|---|\n| 1 | 2 |\n\n- one\n- two\n  - nested\n\n1. first\n")
    assert "<h1>Title</h1>" in out and "<b>bold</b>" in out and "<code>code</code>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr>" in out
    assert "<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>" in out
    assert "<ol><li>first</li></ol>" in out


def test_a_markdown_link_may_only_go_to_the_web():
    out = df.markdown_html("[ok](https://example.com) [bad](javascript:alert(1))")
    assert 'href="https://example.com"' in out
    assert "javascript:" not in out.split("[bad]")[0] and 'href="javascript' not in out


def test_a_word_document_keeps_its_headings_lists_and_tables():
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Concise description of relevance", level=1)
    p = d.add_paragraph("Reference ")
    p.add_run("D1").bold = True
    d.add_paragraph("first point", style="List Bullet")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Claim 1", "<b>shown</b>"
    buf = io.BytesIO()
    d.save(buf)
    out = df.docx_html(buf.getvalue()).replace("\n", "")
    assert "<h1>Concise description of relevance</h1>" in out
    assert "Reference <b>D1</b>" in out
    assert "<ul><li>first point</li></ul>" in out
    assert "<td>Claim 1</td><td>&lt;b&gt;shown&lt;/b&gt;</td>" in out


def test_every_preview_but_a_pdf_is_sandboxed():
    for name, body in (("a.md", b"# x"), ("a.html", b"<script>x</script>"), ("a.txt", b"x"),
                       ("a.json", b'{"a":1}'), ("a.csv", b"a,b"), ("a.svg", b"<svg/>")):
        r = df.respond(body, name)
        assert r.headers["Content-Security-Policy"].startswith("sandbox"), name
        assert "script-src" not in r.headers["Content-Security-Policy"]
    r = df.respond(b"%PDF-1.4", "docs/a.pdf")
    assert r.mimetype == "application/pdf"
    assert r.headers["Content-Security-Policy"] == "frame-ancestors 'self'"
    assert r.headers["Content-Disposition"].startswith("inline")


def test_a_download_is_an_attachment_and_json_is_pretty():
    r = df.respond(b"%PDF-1.4", "docs/a.pdf", download=True)
    assert r.headers["Content-Disposition"] == 'attachment; filename="a.pdf"'
    r = df.respond(b'{"a":{"b":1}}', "x.json")
    assert '&quot;b&quot;: 1' in r.get_data(as_text=True)


# ---------------------------------------------------------------------------------------------
# the USPTO file
# ---------------------------------------------------------------------------------------------

DOCS = {"documentBag": [
    {"documentIdentifier": "OLD1", "documentCode": "CTNF", "documentCodeDescriptionText": "Non-Final Rejection",
     "officialDate": "2026-01-05T00:00:00.000-0500", "directionCategory": "OUTGOING",
     "downloadOptionBag": [{"mimeTypeIdentifier": "PDF", "downloadUrl": "https://x/OLD1.pdf", "pageTotalQuantity": 9}]},
    {"documentIdentifier": "NEW2", "documentCode": "3P.RELEVANCE",
     "documentCodeDescriptionText": "Concise Description of Relevance",
     "officialDate": "2026-08-02T22:33:18.000-0400", "directionCategory": "INCOMING",
     "downloadOptionBag": [{"mimeTypeIdentifier": "PDF", "downloadUrl": "https://x/NEW2/files/a.pdf"}]},
    {"documentIdentifier": "NOPDF", "documentCode": "SRNT", "documentCodeDescriptionText": "Search notes",
     "officialDate": "2026-03-01T00:00:00.000-0500", "directionCategory": "INTERNAL", "downloadOptionBag": []},
]}


def test_the_file_wrapper_is_listed_newest_first(monkeypatch):
    monkeypatch.setattr(df, "_LISTS", {})
    docs = df.uspto_documents("19315746", odp=lambda path: DOCS)
    assert [d["id"] for d in docs] == ["NEW2", "NOPDF", "OLD1"]
    assert docs[2]["pages"] == 9 and docs[1]["has_pdf"] is False
    assert all("_url" not in d for d in df.public_list(docs))


def test_a_document_is_fetched_once_and_only_when_it_is_a_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(df, "_LISTS", {})
    fetched = []

    def fetch(url):
        fetched.append(url)
        return b"%PDF-1.7 body" if "OLD1" in url else b"<html>login</html>"
    odp = lambda path: DOCS  # noqa: E731
    assert df.uspto_pdf("19315746", "OLD1", cache=tmp_path, odp=odp, fetch=fetch).startswith(b"%PDF")
    assert df.uspto_pdf("19315746", "OLD1", cache=tmp_path, odp=odp, fetch=fetch).startswith(b"%PDF")
    assert fetched == ["https://x/OLD1.pdf"]
    assert df.uspto_pdf("19315746", "NEW2", cache=tmp_path, odp=odp, fetch=fetch) is None
    assert df.uspto_pdf("19315746", "NOPDF", cache=tmp_path, odp=odp, fetch=fetch) is None
    assert df.uspto_pdf("19315746", "../../etc", cache=tmp_path, odp=odp, fetch=fetch) is None


def test_an_application_number_is_eight_digits_whatever_its_spelling():
    assert df.app_number("19/315,746") == "19315746"
    assert df.app_number("US 19-315746") == "19315746"
    assert df.app_number("") is None and df.app_number("12") is None


# ---------------------------------------------------------------------------------------------
# the daily check's record, as the page reads it
# ---------------------------------------------------------------------------------------------

def test_the_daily_check_writes_what_the_page_shows(tmp_path, monkeypatch):
    import actions_daily as ad
    monkeypatch.setattr(ad, "LOG_DIR", tmp_path)
    monkeypatch.setattr(ad, "all_targets", lambda user=None: [(4, {"id": 1, "name": "Schmalz"}),
                                                              (4, {"id": 2, "name": "Piab"})])

    def check(uid, target, kind):
        if target["id"] == 2 and kind == "trademark":
            raise RuntimeError("TMview down")
        return {"user_id": uid, "target_id": target["id"], "target": target["name"], "kind": kind,
                "cases": 10, "updated": 10, "new": 1 if kind == "patent" else 0,
                "errors": ["x: HTTP 500"] if target["id"] == 1 and kind == "patent" else [],
                "changes": ["New on the docket: US2026..."], "seconds": 1.0}
    monkeypatch.setattr(ad, "check", check)
    s = ad.run(iptorch=False)
    assert s["new"] == 2 and s["read_errors"] == 1 and not s["ok"]
    assert s["failures"][0]["target"] == "Piab" and "TMview" in s["failures"][0]["error"]
    last = ad.latest(tmp_path)
    assert last["new"] == 2 and last["failures"] == 1 and last["ok"] is False
    assert json.loads((tmp_path / ("%s.json" % s["started"][:10])).read_text())["new"] == 2


def test_no_daily_check_yet_is_said_as_such(tmp_path):
    import actions_daily as ad
    assert ad.latest(tmp_path) is None
