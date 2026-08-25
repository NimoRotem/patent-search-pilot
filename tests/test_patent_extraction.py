"""Extraction of the search material from an uploaded patent.

These lock in the defects that were measured on real patent PDFs before the layout-aware path
existed, so a regression is caught by the suite rather than by a bad search:

  * a two-column grant whose claims came out spliced together mid-sentence (US-11338449-B2:
    12 shredded claims where the cover page says 20);
  * marginal line numbers landing inside claim text;
  * a CN publication whose claim NUMBERS were deleted as if they were line numbers, and whose
    last claim then swallowed the whole 12,000-character description;
  * a grounding guard that tokenised CJK by runs and therefore rejected every correctly
    transcribed Chinese claim;
  * a search brief condensed from the first pages instead of the whole file.

Hermetic: the model is stubbed by conftest's autouse fixture, and the PDF fixtures are generated
locally with reportlab rather than downloaded.
"""
import io
import json

import pytest

import ingest_input as ii
import patent_doc
import patent_pdf


# ---------------------------------------------------------------------------
# claim splitting
# ---------------------------------------------------------------------------
def test_split_claims_sequential_not_every_number():
    """"the system of claim 10" must not start claim 10 — numbering is walked in sequence."""
    blob = ("1. A system comprising a pump rated at 10. bar and a valve.\n"
            "2. The system of claim 1, wherein the pump is electric.\n"
            "3. The system of claim 2, further comprising a sensor.\n")
    claims = patent_doc.split_claims(blob)
    assert [c["claim_no"] for c in claims] == [1, 2, 3]
    assert "10. bar" in claims[0]["text"]          # the internal "10." stayed inside claim 1


def test_split_claims_internal_reference_does_not_split():
    blob = ("1. A gripper comprising a compliant suction cup and a pump.\n"
            "2. The gripper of claim 1, wherein a spacing of 3. 5 mm is used.\n")
    claims = patent_doc.split_claims(blob)
    assert len(claims) == 2


def test_split_claims_resyncs_over_a_dropped_numeral():
    """OCR drops a numeral: the tail must survive, not be truncated at the gap."""
    blob = ("1. A vacuum lifter comprising a suction cup and a pump.\n"
            "2. The lifter of claim 1, wherein the cup is elastomeric.\n"
            "4. The lifter of claim 1, further comprising an alarm.\n")
    claims = patent_doc.split_claims(blob)
    assert [c["claim_no"] for c in claims] == [1, 2, 4]


def test_split_claims_cjk_punctuation_without_a_following_space():
    blob = "1．一种管理环境中的气味的方法， 其包括步骤。\n2．如权利要求1所述的方法， 其中泵是电动的。\n"
    claims = patent_doc.split_claims(blob)
    assert len(claims) == 2
    assert claims[0]["text"].startswith("一种管理")


def test_split_claims_stops_at_the_description():
    """A CN/EP publication prints claims FIRST; without an end boundary the last claim
    swallowed the entire specification (measured: 11,997 characters)."""
    blob = ("1. A vacuum lifter comprising a suction cup and a pump.\n"
            "2. The lifter of claim 1, wherein the cup is elastomeric.\n"
            "3. The lifter of claim 1, further comprising an alarm.\n"
            "说明书\n" + "描述内容 " * 400)
    claims = patent_doc.split_claims(blob)
    assert len(claims) == 3
    assert len(claims[-1]["text"]) < 200


def test_is_independent():
    assert patent_doc.is_independent("A gripper comprising a suction cup and a pump.")
    assert not patent_doc.is_independent("The gripper of claim 1, further comprising a sensor.")
    assert not patent_doc.is_independent("Vorrichtung nach Anspruch 1, dadurch gekennzeichnet.")
    assert not patent_doc.is_independent("如权利要求1所述的方法， 其中泵是电动的。")


