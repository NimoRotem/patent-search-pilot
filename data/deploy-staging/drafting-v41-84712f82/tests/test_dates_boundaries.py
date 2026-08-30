"""Exhaustive boundary tests for the legal dating/basis engine (M8 §1).
RULE: same-day-as-EFD publication is NOT prior art; public = strictly-before-EFD; secret =
filed-strictly-before-EFD AND published-strictly-after-EFD. Locks in the priority-date fix."""
from datetime import date
from search_modes import Mode, Subject, classify_basis, usable_for, Basis, citable_where

# subject: priority (EFD) 2018-09-10, filing 2018-09-10 (no priority interval), granted 2022-03-09
S = Subject(number="EP-X", efd=date(2018, 9, 10), filing_date=date(2018, 9, 10),
            publication_date=date(2022, 3, 9), jurisdiction="EP")
EFD = date(2018, 9, 10)


def cb(pub, prio=None, filing=None):
    return classify_basis({"publication_date": pub, "earliest_priority_date": prio,
                           "filing_date": filing}, S)


# ---- publication-date boundary vs EFD ------------------------------------------------------
def test_published_day_before_efd_is_public():
    assert cb(date(2018, 9, 9), prio=date(2010, 1, 1)) == Basis.PUBLIC_PRIOR_ART


def test_published_exactly_on_efd_is_NOT_prior_art():
    """THE fix: same-day-as-priority publication is NOT prior art (was wrongly SECRET)."""
    assert cb(date(2018, 9, 10), prio=date(2010, 1, 1)) == Basis.NOT_PRIOR_ART
    assert cb(date(2018, 9, 10), prio=None) == Basis.NOT_PRIOR_ART


def test_published_day_after_efd_with_earlier_filing_is_secret():
    assert cb(date(2018, 9, 11), prio=date(2010, 1, 1)) == Basis.SECRET_PRIOR_ART


def test_published_day_after_efd_without_earlier_filing_is_not_prior_art():
    assert cb(date(2018, 9, 11), prio=date(2019, 1, 1)) == Basis.NOT_PRIOR_ART  # filed after
    assert cb(date(2018, 9, 11), prio=None) == Basis.NOT_PRIOR_ART              # can't establish


# ---- reference filing-date boundary (secret art requires filing STRICTLY before EFD) --------
def test_ref_filed_day_before_efd_published_after_is_secret():
    assert cb(date(2020, 1, 1), prio=date(2018, 9, 9)) == Basis.SECRET_PRIOR_ART


def test_ref_filed_exactly_on_efd_is_not_secret():
    assert cb(date(2020, 1, 1), prio=date(2018, 9, 10)) == Basis.NOT_PRIOR_ART


def test_ref_filed_after_efd_is_not_prior_art():
    assert cb(date(2020, 1, 1), prio=date(2018, 9, 11)) == Basis.NOT_PRIOR_ART


# ---- missing dates -------------------------------------------------------------------------
def test_missing_publication_date_is_not_prior_art():
    assert cb(None, prio=date(2010, 1, 1)) == Basis.NOT_PRIOR_ART


def test_missing_subject_efd_is_not_prior_art():
    s2 = Subject(number="X", efd=None, filing_date=None, publication_date=None)
    assert classify_basis({"publication_date": date(2000, 1, 1)}, s2) == Basis.NOT_PRIOR_ART


def test_filing_date_used_when_priority_missing():
    # prio missing -> falls back to filing_date for the secret test
    assert cb(date(2020, 1, 1), prio=None, filing=date(2018, 9, 9)) == Basis.SECRET_PRIOR_ART


# ---- priority interval (subject claims priority earlier than its filing) --------------------
def test_priority_interval_strictly_inside():
    s = Subject(number="Y", efd=date(2018, 1, 1), filing_date=date(2018, 9, 10),
                publication_date=date(2022, 1, 1))
    inside = classify_basis({"publication_date": date(2018, 6, 1),
                             "earliest_priority_date": date(2019, 1, 1)}, s)
    assert inside == Basis.PRIORITY_INTERVAL
    # exactly on the subject's priority date -> NOT prior art (same-day rule holds)
    on_prio = classify_basis({"publication_date": date(2018, 1, 1),
                              "earliest_priority_date": date(2019, 1, 1)}, s)
    assert on_prio == Basis.NOT_PRIOR_ART


# ---- reference in the subject's own family (same priority) ----------------------------------
def test_own_family_same_priority_is_not_prior_art():
    # the subject's own later-published member: same priority as the subject, published after EFD
    assert cb(date(2022, 3, 9), prio=EFD) == Basis.NOT_PRIOR_ART


# ---- usable_for: secret art is novelty-only ------------------------------------------------
def test_secret_art_novelty_only():
    assert usable_for(Basis.SECRET_PRIOR_ART, Mode.NOVELTY) is True
    assert usable_for(Basis.SECRET_PRIOR_ART, Mode.INVENTIVE_STEP) is False
    assert usable_for(Basis.PUBLIC_PRIOR_ART, Mode.INVENTIVE_STEP) is True
    assert usable_for(Basis.PRIORITY_INTERVAL, Mode.NOVELTY) is False


# ---- SQL filter agrees with classify_basis (strictly-after for secret) ---------------------
def test_citable_where_secret_is_strictly_after():
    frag, params = citable_where(Mode.NOVELTY, S)
    assert "publication_date > %s" in frag       # strictly after (not >=)
    inf, _ = citable_where(Mode.INVENTIVE_STEP, S)
    assert "publication_date < %s" in inf and ">" not in inf.split("<")[1][:3]
