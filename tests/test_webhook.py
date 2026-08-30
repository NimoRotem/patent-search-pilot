"""The webhook, signed exactly as Stripe signs it, against the real route.

Every case here is a way a webhook endpoint that moves money can go wrong: an unsigned body, a
replay, an event for a DIFFERENT product on the same Stripe account, and an event we do not
handle. None of them may change a balance.
"""
import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import billing                                                            # noqa: E402
import db                                                                 # noqa: E402
import webapp                                                             # noqa: E402

SECRET = billing.STRIPE_WEBHOOK_SECRET
CUSTOMER = "cus_test_webhook_iptorch"


def _sign(payload: bytes) -> str:
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), (ts + "." + payload.decode()).encode(),
                   hashlib.sha256).hexdigest()
    return "t=%s,v1=%s" % (ts, sig)


def _post(body: dict, signature=None):
    raw = json.dumps(body).encode()
    c = webapp.app.test_client()
    return c.post("/billing/webhook", data=raw,
                  headers={"Stripe-Signature": signature if signature is not None else _sign(raw),
                           "Content-Type": "application/json"})


def _event(eid, etype, obj):
    return {"id": eid, "type": etype, "object": "event", "data": {"object": obj}}


def _user():
    """A throwaway account with a Stripe customer id, created once."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM app_users WHERE email=%s", ("webhook-test@iptorch.local",))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO app_users (email, full_name, password_hash, "
                        "stripe_customer_id, stripe_payment_method, card_last4) "
                        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                        ("webhook-test@iptorch.local", "Webhook Test", "x", CUSTOMER,
                         "pm_test_webhook", "4242"))
            row = cur.fetchone()
        else:
            cur.execute("UPDATE app_users SET stripe_customer_id=%s, stripe_payment_method=%s "
                        "WHERE id=%s", (CUSTOMER, "pm_test_webhook", row["id"]))
        conn.commit()
    return row["id"]


def _clear(uid):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_billing_ledger WHERE user_id=%s", (uid,))
        cur.execute("DELETE FROM app_billing_events WHERE user_id=%s OR event_id LIKE 'evt_test%%'",
                    (uid,))
        conn.commit()


# ------------------------------------------------------------------ authentication
def test_an_unsigned_body_is_refused():
    r = _post(_event("evt_test_unsigned", "charge.refunded", {}), signature="")
    assert r.status_code == 400


def test_a_forged_signature_is_refused():
    r = _post(_event("evt_test_forged", "charge.refunded", {}), signature="t=1,v1=deadbeef")
    assert r.status_code == 400
    #  ...and nothing was recorded, so a forger cannot even burn an event id.
    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM app_billing_events WHERE event_id='evt_test_forged'")
        assert cur.fetchone() is None


# ------------------------------------------------------------------ the ledger stays true
def test_a_refund_takes_the_credit_back():
    uid = _user(); _clear(uid)
    billing._post(uid, "topup", 25.0, stripe_id="pi_test_refund", note="test credit")
    assert billing.balance(uid) == 25.0
    r = _post(_event("evt_test_refund", "charge.refunded",
                     {"id": "ch_test_1", "customer": CUSTOMER, "amount_refunded": 1000}))
    assert r.status_code == 200 and r.get_json()["ok"]
    assert billing.balance(uid) == 15.0
    _clear(uid)


def test_a_chargeback_debits_and_winning_it_credits_back():
    uid = _user(); _clear(uid)
    billing._post(uid, "topup", 50.0, stripe_id="pi_test_dispute", note="test credit")
    _post(_event("evt_test_dispute_open", "charge.dispute.created",
                 {"id": "dp_test_1", "customer": CUSTOMER, "amount": 5000}))
    assert billing.balance(uid) == 0.0
    _post(_event("evt_test_dispute_won", "charge.dispute.closed",
                 {"id": "dp_test_1", "customer": CUSTOMER, "amount": 5000, "status": "won"}))
    assert billing.balance(uid) == 50.0
    _clear(uid)


def test_a_lost_chargeback_stays_debited():
    uid = _user(); _clear(uid)
    billing._post(uid, "topup", 50.0, stripe_id="pi_test_lost", note="test credit")
    _post(_event("evt_test_lost_open", "charge.dispute.created",
                 {"id": "dp_test_2", "customer": CUSTOMER, "amount": 5000}))
    _post(_event("evt_test_lost_closed", "charge.dispute.closed",
                 {"id": "dp_test_2", "customer": CUSTOMER, "amount": 5000, "status": "lost"}))
    assert billing.balance(uid) == 0.0
    _clear(uid)


def test_a_replay_changes_nothing():
    """Stripe redelivers for three days on any non-2xx, and sometimes on a 2xx it did not hear."""
    uid = _user(); _clear(uid)
    billing._post(uid, "topup", 30.0, stripe_id="pi_test_replay", note="test credit")
    ev = _event("evt_test_replay", "charge.refunded",
                {"id": "ch_test_replay", "customer": CUSTOMER, "amount_refunded": 500})
    _post(ev)
    after_first = billing.balance(uid)
    for _ in range(3):
        again = _post(ev)
        assert again.get_json().get("duplicate") is True
    assert billing.balance(uid) == after_first == 25.0
    _clear(uid)


def test_a_detached_card_drops_the_account_to_free():
    import entitlements
    uid = _user(); _clear(uid)
    _post(_event("evt_test_detach", "payment_method.detached", {"id": "pm_test_webhook"}))
    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT stripe_payment_method, card_last4 FROM app_users WHERE id=%s", (uid,))
        row = dict(cur.fetchone())
    assert row["stripe_payment_method"] == "" and row["card_last4"] == ""
    assert entitlements.tier_of(dict(row, id=uid, is_active=True)) == entitlements.FREE
    _clear(uid)


# ------------------------------------------------------------------ the shared-account hazard
def test_an_event_for_another_product_on_this_account_is_ignored():
    """acct_1JNHaT also serves phoneline.ai, birthdaybuddyai and the iptorch.com Laravel app, and
    Stripe delivers every subscribed event type to EVERY endpoint on the account. A charge made
    by one of those must not credit or debit anybody here."""
    uid = _user(); _clear(uid)
    r = _post(_event("evt_test_foreign", "payment_intent.succeeded",
                     {"id": "pi_someone_else", "customer": "cus_not_ours", "amount": 9900}))
    assert r.status_code == 200
    assert billing.balance(uid) == 0.0
    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM app_billing_ledger WHERE stripe_id='pi_someone_else'")
        assert cur.fetchone() is None
    _clear(uid)


def test_a_charge_this_app_already_posted_is_not_posted_twice():
    """topup() records synchronously; the webhook for the same PaymentIntent must add nothing."""
    uid = _user(); _clear(uid)
    billing._post(uid, "topup", 10.0, stripe_id="pi_already_here", note="synchronous")
    _post(_event("evt_test_dup_pi", "payment_intent.succeeded",
                 {"id": "pi_already_here", "customer": CUSTOMER, "amount_received": 1000}))
    assert billing.balance(uid) == 10.0
    _clear(uid)


def test_an_event_type_we_do_not_handle_is_acknowledged_and_ignored():
    uid = _user(); _clear(uid)
    r = _post(_event("evt_test_unknown", "invoice.created", {"customer": CUSTOMER}))
    assert r.status_code == 200 and r.get_json()["note"] == "ignored"
    assert billing.balance(uid) == 0.0
    _clear(uid)
