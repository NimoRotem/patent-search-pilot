"""Who may do what, in one place, and the reason each limit exists.

THREE TIERS, AND THE LINE BETWEEN THEM IS COST, NOT FEATURES-FOR-THE-SAKE-OF-IT.

    ANON   not signed in. The prior-art search, and nothing else. It is 16 seconds of our own
           corpus and no model reading, so it costs almost nothing to give away and it is the
           thing somebody has to be able to try before deciding whether this tool is for them.
    FREE   signed in. Everything above, plus the third-party build, capped at reading 20
           references in full. Twenty is roughly $2 of model spend at the measured rate, which is
           a real trial of the claim attack rather than a teaser of it.
    PRO    a card on file and a positive balance. No read cap beyond the ladder's own ceiling,
           the drafting studio, and the outside patent APIs. Charged by what it actually spends.

WHY THE CAP IS ON READING AND NOT ON SEARCHES. Measured on this corpus: retrieval is 16 seconds
and a few tenths of a cent, and reading is 78% of a submission run's wall clock and essentially
all of its bill (one run: $16.11, of which $15.89 was 2,030 model calls reading references). A cap
on searches would ration the cheap thing; a cap on reading rations the expensive one.

EVERY GATE GOES THROUGH `check()`. A limit enforced in a template and not in the route is not a
limit, and one enforced in two places drifts. `check()` returns a Verdict that carries the reason
in the words the user should see, so the route, the API and the page all say the same thing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

ANON = "anon"
FREE = "free"
PRO = "pro"
TIERS = (ANON, FREE, PRO)

LABEL = {ANON: "Not signed in", FREE: "Free account", PRO: "Pro"}

#  What a free account may read in full in one run. See the module docstring for why 20.
FREE_READ_TOP = int(os.environ.get("FREE_READ_TOP", "20"))
#  The floor a Pro account tops up to, and the minimum charge. Stripe's own minimum is $0.50; ten
#  dollars is roughly one full submission run at the measured rate, so it is a meaningful float
#  rather than a number that needs topping up mid-run.
MIN_TOPUP_USD = float(os.environ.get("MIN_TOPUP_USD", "10"))
#  Below this, the next usage debit triggers an automatic top-up on the card on file.
AUTO_TOPUP_AT_USD = float(os.environ.get("AUTO_TOPUP_AT_USD", "2"))

#  The actions the app gates. Named for what the user is trying to do, not for the route.
SEARCH_FAST = "search.fast"
SEARCH_ATTACK = "search.attack"
READ_IN_FULL = "read.in_full"
THIRD_PARTY = "sources.third_party"
DRAFTING = "drafting"
EMAIL_ON_DONE = "notify.email"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""          # shown to the user, in their words, when ok is False
    need: str = ""            # the tier that would allow it: FREE or PRO
    limit: int | None = None  # for READ_IN_FULL: the most this tier may read

    def __bool__(self):
        return self.ok


OK = Verdict(True)


def tier_of(user, balance_usd=None) -> str:
    """-> ANON | FREE | PRO.

    PRO IS A STATE, NOT A FLAG. It is "has a usable card on file", which is what actually decides
    whether the next dollar of model spend can be collected. A user who set one up and then
    removed it is FREE again the moment the card goes, without anything having to remember to
    flip a column.
    """
    if not user or not user.get("is_active", True):
        return ANON
    if user.get("is_admin"):
        return PRO
    if user.get("stripe_payment_method"):
        return PRO
    return FREE


def check(user, action, *, read_top=None, balance_usd=None) -> Verdict:
    """May this user do this? Every gate in the app comes through here."""
    tier = tier_of(user, balance_usd)

    if action == SEARCH_FAST:
        return OK                                   # the whole point of the free tier

    if action in (SEARCH_ATTACK, EMAIL_ON_DONE):
        if tier == ANON:
            return Verdict(False, need=FREE, reason=(
                "A third-party build reads references in full and builds filing papers, so it "
                "runs against an account. Registration is free and the prior-art search does not "
                "need one." if action == SEARCH_ATTACK else
                "There has to be an address to send it to. Registration is free."))
        return OK

    if action == THIRD_PARTY:
        if tier == ANON:
            return Verdict(False, need=FREE, reason=(
                "Searching the outside patent APIs is metered per search, so it runs against an "
                "account. Registration is free."))
        if tier == FREE:
            return Verdict(False, need=PRO, reason=(
                "The outside patent APIs (PQAI, Google Patents through BigQuery, SerpApi, USPTO "
                "and the rest) are billed per call. They are on for Pro accounts, which pay for "
                "what they use."))
        return OK

    if action == READ_IN_FULL:
        if tier == ANON:
            return Verdict(False, need=FREE, limit=0, reason=(
                "Reading references end to end costs model time, so it runs against an account. "
                "Registration is free."))
        if tier == FREE:
            want = int(read_top or 0)
            if want <= FREE_READ_TOP:
                return Verdict(True, limit=FREE_READ_TOP)
            return Verdict(False, need=PRO, limit=FREE_READ_TOP, reason=(
                "A free account reads up to %d references in full in one run. Reading %d is a "
                "Pro run: add a card and you are billed for what it actually spends, at cost."
                % (FREE_READ_TOP, want)))
        return Verdict(True, limit=None)

    if action == DRAFTING:
        if tier != PRO:
            return Verdict(False, need=PRO, reason=(
                "The drafting studio writes and revises a specification with a model, turn by "
                "turn, so it is billed by what it uses. It is a Pro feature."))
        return OK

    return OK                                       # an unknown action is not a gate


def clamp_read_top(user, want) -> int:
    """The read budget this user may actually have. Never raises; always returns a usable number."""
    v = check(user, READ_IN_FULL, read_top=want)
    if v.ok:
        return int(want or 0)
    return int(v.limit or 0)


def describe(user, balance_usd=None) -> dict:
    """What the page shows about the account it is rendering for."""
    tier = tier_of(user, balance_usd)
    return {
        "tier": tier,
        "label": LABEL[tier],
        "can_attack": bool(check(user, SEARCH_ATTACK)),
        "can_third_party": bool(check(user, THIRD_PARTY)),
        "can_draft": bool(check(user, DRAFTING)),
        "read_limit": (None if tier == PRO else (FREE_READ_TOP if tier == FREE else 0)),
        "balance_usd": balance_usd,
        "min_topup_usd": MIN_TOPUP_USD,
    }
