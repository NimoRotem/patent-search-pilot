"""What must be true before a 1.290 paper ships. Every test anchors on the refusal.

A submission that argues the merits is DISCARDED rather than corrected, and one that cites a
document which is not prior art wastes a slot and credibility. These are the checks that decide
what leaves the building, so they are tested for what they BLOCK, not what they let through.
"""
import datetime

import pytest

import submission_compliance as sc


def _doc(pub="US-11413727-B2", pub_date="2022-08-16", prio="2018-05-09", assignee="",
         rows=None, family=None):
    return {"n": 1, "pub": pub, "family_id": family,
            "biblio": {"pub": pub, "publication_date": pub_date, "priority_date": prio,
                       "filing_date": prio, "assignee": assignee, "title": "Vacuum Gripper"},
            "summary": "This document discloses a gripper.",
            "rows": rows if rows is not None else [
                {"claim_no": 1, "strong": True, "quote": "a base element 141 with openings",
                 "note": "n", "disclosure": "The reference discloses a base element.",
                 "cites": ["Paragraph [0012]"]}]}


# ------------------------------------------------------------------ prior-art qualification


def test_a_document_published_after_the_target_is_blocked():
    """The whole submission fails if it cites something that is not prior art at all."""
    d = _doc(pub_date="2026-01-01", prio="2025-06-01")
    out = sc.qualify(d, datetime.date(2021, 4, 20))
    assert out["blocked"] is True
    assert "not prior art" in out["note"]


def test_a_document_published_before_the_target_is_public_prior_art():
    d = _doc(pub_date="2002-07-16", prio="2001-02-26")
    out = sc.qualify(d, datetime.date(2021, 4, 20))
    assert out["basis"] == "public_prior_art" and out["blocked"] is False


def test_earlier_filed_later_published_is_flagged_as_novelty_only():
    """102(a)(2) / Art 54(3) art may not be used for obviousness, and the paper must say so."""
    d = _doc(pub_date="2022-08-16", prio="2018-05-09")
    out = sc.qualify(d, datetime.date(2021, 4, 20))
    assert out["basis"] == "secret_prior_art"
    assert "novelty only" in out["note"]


def test_a_missing_target_date_does_not_silently_pass_as_checked():
    out = sc.qualify(_doc(), None)
    assert out["basis"] == "unknown" and "Confirm it before filing" in out["note"]


# ------------------------------------------------------------------ self-collision, 102(b)(2)(C)


def test_a_commonly_owned_secret_reference_is_reported_as_dead_in_the_us_and_lethal_in_europe():
    d = _doc(assignee="J. Schmalz GmbH")
    d["compliance"] = {"qualify": {"basis": "secret_prior_art"}}
    out = sc.self_collision(d, ["J Schmalz GmbH"])
    assert out and out["same_owner"] is True and out["us_disqualified"] is True
    assert "102(b)(2)(C)" in out["note"]
    #  The exception is US-only; the EPO half is the part a US-trained reader would miss.
    assert "EPO" in out["note"] and "54(3)" in out["note"]


def test_a_commonly_owned_but_PUBLIC_reference_is_not_claimed_to_be_disqualified():
    """102(b)(2)(C) reaches 102(a)(2) art only. Over-claiming it would drop good art."""
    d = _doc(assignee="J. Schmalz GmbH")
    d["compliance"] = {"qualify": {"basis": "public_prior_art"}}
    out = sc.self_collision(d, ["J. Schmalz GmbH"])
    assert out["us_disqualified"] is False
    assert "does not reach it" in out["note"]


def test_an_unrelated_owner_is_not_a_self_collision():
    d = _doc(assignee="Dexterity Inc")
    assert sc.self_collision(d, ["J. Schmalz GmbH"]) is None


def test_corporate_suffixes_do_not_hide_a_self_collision():
    d = _doc(assignee="Schmalz GmbH & Co. KG")
    assert sc.self_collision(d, ["J. Schmalz AG"]) is not None


# ------------------------------------------------------------------ family collapse


def test_two_members_of_one_family_collapse_to_the_stronger():
    a = _doc(pub="DE-102019-A1", family="F1", rows=[{"claim_no": 1, "strong": False, "quote": "",
                                                     "note": "", "cites": []}])
    b = _doc(pub="EP-3000000-A1", family="F1")          # one strong row
    kept, notes = sc.collapse_families([a, b])
    assert [d["pub"] for d in kept] == ["EP-3000000-A1"]
    assert notes and "same DOCDB family" in notes[0] and "DE-102019-A1" in notes[0]


def test_documents_without_a_family_are_never_merged():
    a, b = _doc(pub="US-1-A"), _doc(pub="US-2-A")
    kept, notes = sc.collapse_families([a, b])
    assert len(kept) == 2 and notes == []


# ------------------------------------------------------------------ neutral language


