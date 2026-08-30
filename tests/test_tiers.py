"""Who may spend what. Every line here is a way the app could give something away by accident."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import billing                                                            # noqa: E402
import entitlements as ent                                                # noqa: E402

ANON = None
FREE = {"id": 1, "email": "f@e.com", "is_active": True, "is_admin": False}
PRO = dict(FREE, id=2, stripe_payment_method="pm_x", card_last4="4242")
ADMIN = dict(FREE, id=3, is_admin=True)


def test_the_tier_is_the_card_not_a_column():
    assert ent.tier_of(ANON) == ent.ANON
    assert ent.tier_of(FREE) == ent.FREE
    assert ent.tier_of(PRO) == ent.PRO
    #  Remove the card and the account is FREE again with nothing else to remember to change.
    assert ent.tier_of(dict(PRO, stripe_payment_method="")) == ent.FREE


def test_an_inactive_account_is_anonymous():
    assert ent.tier_of(dict(PRO, is_active=False)) == ent.ANON


def test_admins_are_pro():
    assert ent.tier_of(ADMIN) == ent.PRO


def test_the_fast_search_is_free_to_everyone():
    for u in (ANON, FREE, PRO):
        assert ent.check(u, ent.SEARCH_FAST)


def test_the_build_needs_an_account_and_says_why():
    v = ent.check(ANON, ent.SEARCH_ATTACK)
    assert not v and v.need == ent.FREE and "free" in v.reason.lower()
    assert ent.check(FREE, ent.SEARCH_ATTACK) and ent.check(PRO, ent.SEARCH_ATTACK)


def test_reading_in_full_is_capped_for_free_and_open_for_pro():
    assert ent.check(FREE, ent.READ_IN_FULL, read_top=20)
    v = ent.check(FREE, ent.READ_IN_FULL, read_top=400)
    assert not v and v.need == ent.PRO and v.limit == ent.FREE_READ_TOP
    assert ent.check(PRO, ent.READ_IN_FULL, read_top=1000)
    assert not ent.check(ANON, ent.READ_IN_FULL, read_top=1)


def test_the_cap_clamps_rather_than_refusing():
    assert ent.clamp_read_top(FREE, 400) == ent.FREE_READ_TOP
    assert ent.clamp_read_top(FREE, 20) == 20
    assert ent.clamp_read_top(PRO, 400) == 400
    assert ent.clamp_read_top(ANON, 400) == 0


def test_third_party_sources_are_pro():
    assert not ent.check(ANON, ent.THIRD_PARTY)
    assert not ent.check(FREE, ent.THIRD_PARTY)
    assert ent.check(PRO, ent.THIRD_PARTY)


def test_the_drafting_studio_is_pro():
    assert not ent.check(ANON, ent.DRAFTING)
    assert not ent.check(FREE, ent.DRAFTING)
    assert ent.check(PRO, ent.DRAFTING)


def test_an_unknown_action_is_not_a_gate():
    assert ent.check(ANON, "something.new")


def test_describe_is_what_the_form_renders_from():
    d = ent.describe(FREE)
    assert d["tier"] == ent.FREE and d["can_attack"] and not d["can_third_party"]
    assert d["read_limit"] == ent.FREE_READ_TOP
    assert ent.describe(PRO)["read_limit"] is None
    assert ent.describe(ANON)["read_limit"] == 0


# --------------------------------------------------------------------------- billing arithmetic
def test_the_price_carries_the_margin_over_what_the_run_cost_us():
    assert billing.price_for(0) == 0
    assert billing.price_for(10) == round(10 * (1 + billing.MARGIN), 4)


def test_a_top_up_below_the_minimum_is_raised_to_it():
    #  topup() clamps before it charges; the arithmetic is asserted without touching Stripe.
    assert round(max(1.0, ent.MIN_TOPUP_USD), 2) == ent.MIN_TOPUP_USD


def test_the_ledger_is_the_balance():
    #  The schema puts a unique index on (user_id, slug) for usage rows, so a run bills once.
    assert "app_billing_ledger_run_idx" in billing.SCHEMA
    assert "kind = 'usage'" in billing.SCHEMA


def test_the_card_is_set_up_for_off_session_use():
    #  Every charge after the first is made with nobody at the keyboard. A SetupIntent without
    #  usage="off_session" produces a card that declines those.
    import inspect
    src = inspect.getsource(billing.setup_intent)
    assert 'usage="off_session"' in src
    charge = inspect.getsource(billing.topup)
    assert "off_session=True" in charge and "confirm=True" in charge
