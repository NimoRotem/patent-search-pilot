"""Export tests: /export produces a valid PDF and a valid DOCX with the expected content.
Uses the cached grabo report + locally-cached drawings (no paid APIs)."""
import os, tempfile
import pytest

SEL = ["US-11207792-B2", "US-9457478-B2", "US-4557659-A"]   # cached refs with local drawings


@pytest.fixture()
def model():
    import export_data
    return export_data.assemble("grabo_gripper_novelty", SEL)


def test_export_model_shape(model):
    assert model["mode"] == "novelty"
    assert len(model["references"]) == 3
    assert model["claim_chart"]["columns"], "claim chart must have reference columns"
    assert model["elements"], "elements present"
    # at least one selected reference has a locally embedded drawing + a quoted passage
    assert any(r["drawing_path"] and os.path.exists(r["drawing_path"]) for r in model["references"])
    assert any(r["quoted"] and r["quoted"]["text"] for r in model["references"])


def test_pdf_is_valid_and_complete(model, tmp_path):
    import export_pdf
    out = tmp_path / "r.pdf"
    export_pdf.render(model, out)
    data = out.read_bytes()
    assert data[:5] == b"%PDF-", "must be a real PDF"
    from pypdf import PdfReader
    reader = PdfReader(str(out))
    assert len(reader.pages) >= 5, f"expected a multi-page report, got {len(reader.pages)}"
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    #  ACCEPTANCE TEST for the export-disclosure fix. An exported document has to stand alone, so
    #  each of these was a real defect: the doc was titled like a search opinion, the grid was
    #  titled "claim chart", cells were shaded by retrieval score, and there was no scope
    #  disclosure of any kind.
    assert "Prior-Art Search Report" not in text, "the old authoritative title must be gone"
    assert "Prior-art retrieval report" in text
    assert "Element" in text and "retrieval map" in text
    assert "drafting aid" in text.lower()
    assert "not a verified claim chart" in text.lower()
    assert "Absence of results is not evidence of absence" in text
    assert "Measured recall" in text
    assert "CPC classes" in text
    assert SEL[0] in text, "a selected reference must appear"


def test_docx_is_valid_and_complete(model, tmp_path):
    import export_docx
    out = tmp_path / "r.docx"
    export_docx.render(model, out)
    from docx import Document
    doc = Document(str(out))
    paras = [p.text for p in doc.paragraphs]
    joined = "\n".join(paras)
    assert "Prior-Art Search Report" not in joined, "the old authoritative title must be gone"
    assert "Prior-art retrieval report" in joined
    assert any("retrieval map" in p.lower() for p in paras)
    assert any("drafting aid" in p.lower() for p in paras)
    assert any("Absence of results is not evidence of absence" in p for p in paras)
    assert any("Measured recall" in p for p in paras)
    assert len(doc.tables) >= 2, "retrieval map + appendix tables"
    assert len(doc.inline_shapes) >= 1, "at least one embedded drawing"
    # a selected reference appears somewhere (heading or appendix table)
    all_text = joined + "\n" + "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert any(s in all_text for s in SEL)


def test_export_chart_cells_carry_a_verification_verdict(model):
    """The root cause of the export-accuracy defect, pinned.

    export_data.assemble() builds its own view via webview.build_view(), and build_view does NOT
    verify anything -- verification was applied only to the cached view the browser reads. So every
    exported cell arrived with no `verify` key and the exporters shaded it green by retrieval
    score. If this assertion ever fails again, the exports have silently gone back to publishing
    unverified cells as coverage.
    """
    covered = [c for row in model["claim_chart"]["rows"] for c in row["cells"] if c.get("covered")]
    assert covered, "the gold report must have covered cells to check"
    missing = [c for c in covered if not c.get("verify")]
    assert not missing, f"{len(missing)} covered cells reached the export with no verdict"
    assert model["claim_chart"].get("verification"), "legend counts must reach the exporters"


def test_exports_never_shade_by_retrieval_score():
    """Cell colour must encode the verification verdict, never the fused retrieval score.

    The old behaviour made the most confidently-retrieved cell the greenest thing on the page --
    including cells the verifier had judged `unrelated`.
    """
    import inspect
    import export_docx, export_pdf

    def code_only(fn):
        """Strip comments: both functions EXPLAIN in a comment why they no longer use `intensity`,
        and that explanation must not itself trip the check."""
        out = []
        for line in inspect.getsource(fn).splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            out.append(line.split("#", 1)[0])
        return "\n".join(out)

    assert not hasattr(export_docx, "_green_shade"), "score-proportional green shading is back"
    assert "intensity" not in code_only(export_pdf._claim_chart_table), \
        "PDF chart is shading by retrieval score again"
    assert "intensity" not in code_only(export_docx._claim_chart), \
        "DOCX chart is shading by retrieval score again"


def test_disclosure_reports_the_independent_finding_not_the_self_grade():
    """'0 of 5 rejected' was the verifier grading its own work and read as reassurance the
    evidence does not support. The disclosure must lead with the independent re-read."""
    import corpus_facts, disclosure
    f = corpus_facts.facts()
    assert f["chart_indep_checked"] == 5
    assert f["chart_indep_overclaim"] == 2
    assert f["chart_indep_pct"] == 40
    warn = disclosure.chart_warning(f)
    assert "independent" in warn.lower()
    assert "overclaim" in warn.lower()
    assert "lower bound" in warn.lower(), "the shared-model-family caveat must survive"