# ---------------------------------------------------------------------------
# document segmentation
# ---------------------------------------------------------------------------
US_STYLE = (
    "Portable Vacuum Lifter\n\n"
    "Abstract\n"
    "A handheld vacuum lifter for glass panels with a compliant sealing lip.\n\n"
    "DETAILED DESCRIPTION\n"
    "The lifter includes a vacuum pump connected to a manifold that distributes negative "
    "pressure across a suction cup, and a pressure sensor that alarms on loss of grip.\n\n"
    "A control unit monitors the vacuum level continuously and warns the operator before the "
    "load can be dropped, which is the problem the invention solves.\n\n"
    "What is claimed is:\n"
    "1. A vacuum lifter comprising a suction cup, a vacuum pump and a pressure sensor.\n"
    "2. The lifter of claim 1, wherein the sealing lip is elastomeric.\n"
    "3. The lifter of claim 1, further comprising an audible alarm.\n"
)

CN_STYLE = (
    "(19) State Intellectual Property Office\n\n"
    "摘要\n"
    "本公开涉及一种管理环境中的气味的方法。\n\n"
    "1．一种管理环境中的气味的方法， 其包括在环境内设置至少一个气味扩散装置， 其中所述至少一个"
    "气味扩散装置包括通信设施， 其使得能够向远程计算机发射信号和从其接收信号； 在所述远程计算机"
    "处接收所述环境的气味参数的至少一个目标值。\n"
    "2．如权利要求1所述的方法， 其中所述操作参数是占空比， 并且所述部件是泵， 通过更改施加到所述"
    "泵的电压对所述泵的功率进行外部控制。\n"
    "3．如权利要求1所述的方法， 其中所述部件是风扇， 并且所述操作参数是风扇速度， 其独立于所述电"
    "压范围和独立于占空比。\n"
    "说明书\n"
    "本发明涉及气味管理系统的各种方面， 装置可工作来分配任何液体， 并且可与传感器通信。\n\n"
    "在这个实施方案中， 主扩散装置与一个或多个从扩散装置和服务器通信， 服务器诸如云服务器。\n"
)


def test_segment_us_layout_claims_last():
    s = patent_doc.segment(US_STYLE)
    assert "vacuum lifter" in s["title"].lower()
    assert "sealing lip" in s["abstract"].lower()
    assert [c["claim_no"] for c in s["claims"]] == [1, 2, 3]
    assert s["claims"][0]["independent"] and not s["claims"][1]["independent"]
    # the claims section is lifted OUT of the body, so it is not also chunked as description
    assert not any("vacuum lifter comprising" in p for p in s["paragraphs"])
    assert any("pressure sensor" in p for p in s["paragraphs"])


def test_segment_cn_layout_claims_first_keeps_the_description():
    """Claims come before the description in CN/EP. Cutting the body at the claims start would
    throw the entire description away — which is the text the brief is condensed from."""
    s = patent_doc.segment(CN_STYLE)
    assert len(s["claims"]) == 3
    assert s["claims"][0]["independent"] and not s["claims"][1]["independent"]
    body = "\n".join(s["paragraphs"])
    assert "云服务器" in body                       # description survived the claims cut
    assert "如权利要求1所述的方法" not in body        # ... and the claims did not stay in it


def test_segment_claims_header_running_into_the_first_claim():
    text = ("A Title\n\nWhat is claimed is: 1. A first claim body with a suction cup.\n"
            "2. The thing of claim 1, wherein the cup is elastomeric.\n")
    s = patent_doc.segment(text)
    assert len(s["claims"]) == 2
    assert s["claims"][0]["text"].startswith("A first claim body")


def test_segment_repairs_ocr_spacing():
    """A USPTO text layer reads "the system , comprising : a pick - up position". Left alone,
    "pick - up" and "pick-up" are different tokens to the embedder."""
    text = ("Title\n\nWhat is claimed is:\n"
            "1. A system , comprising : a pick - up position and a robot - side platform .\n")
    s = patent_doc.segment(text)
    got = s["claims"][0]["text"]
    assert "system, comprising:" in got
    assert "pick-up" in got and "robot-side" in got


