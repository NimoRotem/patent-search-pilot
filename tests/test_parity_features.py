"""The IPTorch-parity layer: client-facing report, IDS listing, library, figures, invitations.

Hermetic — the model is stubbed by conftest's autouse fixture and the image model is stubbed per
test. Each test anchors on a specific decision, several of which were bugs found while building:

  * a route named after the app's own mount prefix is unreachable behind the proxy (`/patents`
    resolved to the index page);
  * a module named `figures` was shadowed by an existing route function of the same name and the
    app failed to boot;
  * the image model copied "10 = suction cup" onto the drawing when the numerals were passed as
    an equals mapping;
  * an edited letterhead served a stale cached export because the cache key did not include it.
"""
import io
import json

import pytest

import deliverables
import draft_figures
import export_ids
import library


# ---------------------------------------------------------------------------
# the IDS citation listing
# ---------------------------------------------------------------------------
def test_ids_splits_into_the_form_s_three_tables():
    """SB/08a has separate tables for US patents, US applications and foreign documents, and a
    document in the wrong one is a defective disclosure."""
    model = {"references": [
        {"pub": "US-11338449-B2", "assignees": ["Nike Inc"], "publication_date": "2022-05-24"},
        {"pub": "US-20220242700-A1", "assignees": ["Vacuworx"], "publication_date": "2022-08-04"},
        {"pub": "US-3240525-A", "inventors": ["A Smith", "B Jones"], "publication_date": "1966-03-15"},
        {"pub": "CN-217398348-U", "assignees": ["Grabo"], "publication_date": "2022-09-06"},
        {"pub": "DE-1286275-B", "publication_date": "1969-01-02"},
    ]}
    b = export_ids.build(model)
    assert [r["pub"] for r in b["us_patents"]] == ["US-11338449-B2", "US-3240525-A"]
    assert [r["pub"] for r in b["us_applications"]] == ["US-20220242700-A1"]
    assert [r["pub"] for r in b["foreign"]] == ["CN-217398348-U", "DE-1286275-B"]
    # the running cite number is continuous ACROSS the tables, as the form requires
    assert sorted(r["cite"] for r in b["us_patents"] + b["us_applications"] + b["foreign"]) == \
        [1, 2, 3, 4, 5]


def test_ids_patentee_prefers_the_assignee_and_marks_multiple_inventors():
    rows = export_ids.build({"references": [
        {"pub": "US-1-B2", "assignees": ["Acme Corp"], "inventors": ["X", "Y"]},
        {"pub": "US-2-B2", "inventors": ["Solo Inventor"]},
        {"pub": "US-3-B2", "inventors": ["First One", "Second One"]},
        {"pub": "US-4-B2"},
    ]})["us_patents"]
    assert [r["patentee"] for r in rows] == \
        ["Acme Corp", "Solo Inventor", "First One et al.", "—"]


def test_ids_us_patent_number_is_grouped_but_an_application_number_is_not():
    """A patent number prints as 11,338,449; an 11-digit publication number must not acquire
    commas — it is not that kind of number."""
    b = export_ids.build({"references": [{"pub": "US-11338449-B2"},
                                         {"pub": "US-20220242700-A1"}]})
    assert b["us_patents"][0]["number_display"] == "11,338,449"
    assert b["us_applications"][0]["number_display"] == "20220242700"


def test_ids_renders_a_pdf(tmp_path):
    pytest.importorskip("reportlab")
    out = tmp_path / "ids.pdf"
    export_ids.render({"title": "T", "slug": "s", "references": [
        {"pub": "US-11338449-B2", "assignees": ["Acme"], "publication_date": "2022-05-24"}]}, out)
    assert out.exists() and out.read_bytes().startswith(b"%PDF")


def test_ids_with_no_selection_still_renders(tmp_path):
    pytest.importorskip("reportlab")
    out = tmp_path / "empty.pdf"
    export_ids.render({"title": "T", "slug": "s", "references": []}, out)
    assert out.read_bytes().startswith(b"%PDF")


