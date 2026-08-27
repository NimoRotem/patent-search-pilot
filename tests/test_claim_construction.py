"""A limitation is searched for what it REQUIRES, not only for the words it is written in.

The measured case throughout is the one counsel reported on 2026-08-26: claim 1[e] of the Schmalz
application is "the contact surface angle ranges in size from 170° to 190°", the report said 0 of
232 references disclosed it, and the specification defines the term one paragraph away as "at least
substantially parallel to the displacement direction". GB 874,600, which the same packet filed as
Document 6, claims the geometry outright.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import claim_construction as cc                                          # noqa: E402
import limitation_query as lq                                            # noqa: E402

SCHMALZ_SPEC = (
    "The magnet gripper comprises a pole shoe. Each of the workpiece contact surfaces is aligned "
    "at least substantially parallel to the displacement direction, i.e., defines between itself "
    "and the displacement direction of the magnet assembly a contact surface angle which ranges "
    "in size from 170° to 190°. The bevel angle of the pole shoe is between 130 and 170 degrees, "
    "preferably 140 to 160 degrees."
)

ANGLE_LIMITATION = "the contact surface angle ranges in size from 170° to 190°"
BEVEL_LIMITATION = "a bevel angle of between 130 and 170 degrees"


# --------------------------------------------------------------------------- geometry


def test_a_range_centred_on_180_degrees_is_parallel():
    rel, why = cc.angle_relation(170, 190)
    assert rel == cc.PARALLEL
    assert "parallel" in why


def test_a_range_centred_on_90_degrees_is_perpendicular():
    assert cc.angle_relation(85, 95)[0] == cc.PERPENDICULAR
    assert cc.angle_relation(88, 92)[0] == cc.PERPENDICULAR


def test_a_range_centred_on_zero_is_parallel():
    assert cc.angle_relation(0, 10)[0] == cc.PARALLEL
    assert cc.angle_relation(-5, 5)[0] == cc.PARALLEL


def test_a_genuinely_oblique_range_gets_no_relationship_invented_for_it():
    """Schmalz's own pole-shoe bevel. Calling 130 to 170 degrees "parallel" would be worse than
    saying nothing, and it is the discrimination the whole rule turns on."""
    assert cc.angle_relation(130, 170)[0] == ""
    assert cc.angle_relation(140, 160)[0] == ""
    assert cc.angle_relation(30, 45)[0] == ""


def test_the_angle_limitation_is_construed_as_parallel_and_the_bevel_is_not():
    con = cc.construe(ANGLE_LIMITATION)
    assert [r["kind"] for r in con["relations"]] == [cc.PARALLEL]
    assert "parallel" in con["terms"]
    assert "coplanar" in con["terms"]
    assert not con["words_alone"]

    bevel = cc.construe(BEVEL_LIMITATION)
    assert bevel["relations"] == []
    assert bevel["words_alone"]


def test_a_number_with_no_angle_anywhere_near_it_is_not_read_as_a_geometry():
    con = cc.construe("a supply voltage of 170 to 190 volts")
    assert con["relations"] == []
    assert con["words_alone"]


def test_a_tolerance_band_is_construed_as_substantially_equal():
    con = cc.construe("a projection width within approximately +/- 25% the thickness of the "
                      "ferromagnetic workpiece")
    kinds = [r["kind"] for r in con["relations"]]
    assert "tolerance" in kinds
    assert "substantially equal" in con["terms"]


# --------------------------------------------------------------------------- lexicography


def test_the_applicants_own_definition_is_found_and_pointed_the_other_way():
    defs = cc.definitions(SCHMALZ_SPEC)
    assert any("i.e" in d["cue"].lower() for d in defs)
    got = cc.definition_for(ANGLE_LIMITATION, defs)
    assert got, "the i.e. sentence defines this limitation"
    #  The claim used the NUMBER; the construction has to hand back the WORDS, because those are
    #  the half nothing has been searched for.
    assert "parallel" in got["construed_as"].lower()
    assert "displacement direction" in got["construed_as"].lower()


def test_a_definition_that_belongs_to_another_limitation_is_not_attached_to_this_one():
    defs = cc.definitions(SCHMALZ_SPEC)
    assert cc.definition_for("a housing having a handle for carrying the gripper", defs) is None


def test_construe_all_attaches_a_construction_to_every_limitation():
    lims = [{"id": "claim 1[e]", "text": ANGLE_LIMITATION},
            {"id": "claim 4[a]", "text": BEVEL_LIMITATION}]
    cc.construe_all(lims, SCHMALZ_SPEC)
    assert not lims[0]["construction"]["words_alone"]
    assert lims[1]["construction"]["words_alone"]


# --------------------------------------------------------------------------- the zero gate


def test_a_zero_on_a_construed_limitation_may_not_be_stated_flat():
    con = cc.construe(ANGLE_LIMITATION, cc.definitions(SCHMALZ_SPEC))
    assert cc.zero_is_confirmable(con) is False
    caveat = cc.zero_caveat(con)
    assert "not found" in caveat and "not disclosed" in caveat


def test_a_zero_on_an_ordinary_limitation_is_stated_flat():
    con = cc.construe("a housing enclosing the magnet assembly")
    assert cc.zero_is_confirmable(con) is True
    assert cc.zero_caveat(con) == ""


def test_once_the_construction_has_actually_been_searched_the_zero_stands():
    con = cc.construe(ANGLE_LIMITATION)
    assert cc.zero_is_confirmable(con) is False
    con["searched"] = True
    assert cc.zero_is_confirmable(con) is True


# --------------------------------------------------------------------------- into the portfolio


def test_the_construed_reading_enters_the_query_portfolio_first():
    """MAX_READINGS_TOTAL must never be what cuts the construction: a reading the model would
    have written anyway is the cheaper thing to lose."""
    lim = {"id": "claim 1[e]", "text": ANGLE_LIMITATION}
    cc.construe_all([lim], SCHMALZ_SPEC)
    model_readings = [{"thing": ["magnet"], "place": ["chuck"], "apparatus": ["lifting"]}]
    got = lq._construed_reading(lim, model_readings)
    assert got and got["construed"] is True
    assert "parallel" in got["thing"]
    #  The place comes from the model's reading: the construction changes WHAT is looked for, not
    #  where.
    assert got["place"] == ["chuck"]
    #  And the limitation now knows its concept reached the search.
    assert lim["construction"]["searched"] is True


def test_an_ordinary_limitation_adds_no_construed_reading():
    lim = {"id": "claim 2[a]", "text": "a housing enclosing the magnet assembly"}
    cc.construe_all([lim], SCHMALZ_SPEC)
    assert lq._construed_reading(lim, [{"thing": ["housing"], "place": ["magnet"]}]) is None


def test_construed_terms_survive_the_portfolios_own_term_hygiene():
    """Every term is matched literally against full text, so it has to pass `_terms`: one or two
    words, four characters or more, no leading preposition."""
    for relation, terms in cc.RELATION_TERMS.items():
        kept = lq._terms(terms)
        assert len(kept) >= 4, (relation, kept)


# --------------------------------------------------------------------------- the subject's spec


def test_the_construction_reads_the_specification_of_a_searched_publication(monkeypatch):
    """The lexicography only works if the specification is available, and a search started from a
    publication number brings no `disclosure_text` with it. That is exactly the Schmalz case: the
    "i.e." sentence that defines 170 to 190 degrees is in the corpus, not in the upload."""
    import deep_analysis
    import deep_rank
    monkeypatch.setattr(deep_analysis, "full_text",
                        lambda pub, **k: {"passages": [{"text": SCHMALZ_SPEC}]})
    got = deep_rank._subject_spec({"publication_number": "US-20250033224-A1"})
    assert "i.e." in got
    #  ...and an upload that carries its own text is not made to fetch anything.
    assert deep_rank._subject_spec({"disclosure_text": "x" * 500}) == "x" * 500


def test_a_subject_with_neither_a_specification_nor_a_number_still_construes_the_geometry():
    import deep_rank
    assert deep_rank._subject_spec({}) == ""
    con = cc.construe(ANGLE_LIMITATION, cc.definitions(""))
    assert [r["kind"] for r in con["relations"]] == [cc.PARALLEL]
