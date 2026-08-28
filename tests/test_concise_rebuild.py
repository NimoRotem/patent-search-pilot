"""A rebuild replaces the package; it does not merge with the last one.

Measured 2026-08-20 on adhoc-8dcf2436929a: a build at 19:06 wrote Doc7_GB2200615A and
Doc8_US8616602B2, a build at 19:15 wrote Doc7_US1675003A and Doc8_US6109885A, and all four
survived. Twelve files for a ten-document package, two of them called Document 7, and the zip
would have carried both into an envelope.

The packet grew past the descriptions on 2026-08-23: it now also holds the document list, the
statements, the audit, a legible copy of each non-U.S. item and a translation of each non-English
one. Every one of those is per-build too. A copy left behind for an item the new build does not
list is worse than a duplicate description, because it would be FILED as part of a submission it
does not belong to. So these tests assert the property, "each of these is cleared before anything
is written", rather than the exact shape of the loop that does it.
"""
import re

import webapp

#  Named as literals. Deriving them from the source under test would make the assertion vacuous.
MUST_BE_CLEARED = (
    "ConciseDescription_*",     # the descriptions themselves
    "*.model.json",             # what the preview and the re-render read
    "00_*",                     # the audit and the read-me
    "01_*",                     # the document list and statements
    "40_Copy_*",                # 1.290(d)(3) copies
    "50_Translation_*",         # 1.290(d)(4) translations
    "MANIFEST.csv",
)


def _source():
    return open(webapp.__file__.replace(".pyc", ".py")).read()


def _clearing_block():
    src = _source()
    m = re.search(r"out = CONCISE_DIR / slug\n(.*?)for k, d in enumerate\(docs, 1\)", src, re.S)
    assert m, "the concise build body moved"
    return m.group(1)


def test_a_build_clears_every_kind_of_artefact_the_last_one_left():
    head = _clearing_block()
    missing = [p for p in MUST_BE_CLEARED if p not in head]
    assert not missing, (
        "a rebuild leaves these behind, so the packet can carry a paper belonging to a document "
        "this submission does not list: %s" % ", ".join(missing))
    assert "unlink()" in head


def test_it_clears_before_it_writes():
    """After the write it would delete what it just made; the order is the whole fix."""
    src = _source()
    i_clear = src.index('"ConciseDescription_*"')
    i_write = src.index("concise_render.filename(d, fmt)")
    assert i_clear < i_write


def test_the_model_files_go_too():
    """`.model.json` is what the preview and the re-render read. A stale one renders a document
    that is not in this package."""
    assert '"*.model.json"' in _clearing_block()


def test_the_working_files_never_reach_the_zip(tmp_path, monkeypatch):
    """The archive is what gets filed. `.md` and `.model.json` are working files: a model.json in
    an envelope to the Office is a leak of the raw cells behind the paper.

    Asserted on the archive this route actually produces, not on the text of the loop. It used to
    read the source, and the source moved: the filter was lifted out of the `with` block so the
    entries could be sorted on the name they will carry, and a passing guard turned red for a
    change that did not touch what it was guarding.
    """
    import io
    import zipfile

    slug = "adhoc-zipfilter"
    monkeypatch.setattr(webapp, "CONCISE_DIR", tmp_path)
    monkeypatch.setattr(webapp, "_can_access_report", lambda s: True)
    webapp.app.config["TESTING"] = True
    d = tmp_path / slug
    d.mkdir(parents=True)
    for name in ("ConciseDescription_Doc1_USA.pdf", "ConciseDescription_Doc1_USA.docx",
                 "ConciseDescription_Doc1_USA.md", "ConciseDescription_Doc1_USA.model.json",
                 "00_AUDIT.pdf", "MANIFEST.csv", "READ_ME_FIRST.txt"):
        (d / name).write_bytes(b"x")

    names = zipfile.ZipFile(io.BytesIO(
        webapp.app.test_client().get("/report/%s/concise.zip" % slug).data)).namelist()
    assert not [n for n in names if n.endswith((".md", ".model.json"))], names
    #  and it did not throw the filing artefacts out with them
    assert len(names) == 5, names


def test_the_rule_about_what_belongs_in_the_archive_is_one_function():
    """Two overlapping filters meant neither could be tested on its own: removing either one
    changed nothing, because the other still caught the same files. One rule, asked directly."""
    assert webapp.zip_member_name("ConciseDescription_Doc1_USA.md") is None
    assert webapp.zip_member_name("ConciseDescription_Doc1_USA.model.json") is None
    assert webapp.zip_member_name("notes.json") is None
    for suffix in webapp.ZIP_FILING_SUFFIXES:
        assert webapp.zip_member_name("00_AUDIT" + suffix) == "00_AUDIT" + suffix
    #  and the descriptions are renamed so the archive reads in filing order
    assert (webapp.zip_member_name("ConciseDescription_Doc1_USA.pdf")
            == "10_ConciseDescription_Doc01_USA.pdf")
    assert (webapp.zip_member_name("ConciseDescription_Doc10_USJ.pdf")
            == "10_ConciseDescription_Doc10_USJ.pdf")


def test_the_model_file_is_refused_by_name_and_not_only_by_its_extension(monkeypatch):
    """The suffix list already keeps `.json` out, so the name rule looks redundant and would be
    the natural thing to delete. It is the one that survives somebody adding `.json` to the list,
    and a model.json in an envelope to the Office is the raw cells behind the paper going with
    it. Asserted with the list widened, because that is the only state where the rule does work.
    """
    monkeypatch.setattr(webapp, "ZIP_FILING_SUFFIXES",
                        webapp.ZIP_FILING_SUFFIXES + (".json",))
    assert webapp.zip_member_name("notes.json") == "notes.json", "the fixture did not take"
    assert webapp.zip_member_name("ConciseDescription_Doc1_USA.model.json") is None
