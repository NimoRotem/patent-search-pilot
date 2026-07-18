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
    assert "Prior-Art Search Report" in text
    assert "claim chart" in text.lower()
    assert SEL[0] in text, "a selected reference must appear"


def test_docx_is_valid_and_complete(model, tmp_path):
    import export_docx
    out = tmp_path / "r.docx"
    export_docx.render(model, out)
    from docx import Document
    doc = Document(str(out))
    paras = [p.text for p in doc.paragraphs]
    joined = "\n".join(paras)
    assert "Prior-Art Search Report" in joined
    assert any("claim chart" in p.lower() for p in paras)
    assert len(doc.tables) >= 2, "claim chart + appendix tables"
    assert len(doc.inline_shapes) >= 1, "at least one embedded drawing"
    # a selected reference appears somewhere (heading or appendix table)
    all_text = joined + "\n" + "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert any(s in all_text for s in SEL)
