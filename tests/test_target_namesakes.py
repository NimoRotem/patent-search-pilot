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