def test_segment_never_returns_nothing_for_prose():
    s = patent_doc.segment("Just some prose about a gripper. " * 20)
    assert s["claims"] == [] and s["paragraphs"]


# ---------------------------------------------------------------------------
# the grounding guard
# ---------------------------------------------------------------------------
def test_grounding_accepts_verbatim_rejects_invention():
    source = set(patent_doc._norm_words(US_STYLE))
    assert patent_doc._grounded(
        "A vacuum lifter comprising a suction cup, a vacuum pump and a pressure sensor.", source)
    assert not patent_doc._grounded(
        "A quantum entanglement reactor comprising a superconducting toroid and cryostat.", source)


def test_grounding_tokenises_cjk_per_character():
    """Run-based CJK tokenisation gave zero overlap between two correct readings of the same
    claim and threw away 9 of 12 verbatim Chinese claims."""
    source = set(patent_doc._norm_words(CN_STYLE))
    assert patent_doc._grounded("一种管理环境中的气味的方法，其包括在环境内设置气味扩散装置。", source)
    assert not patent_doc._grounded("一种用于半导体晶圆的真空吸盘搬运机械手臂结构设计。", source)


def test_shredded_flags_a_short_claim_one_only():
    good = [{"text": "x" * 2500}, {"text": "y" * 200}, {"text": "z" * 300}]
    bad = [{"text": "x" * 281}, {"text": "y" * 1364}, {"text": "z" * 900}]   # the measured shape
    assert not patent_doc._shredded(good)      # a long claim 1 is normal, not damage
    assert patent_doc._shredded(bad)


# ---------------------------------------------------------------------------
# the brief — condensed from the WHOLE document
# ---------------------------------------------------------------------------
def test_brief_windows_cover_the_whole_document(monkeypatch):
    seen = []

    def fake(system, user, **k):
        seen.append(user)
        if "SEARCH BRIEF" in system:
            return {"disclosure": "a brief", "title": "T", "keywords": ["k"]}
        return {"facts": ["fact from " + user[:12]]}

    import llm
    monkeypatch.setattr(llm, "chat_json", fake)
    text = "".join("SECTION%02d " % i + "filler " * 900 for i in range(6))
    notes = []
    out = patent_doc.search_brief(text, {"abstract": "a", "claims": []}, notes)
    assert out["disclosure"] == "a brief"
    windows = [u for u in seen if "SECTION" in u and "TECHNICAL FACTS" not in u]
    # every section of the document reached a window, including the LAST one — a head-only
    # condense would never see SECTION05
    joined = " ".join(windows)
    for i in range(6):
        assert "SECTION%02d" % i in joined
    assert any("whole" in n for n in notes)


def test_brief_samples_evenly_when_the_document_is_huge(monkeypatch):
    import llm
    monkeypatch.setattr(llm, "chat_json",
                        lambda s, u, **k: ({"disclosure": "b", "title": "", "keywords": []}
                                           if "SEARCH BRIEF" in s else {"facts": []}))
    huge = "x" * (patent_doc.WINDOW_CHARS * patent_doc.MAX_WINDOWS * 3)
    wins, sampled = patent_doc._windows(huge)
    assert sampled and len(wins) == patent_doc.MAX_WINDOWS
    notes = []
    patent_doc.search_brief(huge, {"abstract": "", "claims": []}, notes)
    assert any("sampled evenly" in n for n in notes)


def test_brief_falls_back_to_abstract_and_claims_without_a_model(monkeypatch):
    import llm
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {})
    notes = []
    out = patent_doc.search_brief(
        "some text", {"abstract": "An abstract about a gripper.",
                      "claims": [{"claim_no": 1, "text": "A gripper comprising a cup.",
                                  "independent": True}]}, notes)
    assert "abstract about a gripper" in out["disclosure"]
    assert "gripper comprising a cup" in out["disclosure"]


def test_analyze_without_a_model_is_deterministic_only():
    a = patent_doc.analyze(text=US_STYLE, use_model=False)
    assert a["n_claims"] == 3 and a["n_independent"] == 1
    assert a["claims_source"] == "layout"
    assert "sealing lip" in a["abstract"].lower()


