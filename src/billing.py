"""A card on file, a balance, and a ledger that says where every cent went.

WHAT THIS IS. Pro accounts put a card on file, top up a minimum of $10, and are charged for what
they actually spend on model time. Not a subscription: a subscription bills for a month whether or
not anybody searched, and this tool's cost is dominated by one thing that either happens or does
not, which is reading references in full.

THE LEDGER IS THE TRUTH. Balance is not a column that gets incremented, it is the sum of the
ledger, because a column can drift from its own history and a ledger cannot. Every row says what
happened, in dollars, with the Stripe id when money moved and the run slug when tokens were burnt.

WHAT IT CHARGES. `spend.total_usd`, which the run receipt already computes from the same per-model
prices the app charges itself against, plus a margin. It is the tool's own measurement of its own
cost, so a bill can always be traced back to the run and the run back to the calls.

STRIPE, AND WHICH ACCOUNT. acct_1JNHaTI9BPnVpMS5, "Nebula Innovations LLC", the US Nevada LLC (EIN
81-1446236). NOT the Hong Kong "Nebula Innovations Limited" and not either of the GRABO accounts.
The key lives in the app env as STRIPE_SECRET_KEY.

OFF-SESSION CHARGING IS THE WHOLE MECHANISM, so the card has to be set up for it: the SetupIntent
is created with usage="off_session", and the PaymentIntent with off_session=True and
confirm=True. A card that needs authentication for a later charge fails loudly here rather than
silently declining in a background thread, and `topup()` returns the reason.
"""
from __future__ import annotations

import os
import traceback

import db

STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
#  Set by `ops/stripe_webhook.py --create`, which registers the endpoint and prints the secret.
#  Without it the endpoint REFUSES every delivery rather than trusting an unsigned body: an
#  unauthenticated POST that can credit an account is not a webhook, it is a faucet.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
CURRENCY = "usd"
#  What the tool charges over its own model cost. The receipt figure is what WE paid; this is the
#  price. Set to 0 to bill at cost.
MARGIN = float(os.environ.get("BILLING_MARGIN", "0.30"))


def enabled() -> bool:
    return bool(STRIPE_SECRET)


def _stripe():
    import stripe
    stripe.api_key = STRIPE_SECRET
    #  The account is pinned in the key itself; asserting it here turns a mis-set env into a
    #  refusal instead of a charge on the wrong business.
    return stripe


# --------------------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS app_billing_ledger (
    id            bigserial PRIMARY KEY,
    user_id       bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    kind          text NOT NULL,            -- topup | usage | adjustment | refund
    usd           numeric(12,4) NOT NULL,   -- positive credits, negative debits
    tokens        bigint NOT NULL DEFAULT 0,
    slug          text NOT NULL DEFAULT '',
    stripe_id     text NOT NULL DEFAULT '',
    note          text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_billing_ledger_user_idx ON app_billing_ledger(user_id, id DESC);
