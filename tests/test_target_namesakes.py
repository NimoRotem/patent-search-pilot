"""A target's guards against namesakes: companies only, look-alike exclusions, initialisms."""
import observation_refresh as R


def test_an_initialism_is_part_of_the_name_but_a_company_form_is_not():
    assert R.name_words("HG Commerciale") == ["hg", "commerciale"]
    assert R.name_words("Piab AB") == ["piab"]
    assert R.name_words("J. Schmalz GmbH") == ["schmalz"]
    assert R.name_words("Manta Guangzhou") == ["manta", "guangzhou"]


def test_companies_only_keeps_the_company_and_drops_people_with_its_name():
    t = {"companies_only": True}
    w = [R.name_words("Probst GmbH")]
    assert R.owner_matches(t, w, ["PROBST GMBH"])
    assert R.owner_matches(t, w, ["Probst Greiftechnik Verlegesysteme GmbH"])
    assert not R.owner_matches(t, w, ["PROBST, RICARDO CARREON"])
    assert not R.owner_matches(t, w, ["PROBST HANS"])
    assert R.owner_matches(t, [R.name_words("Raimondi")], ["RAIMONDI S.P.A."])


def test_an_excluded_look_alike_never_counts():
    t = {"companies_only": True, "exclude": ["Probst Preserves", "Raimondi Cranes"]}
    assert not R.owner_matches(t, [R.name_words("Probst")], ["Probst Preserves, LLC"])
    assert not R.owner_matches(t, [R.name_words("Raimondi")], ["RAIMONDI CRANES S.p.A."])
    assert R.owner_matches(t, [R.name_words("Raimondi")], ["RAIMONDI SPA"])


def test_a_target_without_guards_matches_exactly_as_before():
    w = [R.name_words("Schmalz")]
    for cand in (["J. Schmalz GmbH"], ["Schmalz, Kurt"], ["SCHMALZ KURT"]):
        assert R.owner_matches({}, w, cand) == R.name_matches(w, cand)


def test_an_owner_search_skips_a_leading_place_name():
    import observation_marks as M
    assert M.search_word(R.name_words("Zhejiang Kaikai One Tool Co., Ltd.")) == "kaikai"
    assert M.search_word(R.name_words("Shanghai Vinon")) == "vinon"
    assert M.search_word(R.name_words("Guangzhou Cowest Machinery")) == "cowest"
    #  A name that does not start with one keeps its first word, as every target did before.
    assert M.search_word(R.name_words("J. Schmalz GmbH")) == "schmalz"
    assert M.search_word(R.name_words("Binar Quick-Lift Systems")) == "binar"
    #  Too short or too common to search on its own.
    assert M.search_word(R.name_words("HG Commerciale")) == "commerciale"
    assert M.search_word(R.name_words("Lark Quzhou")) == "quzhou"
    assert M.search_word(R.name_words("Weha Ludwig Werwein")) == "werwein"


def test_a_strangers_designs_cost_one_detail_call(monkeypatch):
    import observation_marks as M
    hits = [{"designNumber": "00%d-0001" % i, "applicants": [{"office": "EM", "identifier": "959818"}]}
            for i in range(5)]
    calls = []
    monkeypatch.setattr(M, "euipo_designs", lambda word, **k: hits)

    def row(d, name=""):
        calls.append(d["designNumber"])
        return {"publication": "RCD" + d["designNumber"], "applicants": ["The KaiKai Company GbR"],
                "applicant": "The KaiKai Company GbR"}
    monkeypatch.setattr(M, "euipo_design_row", row)
    new, rejected, errors = M.discover({"name": "Kaikai", "assignees": ["Zhejiang Kaikai One Tool"],
                                        "offices": ["EP"], "companies_only": True}, "design", set())
    assert new == [] and len(rejected) == 5 and len(calls) == 1
