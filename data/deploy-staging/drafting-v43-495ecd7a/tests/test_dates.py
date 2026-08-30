"""Jurisdiction-neutral date/status engine tests (novelty vs inventive-step, public vs secret)."""
from datetime import date
from search_modes import (Mode, Subject, citable_where, classify_basis, usable_for, Basis,
                          CombinationBuilder, ElementMapping)

SUBJ = Subject(number="EP-3707092-B1", efd=date(2018, 9, 10), filing_date=date(2019, 9, 9),
               publication_date=date(2022, 3, 9), jurisdiction="EP")


def test_novelty_includes_secret_art_inventive_step_excludes_it():
    nf, _ = citable_where(Mode.NOVELTY, SUBJ)
    inf, _ = citable_where(Mode.INVENTIVE_STEP, SUBJ)
    # novelty admits earlier-filed-later-published (secret) art -> its clause references priority
    assert "earliest_priority_date" in nf or "filing_date" in nf
    # inventive step is public art only -> just a publication-date-before-EFD test
    assert "publication_date" in inf and "priority" not in inf.replace("earliest_priority_date", "")


def test_classify_basis_public_secret_notprior():
    public = {"publication_date": date(2015, 1, 1), "earliest_priority_date": date(2014, 1, 1)}
    secret = {"publication_date": date(2020, 1, 1), "earliest_priority_date": date(2018, 1, 1)}
    later = {"publication_date": date(2023, 1, 1), "earliest_priority_date": date(2021, 1, 1)}
    assert classify_basis(public, SUBJ) == Basis.PUBLIC_PRIOR_ART
    assert classify_basis(secret, SUBJ) == Basis.SECRET_PRIOR_ART
    assert classify_basis(later, SUBJ) == Basis.NOT_PRIOR_ART


def test_usable_for_secret_is_novelty_only():
    assert usable_for(Basis.SECRET_PRIOR_ART, Mode.NOVELTY) is True
    assert usable_for(Basis.SECRET_PRIOR_ART, Mode.INVENTIVE_STEP) is False
    assert usable_for(Basis.PUBLIC_PRIOR_ART, Mode.INVENTIVE_STEP) is True


def test_combination_set_cover():
    cb = CombinationBuilder(["pump", "seal", "handle", "sensor"])
    for el in ["pump", "seal", "handle"]:
        cb.add(ElementMapping(el, "US1", Basis.PUBLIC_PRIOR_ART, {}, 0.8))
    cb.add(ElementMapping("sensor", "EP2", Basis.PUBLIC_PRIOR_ART, {}, 0.7))
    combo = cb.combination()
    assert combo["primary"] == "US1"
    assert set(combo["covers"]) == {"pump", "seal", "handle"}
    assert combo["secondaries"] and combo["secondaries"][0]["ref"] == "EP2"
    assert combo["uncovered_elements"] == []