# ---------------------------------------------------------------------------
# PDF layout: columns, line numbers, hyphenation
# ---------------------------------------------------------------------------
def _two_column_pdf(path, left_lines, right_lines, line_numbers=True):
    """A two-column page laid out the way a granted patent is, with marginal line numbers."""
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=(612, 792))
    c.setFont("Helvetica", 9)
    for col, lines in ((58, left_lines), (320, right_lines)):
        y = 720
        for i, ln in enumerate(lines, 1):
            c.drawString(col, y, ln)
            if line_numbers and i % 5 == 0:
                c.drawString(col + 232, y, str(i))       # right margin of this column
            y -= 12
    c.showPage()
    c.save()
    return str(path)


def test_pdf_columns_are_not_interleaved(tmp_path):
    """poppler's reading order splices the two columns line by line; every sentence and every
    claim then reads as two half-sentences spliced together."""
    left = ["The first platform receives manual placement of a first",
            "article component at a first placement position thereon",
            "by an operator located adjacent an operator side of the",
            "first platform, and the platform is moveable to reposi-",
            "tion the component to a pick up position.",
            "A cover separates the operator from the robot side."]
    right = ["The second platform has a first predetermined location",
             "for the first article component, and an electroadhesive",
             "capture element captures the component placed on the",
             "first platform under control of a multi axis robotic",
             "actuator positioned away from the operator side.",
             "The zones are separately activated."]
    pdf = _two_column_pdf(tmp_path / "twocol.pdf", left, right)
    out = patent_pdf.extract(pdf)
    assert out["text_layer"] is True
    # line breaks are preserved on purpose (the claims/abstract headings are line-anchored);
    # flatten them here so the assertions read on sentences.
    text = out["text"]
    flat = " ".join(text.split())
    # the left column reads as one continuous passage ...
    assert "receives manual placement of a first article component" in flat
    # ... and so does the right one, with no line from the other column spliced in
    assert "capture element captures the component placed on the first platform" in flat
    # de-hyphenation across the line break
    assert "reposition the component" in flat
    # the left column is emitted in full BEFORE the right column starts
    assert text.index("A cover separates") < text.index("The second platform has")


def test_pdf_strips_marginal_line_numbers(tmp_path):
    left = ["alpha bravo charlie delta echo foxtrot golf hotel india",
            "juliet kilo lima mike november oscar papa quebec romeo",
            "sierra tango uniform victor whiskey xray yankee zulu one",
            "two three four five six seven eight nine ten eleven twelve",
            "thirteen fourteen fifteen sixteen seventeen eighteen nine",
            "twenty twentyone twentytwo twentythree twentyfour five"]
    pdf = _two_column_pdf(tmp_path / "lineno.pdf", left, list(left))
    flat = " ".join(patent_pdf.extract(pdf)["text"].split())
    # the "5" printed in the margin beside line 5 must not appear inside the sentence
    assert "eighteen nine" in flat
    assert "eighteen nine 5" not in flat and "five 5" not in flat


def test_pdf_reports_a_scan_with_no_text_layer(tmp_path):
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    p = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(p), pagesize=(612, 792))
    c.rect(50, 50, 200, 200, fill=0)          # a drawing, no text
    c.showPage()
    c.save()
    out = patent_pdf.extract(str(p))
    assert out["text_layer"] is False
    assert any("no usable text layer" in n for n in out["notes"])


# ---------------------------------------------------------------------------
# corrections must reach retrieval
# ---------------------------------------------------------------------------
def test_rebuild_from_edits_reembeds_the_corrected_claims():
    out = ii.rebuild_from_edits(
        abstract="A corrected abstract about a vacuum lifter.",
        claims=[{"text": "A vacuum lifter comprising a suction cup.", "independent": True},
                {"text": "The lifter of claim 1, wherein the cup is elastomeric."}],
        brief="a corrected brief", title="Vacuum lifter")
    assert out["n_claims"] == 2 and out["n_independent"] == 1
    claim_chunks = [c for c in out["chunks"] if c["kind"] == "claim_own"]
    assert len(claim_chunks) == 2
    assert all(c["vector"] and len(c["vector"]) == ii.EMBED_DIM for c in out["chunks"])
    assert any("corrected abstract" in c["text"] for c in out["chunks"])