# ---------------------------------------------------------------------------
# the client-facing report layer
# ---------------------------------------------------------------------------
def test_report_narrative_never_states_a_legal_conclusion(monkeypatch):
    """This text sits directly above a list of prior art on a firm's letterhead — the likeliest
    place in the product for an unsupported opinion to appear."""
    import llm
    monkeypatch.setattr(llm, "chat_json", lambda s, u, **k: {
        "text": "The art discloses a suction cup and a pump. The invention is patentable over "
                "the cited art. A pressure sensor was not found in any reference."})
    out = deliverables.suggest("key_findings", {"cards": [{"pub": "US-1", "title": "t"}]},
                               {"query": "a vacuum lifter"})
    assert "patentable over" not in out
    assert "suction cup and a pump" in out          # the sound sentences survive
    assert "pressure sensor was not found" in out


def test_report_narrative_keeps_a_clean_draft_whole(monkeypatch):
    import llm
    monkeypatch.setattr(llm, "chat_json", lambda s, u, **k: {
        "text": "US-1 discloses a suction cup. No reference discloses the alarm."})
    out = deliverables.suggest("purpose", {"cards": []}, {"query": "q"})
    assert out == "US-1 discloses a suction cup. No reference discloses the alarm."


def test_report_narrative_rejects_an_unknown_section():
    with pytest.raises(ValueError):
        deliverables.suggest("conclusion", {}, {})


