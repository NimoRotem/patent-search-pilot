"""Oracle injection: put known-relevant references INTO a stage to measure the stages below it.

WHY
---
Funnel attribution says where a reference died. It cannot say what would happen if it had not.
"39 of 82 gold families are never retrieved" is a fact; "fixing retrieval would put them on the
page" is an assumption, and it is the assumption every retrieval project makes and few check. If
the screen would have rejected them anyway, or the portfolio would not have selected them, then
the retrieval work buys nothing and the effort belongs downstream.

Injection answers that directly: hand a stage the gold it never received and measure how much
survives to the page. The result is an UPPER BOUND on what fixing everything above that stage
could be worth.

    inject_before_screen        retrieval is perfect. What do screening onward do?
    inject_before_read          retrieval and screening are perfect.
    inject_before_charting      only reading and below are on trial.
    inject_before_portfolio     everything but selection is perfect.

DIAGNOSTIC ONLY, AND IT MUST BE IMPOSSIBLE TO ENABLE BY ACCIDENT. An injected run has seen the
answer key; any number from it is meaningless as a measure of the product and actively misleading
if it escapes into a comparison. So this refuses to arm unless BOTH an explicit flag and an
explicit gold list are supplied, it stamps every report it touches, and the report stamp is what
the metric code checks before it will score a run.
"""
from __future__ import annotations

import os

FLAG = "ORACLE_INJECTION_ENABLED"
STAGES = ("before_screen", "before_read", "before_charting", "before_portfolio")
#  Stamped onto any report an oracle touched. eval code refuses to score a stamped report as if
#  it were a real run.
REPORT_KEY = "oracle_injection"


class Oracle:
    """Injection plan for one run. Disarmed unless explicitly armed with a gold list."""

    def __init__(self, stage: str = "", gold_families=None, enabled: bool = None):
        env_on = os.environ.get(FLAG, "") not in ("", "0", "false", "False")
        self.stage = stage if stage in STAGES else ""
        self.gold = [f for f in (gold_families or []) if f]
        #  Three independent conditions, because a diagnostic that leaks into a headline number is
        #  worse than no diagnostic: the flag, a valid stage, and a non-empty gold list.
        self.enabled = bool((env_on if enabled is None else enabled)
                            and self.stage and self.gold)
        self.injected = []

    def __bool__(self):
        return self.enabled

    def at(self, stage: str) -> bool:
        return self.enabled and self.stage == stage

    def inject(self, families, stage: str):
        """Splice the gold families into `families` at `stage`. -> the new list.

        Injected families go at the FRONT, because the point is to remove every upstream effect,
        including rank. Anything already present is left where it is and recorded as not needing
        injection, which is itself the measurement of what retrieval already did.
        """
        if not self.at(stage):
            return list(families or [])
        have = set(families or [])
        missing = [f for f in self.gold if f not in have]
        self.injected = missing
        return missing + list(families or [])

    def stamp(self) -> dict:
        return {"stage": self.stage, "n_gold": len(self.gold),
                "n_injected": len(self.injected),
                "already_present": len(self.gold) - len(self.injected),
                "WARNING": "this run saw the answer key; its numbers are an upper bound, "
                           "not a measurement of the product"}


def guard_report(report) -> None:
    """Raise if a report that an oracle touched is about to be scored as a real run."""
    if (report or {}).get(REPORT_KEY):
        raise RuntimeError(
            f"report was produced with oracle injection at "
            f"{report[REPORT_KEY].get('stage')!r} and cannot be scored as a real run")


def is_injected(report) -> bool:
    return bool((report or {}).get(REPORT_KEY))