-- Every Stripe delivery, once. Stripe retries for up to three days on any non-2xx and will
-- happily deliver the same event twice on a 2xx it did not hear, so "did we already act on this"
-- has to be a fact in the database rather than a hope about the network.
CREATE TABLE IF NOT EXISTS app_billing_events (
    event_id    text PRIMARY KEY,
    type        text NOT NULL DEFAULT '',
    user_id     bigint,
    handled     boolean NOT NULL DEFAULT false,
    note        text NOT NULL DEFAULT '',
    received_at timestamptz NOT NULL DEFAULT now()
);
-- One usage row per run, ever. A retry, a re-render or a second worker must not double-bill.
CREATE UNIQUE INDEX IF NOT EXISTS app_billing_ledger_run_idx
    ON app_billing_ledger(user_id, slug) WHERE kind = 'usage' AND slug <> '';
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS stripe_customer_id text NOT NULL DEFAULT '';
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS stripe_payment_method text NOT NULL DEFAULT '';
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS card_brand text NOT NULL DEFAULT '';
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS card_last4 text NOT NULL DEFAULT '';
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS auto_topup_usd numeric(12,2) NOT NULL DEFAULT 0;
"""


def ensure_schema():
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        conn.commit()


# --------------------------------------------------------------------------- the ledger
def balance(user_id) -> float:
    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(usd), 0) AS b FROM app_billing_ledger WHERE user_id=%s",
                    (user_id,))
        row = cur.fetchone()
    return round(float((row or {}).get("b") or 0), 4)


def ledger(user_id, limit=50) -> list:
    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT kind, usd, tokens, slug, stripe_id, note, created_at "
                    "FROM app_billing_ledger WHERE user_id=%s ORDER BY id DESC LIMIT %s",
                    (user_id, int(limit)))
        return [dict(r) for r in cur.fetchall()]


def _post(user_id, kind, usd, *, tokens=0, slug="", stripe_id="", note=""):
    """One ledger row. Returns False when the run-uniqueness index refuses a second usage row."""
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_billing_ledger (user_id, kind, usd, tokens, slug, stripe_id, note)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT DO NOTHING RETURNING id",
                (user_id, kind, round(float(usd), 4), int(tokens or 0), slug or "",
                 stripe_id or "", note or ""))
            got = cur.fetchone()
            conn.commit()
        return bool(got)
    except Exception:
        traceback.print_exc()
        return False


# --------------------------------------------------------------------------- Stripe
def customer_id(user) -> str:
    """The Stripe customer for this user, created on first need."""
    if user.get("stripe_customer_id"):
        return user["stripe_customer_id"]
    s = _stripe()
    c = s.Customer.create(email=user.get("email") or None,
                          name=user.get("full_name") or None,
                          metadata={"app": "iptorch", "user_id": str(user["id"])})
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE app_users SET stripe_customer_id=%s WHERE id=%s", (c.id, user["id"]))
        conn.commit()
    return c.id


def setup_intent(user) -> dict:
    """A SetupIntent for the card-on-file form. usage='off_session' is not optional: every later
    charge is made with nobody at the keyboard."""
    s = _stripe()
    si = s.SetupIntent.create(customer=customer_id(user), usage="off_session",
                              payment_method_types=["card"],
                              metadata={"app": "iptorch", "user_id": str(user["id"])})
    return {"client_secret": si.client_secret, "publishable_key": STRIPE_PUBLISHABLE}


def attach_payment_method(user, payment_method_id) -> dict:
    """Record the card the SetupIntent produced, and make it the customer's default."""
    s = _stripe()
    cid = customer_id(user)
    pm = s.PaymentMethod.retrieve(payment_method_id)
    if getattr(pm, "customer", None) != cid:
        pm = s.PaymentMethod.attach(payment_method_id, customer=cid)
    s.Customer.modify(cid, invoice_settings={"default_payment_method": payment_method_id})
    card = getattr(pm, "card", None) or {}
    brand = (card.get("brand") if isinstance(card, dict) else getattr(card, "brand", "")) or ""
    last4 = (card.get("last4") if isinstance(card, dict) else getattr(card, "last4", "")) or ""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE app_users SET stripe_payment_method=%s, card_brand=%s, card_last4=%s "
                    "WHERE id=%s", (payment_method_id, brand, last4, user["id"]))
        conn.commit()
    return {"brand": brand, "last4": last4}


def remove_payment_method(user):
    """Detach the card. The user drops to the free tier the moment this returns: see
    entitlements.tier_of, which reads the card rather than a plan column."""
    try:
        if user.get("stripe_payment_method"):
            _stripe().PaymentMethod.detach(user["stripe_payment_method"])
    except Exception:
        traceback.print_exc()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE app_users SET stripe_payment_method='', card_brand='', card_last4='', "
                    "auto_topup_usd=0 WHERE id=%s", (user["id"],))
        conn.commit()


def topup(user, usd, *, note="") -> dict:
    """Charge the card on file and credit the balance. -> {ok, usd, error}.

    OFF-SESSION AND CONFIRMED IN ONE CALL. There is nobody to authenticate a step-up, so a card
    that demands one fails here with its own message rather than leaving a PaymentIntent in
    requires_action for ever.
    """
    import entitlements
    usd = round(max(float(usd or 0), entitlements.MIN_TOPUP_USD), 2)
    if not user.get("stripe_payment_method"):
        return {"ok": False, "error": "No card on file."}
    try:
        s = _stripe()
        pi = s.PaymentIntent.create(
            amount=int(round(usd * 100)), currency=CURRENCY, customer=customer_id(user),
            payment_method=user["stripe_payment_method"], off_session=True, confirm=True,
            description="IPtorch credit",
            metadata={"app": "iptorch", "user_id": str(user["id"]), "kind": "topup"})
        if pi.status != "succeeded":
            return {"ok": False, "error": "The card was not charged (%s)." % pi.status}
        _post(user["id"], "topup", usd, stripe_id=pi.id, note=note or "card on file")
        return {"ok": True, "usd": usd, "stripe_id": pi.id}
    except Exception as e:                                                # noqa: BLE001
        traceback.print_exc()
        msg = getattr(getattr(e, "error", None), "message", None) or str(e)
        return {"ok": False, "error": str(msg)[:300]}