def test_logo_must_be_a_real_image_of_its_declared_type(monkeypatch):
    """A file that merely claims to be a PNG must not be stored and later served back with an
    image content type."""
    calls = []
    monkeypatch.setattr(deliverables, "get_or_create", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(deliverables, "ensure_schema", lambda *a, **k: None)
    with pytest.raises(ValueError):
        deliverables.set_logo(1, "s", b"<html>gotcha", "image/png")
    with pytest.raises(ValueError):
        deliverables.set_logo(1, "s", b"\x89PNG\r\n\x1a\n", "application/pdf")
    with pytest.raises(ValueError):
        deliverables.set_logo(1, "s", b"x" * (deliverables.MAX_LOGO_BYTES + 1), "image/png")


def test_share_token_is_stored_hashed():
    """A database copy must not be a set of working links."""
    a = deliverables._hash_token("abc")
    assert a != "abc" and len(a) == 64
    assert deliverables._hash_token("abc") == a
    assert deliverables._hash_token("abd") != a


def test_clean_fields_ignores_unknown_keys_and_strips_control_characters():
    out = deliverables.clean_fields({"firm_name": "Ac\x00me\tIP  Ltd", "is_admin": "1",
                                     "purpose": "line one\nline two"})
    assert out == {"firm_name": "Acme IP Ltd", "purpose": "line one\nline two"}
    assert "is_admin" not in out


# ---------------------------------------------------------------------------
# the saved-patent library
# ---------------------------------------------------------------------------
def test_library_refuses_anything_that_is_not_a_publication_number():
    assert library.normalize_pub("us 11338449 b2") == "US11338449B2"
    for bad in ("", None, "hello", "12345", "<script>", "x" * 80):
        with pytest.raises(ValueError):
            library.normalize_pub(bad)


def test_library_route_is_not_named_after_the_mount_prefix():
    """The app is served at rotem.ai/patents and the prefix middleware strips that prefix from
    PATH_INFO. A route at `/patents` therefore arrived as `/patents`, was stripped a SECOND time
    to `""`, and silently served the index page instead."""
    import webapp
    paths = {str(r) for r in webapp.app.url_map.iter_rules()}
    assert "/library" in paths
    assert "/patents" not in paths


# ---------------------------------------------------------------------------
# patent figures
# ---------------------------------------------------------------------------
def test_figures_are_read_out_of_the_drafts_own_drawings_section():
    figs = draft_figures.figures_from_draft({"drawing_descriptions":
        "FIG. 1 is a side elevation view of the vacuum lifter.\n"
        "FIG. 2 is a sectional view of the suction cup.\n"
        "FIGURE 3 shows the control circuit.\n"})
    assert [f["label"] for f in figs] == ["FIG. 1", "FIG. 2", "FIG. 3"]
    assert figs[1]["caption"] == "a sectional view of the suction cup"


def test_figures_invents_nothing_when_there_is_no_drawings_section():
    assert draft_figures.figures_from_draft({}) == []
    assert draft_figures.figures_from_draft({"drawing_descriptions": ""}) == []


def test_numerals_come_from_the_draft_and_name_the_right_part():
    """Measured without this: the model invented numerals 18 and 20, used 16 twice, and wrote the
    word "sensor" where a numeral belonged."""
    d = ("A suction cup 10 carries a flexible sealing lip 12 on a rigid body 14 with a handle 16. "
         "An electric vacuum pump 20 sits inside. A pressure sensor 30 monitors grip vacuum and "
         "drives a warning indicator 32. A rechargeable battery 40 powers the pump.")
    got = dict(x.split(" = ", 1) for x in draft_figures.numerals_for({}, disclosure=d))
    assert got["10"] == "suction cup"
    assert got["12"] == "flexible sealing lip"
    assert got["20"] == "electric vacuum pump"
    assert got["32"] == "warning indicator"          # not "grip vacuum and drives a warning indicator"
    assert got["40"] == "rechargeable battery"


def test_prompt_keeps_reference_text_out_of_image_generation():
    """Reference text is added deterministically after the geometry passes review."""
    p = draft_figures.build_prompt("FIG. 1", "a lifter", ["10 = suction cup", "20 = pump"])
    assert "10 = suction cup" not in p
    assert "the suction cup" in p
    assert "the pump" in p
    assert "10" not in p and "20" not in p
    assert "reference" not in p.lower() and "label" not in p.lower()


def test_prompt_forbids_numerals_when_the_draft_establishes_none():
    """An invented numbering has to be renumbered by hand against the specification later, which
    is worse than a drawing with no numerals at all."""
    p = draft_figures.build_prompt("FIG. 1", "a lifter", [])
    assert "without text or digits" in p
    assert "label" not in p.lower() and "lead line" not in p.lower()


def test_figure_numeral_audit_compares_both_directions_and_duplicates():
    audit = draft_figures.numeral_audit(["10 = body", "12 = pump"], ["10", "10", "14"])
    assert audit["missing"] == ["12"]
    assert audit["unexpected"] == ["14"]
    assert audit["duplicates"] == ["10"]
    assert audit["ok"] is False


def test_figure_image_model_is_configurable_by_role(monkeypatch):
    monkeypatch.setenv("PATENT_FIGURE_IMAGE_MODEL", "test-image-model")
    assert draft_figures.image_model() == "test-image-model"


def test_picture_upload_is_normalized_and_non_images_are_refused():
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (80, 60), "white").save(stream, format="JPEG")
    normalized = draft_figures.normalize_source_image(stream.getvalue(), "image/jpeg")
    assert normalized.startswith(b"\x89PNG")
    with pytest.raises(draft_figures.FigureError):
        draft_figures.normalize_source_image(b"not an image", "image/png")
    unsupported = io.BytesIO()
    Image.new("RGB", (80, 60), "white").save(unsupported, format="TIFF")
    with pytest.raises(draft_figures.FigureError):
        draft_figures.normalize_source_image(unsupported.getvalue(), "image/png")