def test_rebuild_from_edits_renumbers_after_a_deletion():
    out = ii.rebuild_from_edits(claims=[{"text": "Claim A, long enough to be a real claim."},
                                        {"text": "Claim B, long enough to be a real claim."}])
    assert [c["claim_no"] for c in out["claims"]] == [1, 2]


def test_revise_route_returns_a_new_token_and_keeps_the_old_one(app_client, monkeypatch, tmp_path):
    import webapp
    monkeypatch.setattr(webapp, "DOCSTASH", tmp_path)
    original = {
        "ok": True, "source": "upload", "label": "spec.pdf", "title": "T",
        "full_text": "the inventor's verbatim upload",
        "chunks": [{"kind": "claim_own", "text": "A wrong OCR claim.",
                    "coord": {"claim_no": 1}, "independent": True, "vector": [0.1, 0.2]}],
        "figure_images": [{"mime": "image/png",
                           "b64": "UE5HREFUQQ=="}],
    }
    old = webapp._stash_doc(original)
    assert old

    r = app_client.post("/extract/revise", json={
        "doc_token": old, "title": "T", "abstract": "A corrected abstract.",
        "claims": [{"text": "A correctly transcribed claim about a suction cup.",
                    "independent": True}],
        "brief": "a brief"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and body["doc_token"] and body["doc_token"] != old
    assert body["n_claims"] == 1 and body["n_embedded"] == body["n_chunks"]

    new = webapp._load_doc_materials(body["doc_token"])
    assert new["claims"][0]["text"].startswith("A correctly transcribed claim")
    # the drawings and the verbatim upload survive the correction — the user edited the TEXT
    assert new["figure_blobs"] and new["full_text"] == "the inventor's verbatim upload"
    # the original stash is untouched, so an open tab still resolves its own token
    assert webapp._load_doc_materials(old)["claims"][0]["text"] == "A wrong OCR claim."


def test_revise_route_rejects_an_empty_revision(app_client, monkeypatch, tmp_path):
    import webapp
    monkeypatch.setattr(webapp, "DOCSTASH", tmp_path)
    r = app_client.post("/extract/revise", json={"claims": [], "abstract": "", "brief": ""})
    assert r.status_code == 400 and r.get_json()["ok"] is False


def test_revise_route_rejects_an_expired_token(app_client, monkeypatch, tmp_path):
    import webapp
    monkeypatch.setattr(webapp, "DOCSTASH", tmp_path)
    r = app_client.post("/extract/revise",
                        json={"doc_token": "deadbeef" * 4, "abstract": "x", "claims": [],
                              "brief": "y"})
    assert r.status_code == 410


# ---------------------------------------------------------------------------
# the /extract contract the review panel depends on
# ---------------------------------------------------------------------------
def test_extract_upload_returns_the_review_material(monkeypatch):
    """The panel needs the abstract and each claim SEPARATELY, plus a brief — not one blob."""
    monkeypatch.setattr(ii, "_pdf_figures", lambda data: [])
    r = ii.extract_upload(US_STYLE.encode(), "spec.txt")
    assert r["ok"] is True
    assert "sealing lip" in r["abstract"].lower()
    assert r["n_claims"] == 3 and r["n_independent"] >= 1
    assert [c["claim_no"] for c in r["claims"]] == [1, 2, 3]
    assert r["claims"][0]["independent"] is True
    assert r["brief"]
    assert r["verified"] is True


def test_extract_route_exposes_claims_and_abstract_but_not_vectors(app_client, monkeypatch,
                                                                   tmp_path):
    import webapp
    monkeypatch.setattr(webapp, "DOCSTASH", tmp_path)
    r = app_client.post("/extract",
                        data={"file": (io.BytesIO(US_STYLE.encode()), "spec.txt")},
                        content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and body["doc_token"]
    assert body["abstract"] and body["claims"] and body["n_claims"] == 3
    assert "chunks" not in body and "figure_images" not in body and "full_text" not in body
    # every claim carries the fields the panel renders
    assert set(body["claims"][0]) >= {"claim_no", "text", "independent"}


# ---------------------------------------------------------------------------
# a search interrupted by a restart must not sit at "running" for ever
# ---------------------------------------------------------------------------
def test_recovery_completes_a_finished_search_and_fails_a_partial(monkeypatch, tmp_path):
    """Completion is recorded in-process. A deploy in the middle of a multi-minute search used to
    leave the row saying "running" and the promised email never went out."""
    import webapp

    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(webapp, "report_path", lambda slug: tmp_path / f"{slug}.json")
    monkeypatch.setattr(webapp.auth, "accounts_enabled", lambda app=None: True)

    (tmp_path / "done-search.json").write_text(json.dumps({"partial": False, "results": []}))
    (tmp_path / "half-search.json").write_text(json.dumps({"partial": True, "results": []}))
    # "gone-search" deliberately has no report at all

    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *a): pass
        def fetchall(self):
            return [{"slug": "done-search"}, {"slug": "half-search"}, {"slug": "gone-search"}]

    monkeypatch.setattr(webapp.db, "cursor", lambda *a, **k: FakeCursor())
    queued, failed = [], []
    monkeypatch.setattr(webapp.notifications, "queue_search_completion", lambda s: queued.append(s))
    monkeypatch.setattr(webapp.accounts, "mark_search_failed", lambda s: failed.append(s))

    out = webapp.recover_interrupted_searches()
    assert queued == ["done-search"]                      # finished: email the user, as promised
    assert sorted(failed) == ["gone-search", "half-search"]   # never finished: say so
    #  `still_running` was added by the durable cutover: a slug whose durable run is still live
    #  belongs to the worker and must be left alone. With the flag off it is always zero, which is
    #  what this test is asserting here, and what keeps the pre-cutover behaviour exact.
    assert out == {"completed": 1, "failed": 2, "still_running": 0}


def test_recovery_survives_an_unavailable_account_store(monkeypatch):
    """The search page must come up even when the accounts store does not."""
    import webapp

    monkeypatch.setattr(webapp.auth, "accounts_enabled", lambda app=None: True)

    def boom(*a, **k):
        raise RuntimeError("accounts store down")

    monkeypatch.setattr(webapp.db, "cursor", boom)
    assert webapp.recover_interrupted_searches() == {"completed": 0, "failed": 0,
                                                     "still_running": 0}


def test_extract_jobs_are_bounded(app_client, monkeypatch):
    """A background extraction is outside gunicorn's worker pool, so it needs its own bound:
    each one holds up to 30 MB of upload, runs poppler and makes about a dozen Vertex calls."""
    import webapp

    monkeypatch.setattr(webapp, "_EXTRACT_SLOTS", __import__("threading").BoundedSemaphore(1))
    started = []
    monkeypatch.setattr(webapp.ingest_input, "extract_upload",
                        lambda *a, **k: started.append(1) or {"ok": True, "chunks": []})
    webapp._EXTRACT_SLOTS.acquire()                       # pretend one extraction is in flight
    r = app_client.post("/extract", data={"file": (io.BytesIO(b"text"), "a.txt"), "async": "1"},
                        content_type="multipart/form-data")
    assert r.status_code == 429 and r.get_json()["ok"] is False
    webapp._EXTRACT_SLOTS.release()
    r = app_client.post("/extract", data={"file": (io.BytesIO(b"text"), "a.txt"), "async": "1"},
                        content_type="multipart/form-data")
    assert r.status_code == 202 and r.get_json()["job"]


def test_extract_status_unknown_job(app_client):
    r = app_client.get("/extract/status/" + "0" * 32)
    assert r.status_code == 404 and r.get_json()["state"] == "unknown"
