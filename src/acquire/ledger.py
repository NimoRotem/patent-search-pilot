"""Progress and cost, as rows you can query rather than a number in a log line.

One event per PROVIDER ATTEMPT, not per publication: the point of a cascade is knowing which rung
answered and what the rungs above it cost while missing. `provider + outcome` is indexed, so
"what did SerpApi actually buy us" is one query.

THE BUDGET IS RESERVED, NOT COUNTED AFTERWARDS. SerpApi is 30,000 a month and a single deep search
already spends about 160. A cap held in a process variable is four caps when four workers run, and
a cap checked after the call is a cap that has already been exceeded. `reserve()` is one atomic
UPDATE carrying the cap in its WHERE clause: it either returns the new spend or refuses, and
nothing can cross the line in between. `refund()` puts the reservation back when the call did not
happen, so a timeout does not leak budget.
"""
from __future__ import annotations

import datetime as _dt

import db

EVENTS = "fulltext_fetch_event"
BUDGET = "fulltext_budget"

#  Outcomes. Anything that is not a hit is worth its own name: 'miss' is the provider answering
#  "not mine", 'error' is the provider failing, 'timeout' is it hanging, 'budget' is us refusing
#  to spend. Collapsing them loses exactly the signal an operator needs.
OUTCOMES = ("hit", "miss", "error", "timeout", "budget", "skipped", "breaker",
            "settled")


def month_period(when=None) -> str:
    d = when or _dt.datetime.utcnow()
    return f"{d.year:04d}-{d.month:02d}"


def record(*, worker: str, partition_id, publication_number: str, provider: str, outcome: str,
           claims_chars: int = 0, desc_chars: int = 0, credits: float = 0.0, usd: float = 0.0,
           latency_ms: int = 0, detail: str = "") -> None:
    with db.cursor() as cur:
        cur.execute(
            f"INSERT INTO {EVENTS} (worker, partition_id, publication_number, provider, outcome, "
            f"claims_chars, desc_chars, credits, usd, latency_ms, detail) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (worker, partition_id, publication_number, provider, outcome, int(claims_chars),
             int(desc_chars), float(credits), float(usd), int(latency_ms), str(detail)[:500]))