def test_selected_area_edit_composites_only_inside_the_requested_rectangle(monkeypatch):
    from PIL import Image

    source = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(source, format="PNG")
    replacement = io.BytesIO()
    Image.new("RGB", (30, 40), "black").save(replacement, format="PNG")
    monkeypatch.setattr(draft_figures, "generate_png", lambda prompt, previous_png=None:
                        replacement.getvalue())
    edited = draft_figures.edit_region_png(source.getvalue(), "repair this", (10, 20, 40, 60))
    image = Image.open(io.BytesIO(edited)).convert("RGB")
    assert image.getpixel((0, 0)) == (255, 255, 255)
    assert image.getpixel((20, 30)) == (0, 0, 0)
    assert image.getpixel((60, 60)) == (255, 255, 255)

    with pytest.raises(draft_figures.FigureError, match="larger area"):
        draft_figures.edit_region_png(
            source.getvalue(), "repair this", (9_999, 9_999, 20_000, 20_000))


def test_a_figure_cannot_be_edited_through_another_owned_project(monkeypatch):
    monkeypatch.setattr(draft_figures, "get_figure", lambda figure_id, user_id: {
        "id": figure_id, "user_id": user_id, "project_id": 88,
        "figure_label": "FIG. 1", "caption": "another draft",
    })
    with pytest.raises(draft_figures.FigureError, match="no such figure"):
        draft_figures.render_figure(
            77, 91, label="FIG. 1", caption="target draft", figure_id=4, numerals=[])


def test_figure_module_is_not_named_after_an_existing_route_function():
    """`import figures` was shadowed by the `figures` route (the reference-drawing file server),
    and the app booted straight into "'function' object has no attribute 'ensure_schema'"."""
    import webapp
    assert webapp.draft_figures is draft_figures
    assert callable(webapp.figures)                  # still the route function, not the module


def test_generate_png_surfaces_a_refusal_instead_of_returning_nothing(monkeypatch):
    class Part:
        inline_data = None
        text = "I cannot draw a weapon."

    class Resp:
        candidates = [type("C", (), {"content": type("X", (), {"parts": [Part()]})()})()]
        usage_metadata = None

    import llm
    monkeypatch.setattr(llm, "_client", lambda: type("C", (), {
        "models": type("M", (), {"generate_content": staticmethod(lambda **k: Resp())})()})())
    with pytest.raises(draft_figures.FigureError) as exc:
        draft_figures.generate_png("draw something")
    assert "cannot draw" in str(exc.value)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
def test_every_route_maps_to_a_public_function():
    """A decorator that drifts off the function below it silently rebinds the URL. It happened:
    `@app.route("/drafts/<int:project_id>")` landed on a private helper and every draft page
    404'd with "Could not build url for endpoint 'draft_detail'"."""
    import webapp
    private = [(str(r), r.endpoint) for r in webapp.app.url_map.iter_rules()
               if r.endpoint.startswith("_")]
    assert private == []


def test_the_parity_routes_all_exist():
    import webapp
    have = {r.endpoint for r in webapp.app.url_map.iter_rules()}
    for endpoint in ("saved_patents", "api_library", "api_library_state", "api_improve_query",
                     "api_more_references", "report_details", "report_logo_upload", "report_logo",
                     "api_report_suggest", "report_share", "shared_report", "shared_report_logo",
                     "draft_figure_generate", "draft_figure_png", "draft_figure_activate",
                     "draft_figure_delete", "auth.accept_invitation", "auth.verify_email",
                     "auth.admin_invite", "auth.admin_delete_user", "auth.admin_search_detail"):
        assert endpoint in have, f"{endpoint} is missing"


def test_share_and_invite_are_reachable_without_a_session():
    """Both exist precisely for somebody who has no account; gating them would make them useless.
    Each is a single-use or revocable hashed token that resolves to exactly one thing."""
    import auth
    assert "shared_report" in auth._OPEN_ENDPOINTS
    assert "auth.accept_invitation" in auth._OPEN_ENDPOINTS
    assert "auth.verify_email" in auth._OPEN_ENDPOINTS
    #  ...and nothing that writes is open
    for endpoint in ("report_share", "api_library", "draft_figure_generate", "export"):
        assert endpoint not in auth._OPEN_ENDPOINTS