@pytest.mark.parametrize("bad", [
    "The reference reads on claim 1.",
    "This anticipates the claimed invention.",
    "It would have been obvious to combine these.",
    "The claim is not novel over this document.",
    "The reference teaches away from the recited arrangement.",
    "This renders obvious the recited step.",
])
def test_argument_is_removed_because_a_submission_that_argues_is_discarded(bad):
    clean, changed = sc.neutralise(bad)
    assert changed, bad
    low = clean.lower()
    for word in ("reads on", "anticipat", "obvious", "not novel", "teaches away"):
        assert word not in low, "%s -> %s" % (bad, clean)


def test_a_concluding_sentence_about_patentability_is_deleted_outright():
    text = ("The reference discloses a base element. Therefore claim 1 is not patentable over "
            "this document.")
    clean, changed = sc.neutralise(text)
    assert "patentab" not in clean.lower()
    assert "base element" in clean
    assert changed


def test_neutral_text_is_left_alone():
    text = "The reference discloses a base element 141 with openings around its periphery."
    clean, changed = sc.neutralise(text)
    assert clean == text and changed == []


# ------------------------------------------------------------------ quotations


def test_a_quotation_the_source_does_not_contain_is_dropped_not_softened():
    d = _doc(rows=[{"claim_no": 1, "strong": True, "cites": ["Paragraph [0012]"],
                    "quote": "a completely invented passage about turbines", "note": "n",
                    "disclosure": "d"}])
    out = sc.verify_quotes(d, "The gripper has a base element 141 with peripheral openings.")
    assert out["dropped"] == 1
    assert d["rows"][0]["quote"] == ""
    assert d["rows"][0]["quote_unverified"] is True
    #  The finding survives; only the representation that the document SAYS this is withdrawn.
    assert d["rows"][0]["disclosure"] == "d" and d["rows"][0]["cites"] == ["Paragraph [0012]"]


def test_a_real_quotation_survives_even_with_the_readers_ellipsis():
    q = "The gripper has a base element 141 with peripheral openings …"
    d = _doc(rows=[{"claim_no": 1, "strong": True, "quote": q, "note": "n", "disclosure": "d",
                    "cites": []}])
    out = sc.verify_quotes(d, "In one embodiment the gripper has a base element 141 with "
                              "peripheral openings around its rim.")
    assert out["dropped"] == 0 and d["rows"][0]["quote"] == q


def test_no_stored_text_is_reported_rather_than_treated_as_verified():
    d = _doc()
    out = sc.verify_quotes(d, "")
    assert out["checked"] == 0 and "could not be re-verified" in out["note"]
    assert d["rows"][0]["quote"], "the quote must not be silently dropped for lack of a source"


# ------------------------------------------------------------------ language detection


@pytest.mark.parametrize("text,expected", [
    ("Die Vorrichtung ist mit einer Saugglocke und das Verfahren wird", "de"),
    ("真空吸盘装置及其控制方法", "CJK"),
    ("The gripper has a base element with peripheral openings", ""),
])
def test_relied_on_passages_that_need_a_translation_are_detected(text, expected):
    assert sc.needs_translation(text) == expected


# ------------------------------------------------------------------ placeholder parties


def test_an_unnamed_owner_on_both_sides_is_not_common_ownership():
    """"Individual" is what an assignee field says when nobody is named.

    Two documents both carrying it are not commonly owned, they are both unattributed, and
    reporting a self-collision sends the practitioner to verify a relationship that does not
    exist. Observed as a false positive on EP-2390518-A1.
    """
    d = _doc(assignee="Individual")
    assert sc.self_collision(d, ["Individual"]) is None
    for word in ("Unassigned", "N/A", "None", "unknown"):
        assert sc.self_collision(_doc(assignee=word), [word]) is None, word


def test_a_short_token_does_not_substring_match_everything():
    d = _doc(assignee="ABB")
    assert sc.self_collision(d, ["Dexterity Inc"]) is None


def test_a_real_shared_owner_still_matches_through_a_placeholder_in_the_list():
    d = _doc(assignee="Individual, J. Schmalz GmbH")
    assert sc.self_collision(d, ["Individual", "J Schmalz GmbH"]) is not None


# ------------------------------------------------------------------ foreign-language provenance


def test_a_chinese_document_says_its_quotes_are_a_translation_even_when_already_english():
    """The corpus holds English text for CN/JP/KR, so nothing needs translating at build time.

    The passage is still a translation of a foreign-language document, and 1.290(d)(3) wants that
    on the paper rather than inferred from the country code.
    """
    d = _doc(pub="CN-112828797-B")
    d["rows"][0]["quote"] = "The suction device body is a hard body."
    out = sc.translate_rows(d)
    assert out.get("pre_translated") is True
    assert "Chinese-language document" in out["note"]
    assert "verified translation" in out["note"]


def test_a_us_document_makes_no_translation_claim():
    d = _doc(pub="US-11413727-B2")
    assert sc.translate_rows(d) == {"translated": 0}


def test_the_office_code_drives_the_language_name():
    assert sc.source_language("DE-102019-A1") == "German"
    assert sc.source_language("JP-2020-A") == "Japanese"
    assert sc.source_language("EP-3000000-A1") == ""
