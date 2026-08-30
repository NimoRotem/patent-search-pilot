from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shared_shell_uses_the_new_rotem_patents_identity():
    base = (ROOT / "templates" / "base.html").read_text()

    assert 'aria-label="Rotem Patents home"' in base
    assert 'class="brandmark"' in base
    assert "Rotem Patents" in base


def test_shared_shell_declares_a_light_interface():
    base = (ROOT / "templates" / "base.html").read_text()

    assert '<meta name="color-scheme" content="light">' in base


def test_design_tokens_are_warm_neutral_with_one_blue_accent():
    css = (ROOT / "static" / "style.css").read_text()

    assert "--s0:#f5f4ef" in css
    assert "--accent:#175cd3" in css
    assert "--secret:#0f766e" in css


def test_mobile_navigation_has_an_explicit_layout():
    css = (ROOT / "static" / "style.css").read_text()

    assert "header.top .primarynav{grid-area:primary" in css
    assert "header.top .helpnav,header.top .signout{display:none!important}" in css


def test_landing_page_uses_the_new_product_preview_layout():
    landing = (ROOT / "templates" / "landing.html").read_text()

    assert 'class="landingmast"' in landing
    assert 'class="workpreview"' in landing


def test_search_page_opens_with_a_compact_task_heading():
    search = (ROOT / "templates" / "index.html").read_text()

    assert 'class="searchintro"' in search
    assert "Search prior art" in search


def test_old_product_name_is_not_left_on_account_pages_or_messages():
    product_text = "\n".join(
        path.read_text()
        for path in [
            *sorted((ROOT / "templates").glob("*.html")),
            ROOT / "src" / "auth.py",
            ROOT / "src" / "notifications.py",
            ROOT / "src" / "report_archive.py",
        ]
    )

    assert "rotemAI patent search" not in product_text
    assert "rotemAI patent-search" not in product_text


def test_every_application_page_title_uses_the_shared_product_name():
    standalone_documents = {"draft_print.html", "print.html"}

    for path in sorted((ROOT / "templates").glob("*.html")):
        if path.name in standalone_documents:
            continue
        source = path.read_text()
        if "{% block title %}" in source:
            assert "Rotem Patents" in source, path.name
