"""Data-quality regression (M8 §2): what we SHOW must correspond to the underlying publication.
Guards against mislabelled evidence — cards' biblio must match the DB row for that exact pub, and
claim-chart cells must match the report's element_evidence (no off-by-one / wrong-pub rendering)."""
import json
import pytest
import webview
from config import DATA


@pytest.fixture(scope="module")
def view():
    rep = json.loads((DATA / "reports" / "grabo_gripper_novelty.json").read_text())
    return rep, webview.build_view(rep, top_n=25)


def test_card_biblio_matches_the_publication(view):
    import db
    _, v = view
    conn = db.connect(); conn.autocommit = True; cur = conn.cursor()
    for c in v["cards"][:10]:                       # spot-check the top 10 across jurisdictions
        cur.execute("SELECT id, title, country FROM publications WHERE publication_number=%s", (c["pub"],))
        row = cur.fetchone()
        assert row is not None, f"card pub {c['pub']} not in DB"
        assert row["title"] == c["title"], f"title mismatch for {c['pub']}"
        assert row["country"] == c["country"]
        cur.execute("SELECT array_agg(raw_name) a FROM parties WHERE publication_id=%s AND role='assignee'", (row["id"],))
        db_ass = set(x for x in (cur.fetchone()["a"] or []) if x)
        assert db_ass == set(c["assignees"]) or (not db_ass and not c["assignees"]), \
            f"assignee mismatch for {c['pub']}: DB {db_ass} vs card {c['assignees']}"
    cur.close(); conn.close()


def test_claim_chart_cells_match_element_evidence(view):
    rep, v = view
    ev = rep["element_evidence"]
    checked = 0
    for row in v["claim_chart"]["rows"]:
        hits = {h["pub"]: h for h in ev.get(row["element"], []) if h.get("pub")}
        for cell in row["cells"]:
            if not cell.get("covered"):
                continue
            checked += 1
            src = hits.get(cell["pub"])
            assert src is not None, f"chart cell {row['element']}×{cell['pub']} has no backing evidence"
            assert abs(cell["score"] - round(float(src["score"]), 3)) < 0.002, "cell score != evidence"
            assert cell["coord"] == webview._coord_str(src.get("coord")), "cell coord != evidence coord"
    assert checked > 0, "expected some filled claim-chart cells"


def test_covered_elements_come_from_evidence(view):
    """A card's 'covers_elements' must each actually cite that card's family in element_evidence."""
    rep, v = view
    ev = rep["element_evidence"]
    for c in v["cards"]:
        for el in c["covers_elements"]:
            fams = {h.get("family") for h in ev.get(el, [])}
            assert c["family"] in fams, f"{c['pub']} claims to cover '{el}' but isn't in its evidence"