def test_ids_is_an_export_format(app_client):
    import webapp
    assert "ids" in webapp.EXPORT_FORMATS
    spec = webapp.EXPORT_FORMATS["ids"]
    assert spec["drawings"] is False and spec["text"] is False
    assert spec["mime"] == "application/pdf"


def test_export_cache_key_changes_when_the_letterhead_changes(app_client, monkeypatch, tmp_path):
    """Editing the client name and re-exporting used to hand back the file built before the edit."""
    import webapp
    #  Point the export directory at a fresh temp dir: the real one persists between runs, so the
    #  first assertion silently passed on a file an earlier run had left behind.
    monkeypatch.setattr(webapp, "EXPORTS", tmp_path)
    seen = []

    def fake_doc(slug):
        return {"updated_at": seen[-1] if seen else "t0"}

    monkeypatch.setattr(webapp, "_report_doc", fake_doc)
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    monkeypatch.setattr(webapp, "valid_slug", lambda slug: True)
    rendered = []
    monkeypatch.setattr(webapp.export_data, "assemble",
                        lambda *a, **k: {"references": [], "title": "t", "slug": "s"})
    def capture(model, out):
        rendered.append(str(out))
        out.write_bytes(b"%PDF-1.4 stub")

    monkeypatch.setitem(webapp.EXPORT_FORMATS["ids"], "render", capture)
    monkeypatch.setattr(webapp, "report_path", lambda slug: __import__("pathlib").Path(__file__))

    app_client.post("/export", data={"slug": "s1", "format": "ids", "pubs": "US-1-B2"})
    seen.append("t1")
    app_client.post("/export", data={"slug": "s1", "format": "ids", "pubs": "US-1-B2"})
    assert len(set(rendered)) == 2, "a changed letterhead must not reuse the cached export"


# ---------------------------------------------------------------------------
# draft generation: one corrective call rather than three wasted attempts
# ---------------------------------------------------------------------------
def test_draft_generation_asks_again_for_a_section_the_model_skipped(monkeypatch):
    """Reproduced every time on a real project: asked for nine sections with the project title
    already in SOURCE_DATA, the model returned eight and omitted `title`. Every attempt then
    failed validation and a 21,000-character prompt was thrown away three times over one string."""
    import draft_worker
    import drafting
    calls = []

    def fake(system, user, **kw):
        calls.append(user)
        if len(calls) == 1:
            return {k: f"{k} text" for k in drafting.SECTION_KEYS if k != "title"}
        assert "required section(s) from your JSON: title (Title)" in user
        return {"title": "A Handheld Vacuum Lifter"}

    monkeypatch.setattr(draft_worker.llm, "chat_json", fake)
    out = draft_worker._generate("sys", "user")
    assert out["title"] == "A Handheld Vacuum Lifter"
    assert draft_worker._missing_sections(out) == []
    assert len(calls) == 2, "exactly one corrective call, not a retry of the whole draft"


def test_draft_generation_does_not_call_again_when_the_draft_is_complete(monkeypatch):
    import draft_worker
    import drafting
    calls = []
    monkeypatch.setattr(draft_worker.llm, "chat_json",
                        lambda s, u, **k: calls.append(u) or
                        {key: f"{key} text" for key in drafting.SECTION_KEYS})
    draft_worker._generate("sys", "user")
    assert len(calls) == 1


def test_draft_generation_still_fails_when_a_section_is_never_supplied(monkeypatch):
    """A draft missing its claims must NOT be quietly patched from somewhere else."""
    import draft_worker
    import drafting
    monkeypatch.setattr(draft_worker.llm, "chat_json",
                        lambda s, u, **k: {key: f"{key} text"
                                           for key in drafting.SECTION_KEYS if key != "claims"})
    with pytest.raises(RuntimeError) as exc:
        draft_worker._generate("sys", "user")
    assert "claims" in str(exc.value)