def record_many(events) -> int:
    rows = [(e.get("worker", ""), e.get("partition_id"), e.get("publication_number", ""),
             e["provider"], e["outcome"], int(e.get("claims_chars") or 0),
             int(e.get("desc_chars") or 0), float(e.get("credits") or 0.0),
             float(e.get("usd") or 0.0), int(e.get("latency_ms") or 0),
             str(e.get("detail") or "")[:500]) for e in events]
    if not rows:
        return 0
    with db.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {EVENTS} (worker, partition_id, publication_number, provider, outcome, "
            f"claims_chars, desc_chars, credits, usd, latency_ms, detail) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    return len(rows)


# ---------------------------------------------------------------------------------------------
# the hard budget
# ---------------------------------------------------------------------------------------------
def reserve(provider: str, amount: float, cap: float, period: str = None) -> dict:
    """Take `amount` credits out of this provider's cap for this period, atomically.

    Returns {"granted": True, "spent": x, "cap": c} or {"granted": False, ...}. A refusal is a
    normal outcome, not an error: the cascade falls through to the next rung, or the publication
    is left pending for a later period.
    """
    period = period or month_period()
    amount, cap = float(amount), float(cap)
    with db.cursor() as cur:
        cur.execute(f"INSERT INTO {BUDGET} (provider, period, cap, spent) VALUES (%s,%s,%s,0) "
                    f"ON CONFLICT (provider, period) DO NOTHING", (provider, period, cap))
        cur.execute(
            f"UPDATE {BUDGET} SET spent = spent + %s, cap = %s, updated_at = now() "
            f"WHERE provider=%s AND period=%s AND spent + %s <= %s "
            f"RETURNING spent, cap",
            (amount, cap, provider, period, amount, cap))
        row = cur.fetchone()
        if row is not None:
            return {"granted": True, "spent": float(row["spent"]), "cap": float(row["cap"])}
        #  Refused. Still write the configured cap through, or the stored one stays at whatever it
        #  was when the last reservation SUCCEEDED and `status` reports a cap that is no longer
        #  the one being enforced. The WHERE clause above always uses the live value, so this is
        #  about the ledger telling the truth rather than about enforcement.
        cur.execute(f"UPDATE {BUDGET} SET cap=%s, updated_at=now() "
                    f"WHERE provider=%s AND period=%s RETURNING spent, cap",
                    (cap, provider, period))
        row = cur.fetchone() or {}
        return {"granted": False, "spent": float(row.get("spent") or 0.0),
                "cap": float(row.get("cap") or cap)}


def refund(provider: str, amount: float, period: str = None) -> None:
    period = period or month_period()
    with db.cursor() as cur:
        cur.execute(f"UPDATE {BUDGET} SET spent = GREATEST(0, spent - %s), updated_at = now() "
                    f"WHERE provider=%s AND period=%s", (float(amount), provider, period))


def budget_state(period: str = None) -> list:
    period = period or month_period()
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute(f"SELECT provider, period, cap, spent FROM {BUDGET} WHERE period=%s "
                    f"ORDER BY provider", (period,))
        return [{"provider": r["provider"], "period": r["period"],
                 "cap": float(r["cap"]), "spent": float(r["spent"]),
                 "left": float(r["cap"]) - float(r["spent"])} for r in cur.fetchall() or []]


# ---------------------------------------------------------------------------------------------
# what has it done, and what has it cost
# ---------------------------------------------------------------------------------------------
def progress(minutes: int = 60) -> dict:
    """The one query an operator runs: pool state, per-provider outcomes, spend, and the rate."""
    from . import tasks
    out = {"pool": tasks.counts(), "period": month_period(), "window_minutes": int(minutes)}
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute(
            f"SELECT provider, outcome, count(*) n, sum(credits) credits, sum(usd) usd, "
            f"sum(claims_chars) claims_chars, sum(desc_chars) desc_chars, "
            f"round(avg(latency_ms)) avg_ms FROM {EVENTS} GROUP BY provider, outcome "
            f"ORDER BY provider, outcome")
        out["providers"] = [{"provider": r["provider"], "outcome": r["outcome"],
                             "n": int(r["n"]), "credits": float(r["credits"] or 0),
                             "usd": float(r["usd"] or 0),
                             "claims_chars": int(r["claims_chars"] or 0),
                             "desc_chars": int(r["desc_chars"] or 0),
                             "avg_ms": int(r["avg_ms"] or 0)} for r in cur.fetchall() or []]
        cur.execute(f"SELECT count(*) n, sum(credits) credits, sum(usd) usd FROM {EVENTS} "
                    f"WHERE outcome='hit'")
        r = cur.fetchone() or {}
        out["hits_total"] = int(r.get("n") or 0)
        cur.execute(f"SELECT sum(credits) credits, sum(usd) usd FROM {EVENTS}")
        r = cur.fetchone() or {}
        out["credits_total"] = float(r.get("credits") or 0)
        out["usd_total"] = float(r.get("usd") or 0)
        cur.execute(f"SELECT count(*) n FROM {EVENTS} WHERE outcome='hit' "
                    f"AND at > now() - make_interval(mins => %s)", (int(minutes),))
        recent = int((cur.fetchone() or {}).get("n") or 0)
        out["hits_in_window"] = recent
        out["hits_per_hour"] = round(recent * 60.0 / max(1, minutes), 1)
    out["budgets"] = budget_state()
    return out


def delete_events(publication_numbers) -> int:
    """Exact publication numbers only. Test fixture cleanup."""
    pubs = list(publication_numbers)
    if not pubs:
        return 0
    with db.cursor() as cur:
        cur.execute(f"DELETE FROM {EVENTS} WHERE publication_number = ANY(%s)", (pubs,))
        return cur.rowcount or 0


def delete_budget(provider: str, period: str) -> int:
    with db.cursor() as cur:
        cur.execute(f"DELETE FROM {BUDGET} WHERE provider=%s AND period=%s", (provider, period))
        return cur.rowcount or 0