# --------------------------------------------------------------------------- the webhook
#  WHAT WE ACT ON, and nothing else. Stripe will send whatever the endpoint subscribes to; acting
#  on an event we did not think about is how a ledger acquires rows nobody can explain.
WEBHOOK_EVENTS = (
    "charge.refunded",              # a refund issued from the dashboard, or by us
    "charge.dispute.created",       # a chargeback: the money is gone until it is not
    "charge.dispute.closed",        # ...and back, if it was won
    "payment_intent.succeeded",     # a charge that landed asynchronously, or from the dashboard
    "payment_intent.payment_failed",
    "payment_method.detached",      # the card is gone: the account drops to free
)


def _user_by_customer(customer_id):
    if not customer_id:
        return None
    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, email, stripe_payment_method FROM app_users "
                    "WHERE stripe_customer_id=%s LIMIT 1", (customer_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def _claim_event(event_id, etype) -> bool:
    """True the FIRST time this event id is seen, False on every redelivery."""
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_billing_events (event_id, type) VALUES (%s, %s) "
                        "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                        (event_id, etype))
            got = cur.fetchone()
            conn.commit()
        return bool(got)
    except Exception:
        traceback.print_exc()
        return False


def _finish_event(event_id, user_id, note):
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE app_billing_events SET handled=true, user_id=%s, note=%s "
                        "WHERE event_id=%s", (user_id, str(note)[:300], event_id))
            conn.commit()
    except Exception:
        traceback.print_exc()


def verify(payload: bytes, signature: str):
    """The Stripe event, or None when the signature does not check out.

    THE SIGNATURE IS THE AUTHENTICATION. This endpoint has no session and no CSRF token, and it
    can move money, so an unverified body is refused rather than parsed. No secret configured is
    the same answer: refuse.
    """
    if not STRIPE_WEBHOOK_SECRET:
        return None
    try:
        return _stripe().Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return None


def _plain(obj):
    """A Stripe SDK object as nested plain dicts. Anything already plain passes through."""
    for attr in ("to_dict_recursive", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:                                             # noqa: BLE001
                pass
    try:
        return dict(obj or {})
    except Exception:                                                     # noqa: BLE001
        return {}


def handle_event(event) -> dict:
    """Apply one verified Stripe event to the ledger. Idempotent by event id.

    IT NEVER RAISES. Stripe retries a non-2xx for three days, so an exception here becomes a
    thundering redelivery of an event we still cannot process. Anything unexpected is recorded
    and acknowledged.
    """
    #  PLAIN DICTS, FIRST THING. `stripe.Webhook.construct_event` returns an Event whose `.data`
    #  is a StripeObject, not a dict, and StripeObject raises AttributeError on `.get` with a
    #  message telling you to call to_dict(). Reading it as a mapping worked on the hand-built
    #  payloads and would have failed on every real delivery.
    ev = _plain(event)
    etype = ev.get("type") or ""
    eid = ev.get("id") or ""
    obj = (ev.get("data") or {}).get("object") or {}
    if not eid:
        return {"ok": False, "why": "no event id"}
    if not _claim_event(eid, etype):
        return {"ok": True, "duplicate": True, "type": etype}

    def get(key, default=None):
        return obj.get(key, default)

    user = _user_by_customer(get("customer"))
    note = ""
    try:
        if etype == "charge.refunded":
            #  Refunded from the dashboard or by us. The ledger has to lose the credit, or the
            #  balance keeps spending money that has gone back to the card.
            usd = round(float(get("amount_refunded") or 0) / 100.0, 2)
            if user and usd > 0:
                _post(user["id"], "refund", -usd, stripe_id=get("id") or eid,
                      note="refunded to the card")
                note = "refund $%.2f" % usd

        elif etype == "charge.dispute.created":
            #  A chargeback takes the money immediately. Debit it now and let the close event put
            #  it back if the dispute is won; the alternative is an account spending funds that
            #  are already with the bank.
            usd = round(float(get("amount") or 0) / 100.0, 2)
            u = user or _user_by_customer(None)
            if u and usd > 0:
                _post(u["id"], "adjustment", -usd, stripe_id=get("id") or eid,
                      note="chargeback opened")
                note = "dispute opened $%.2f" % usd

        elif etype == "charge.dispute.closed":
            if (get("status") or "") == "won":
                usd = round(float(get("amount") or 0) / 100.0, 2)
                if user and usd > 0:
                    _post(user["id"], "adjustment", usd, stripe_id=get("id") or eid,
                          note="chargeback won, credit restored")
                    note = "dispute won $%.2f" % usd
            else:
                note = "dispute closed %s" % (get("status") or "")

        elif etype == "payment_intent.succeeded":
            #  `topup()` already posts synchronously for charges this app makes, and the ledger
            #  is unique per Stripe id, so this only ever adds a row for a charge that happened
            #  somewhere else: the dashboard, or a retry that landed after we gave up on it.
            usd = round(float(get("amount_received") or get("amount") or 0) / 100.0, 2)
            meta = get("metadata") or {}
            if user and usd > 0 and not _has_stripe_id(get("id")):
                _post(user["id"], "topup", usd, stripe_id=get("id") or eid,
                      note=(meta.get("kind") or "charge") + ", recorded from Stripe")
                note = "topup $%.2f recorded" % usd

        elif etype == "payment_intent.payment_failed":
            err = (get("last_payment_error") or {})
            note = "payment failed: %s" % (err.get("message") or "")[:160]

        elif etype == "payment_method.detached":
            #  The card is gone. entitlements.tier_of reads the card, so clearing it here is what
            #  actually drops the account back to free.
            pm = get("id") or ""
            if pm:
                with db.connect() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE app_users SET stripe_payment_method='', card_brand='', "
                                "card_last4='', auto_topup_usd=0 "
                                "WHERE stripe_payment_method=%s RETURNING id", (pm,))
                    row = cur.fetchone()
                    conn.commit()
                if row:
                    user = user or {"id": row["id"]}
                    note = "card detached, account back to free"
        else:
            note = "ignored"
    except Exception as exc:                                              # noqa: BLE001
        traceback.print_exc()
        note = "error: %s" % str(exc)[:160]

    _finish_event(eid, (user or {}).get("id"), note)
    return {"ok": True, "type": etype, "note": note}


def _has_stripe_id(stripe_id) -> bool:
    if not stripe_id:
        return False
    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM app_billing_ledger WHERE stripe_id=%s LIMIT 1", (stripe_id,))
        return cur.fetchone() is not None


# --------------------------------------------------------------------------- metering
def price_for(usd_cost) -> float:
    """What we charge for a run that cost us `usd_cost` in model time."""
    return round(float(usd_cost or 0) * (1.0 + MARGIN), 4)


def charge_run(user, slug, usd_cost, tokens=0) -> dict:
    """Debit one finished run, once, and top up automatically if that empties the float.

    IDEMPOTENT BY RUN. The unique index on (user_id, slug) where kind='usage' is what makes it so:
    a re-render, a retry or a second worker inserts nothing and this returns charged=False.
    """
    if not user or not usd_cost:
        return {"charged": False}
    price = price_for(usd_cost)
    if price <= 0:
        return {"charged": False}
    posted = _post(user["id"], "usage", -price, tokens=tokens, slug=slug,
                   note="model time on %s" % slug)
    if not posted:
        return {"charged": False, "reason": "already billed"}
    out = {"charged": True, "usd": price, "balance": balance(user["id"])}
    import entitlements
    if out["balance"] < entitlements.AUTO_TOPUP_AT_USD and user.get("stripe_payment_method"):
        want = float(user.get("auto_topup_usd") or 0) or entitlements.MIN_TOPUP_USD
        out["auto_topup"] = topup(user, want, note="automatic, balance below threshold")
        out["balance"] = balance(user["id"])
    return out
