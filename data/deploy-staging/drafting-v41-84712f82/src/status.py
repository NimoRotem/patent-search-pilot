"""Best-effort legal-status classification for display (granted / pending / expired / …).

The corpus has almost no populated `legal_events` (BigQuery didn't ship them), so the primary
signal is the **kind code** (A1 = application/pre-grant, B1/B2/C = granted, U* = utility model,
S* = design) plus **age** (a granted patent whose earliest filing is > ~20 years ago is very
likely expired — useful for prior art: expired = freely practisable). When a reference is enriched
(SerpApi / Lens legal events available) the explicit status overrides the heuristic.

Returns {code, label, tone, note}. `tone` maps to a CSS colour class on the card
(good/accent/muted/bad/secret/warn).
"""
from __future__ import annotations
from datetime import date

TERM_YEARS = 20            # nominal utility-patent term from earliest filing (rough, no PTA/PTE)


def _yr(d):
    try:
        return int(str(d)[:4])
    except (TypeError, ValueError):
        return None


def _s(code, label, tone, note=None):
    return {"code": code, "label": label, "tone": tone, "note": note}


def _age_adjust(st, priority_date, filing_date, today_year=None):
    """Downgrade a granted/utility right to 'likely expired' once it is older than the nominal term."""
    if st["code"] in ("granted", "utility"):
        base = _yr(priority_date) or _yr(filing_date)
        ty = today_year or date.today().year
        if base and (ty - base) > TERM_YEARS:
            return _s("expired", "Likely expired", "muted", f"filed ~{base}, term ~{TERM_YEARS}y")
    return st


def classify_status(kind_code, country, priority_date=None, filing_date=None,
                    publication_date=None, legal_events=None, today_year=None):
    kc = (kind_code or "").upper()
    country = (country or "").upper()
    ev_text = " ".join(((e.get("code") or "") + " " + (e.get("title") or ""))
                       for e in (legal_events or []) if isinstance(e, dict)).lower()

    # 1) explicit legal-event signals (most reliable, only present after enrichment)
    if any(w in ev_text for w in ("expired", "lapsed", "ceased", "not-in-force", "term expired",
                                  "failure to pay")):
        return _s("expired", "Expired / lapsed", "muted")
    if any(w in ev_text for w in ("withdrawn", "refused", "abandoned", "deemed withdrawn",
                                  "revoked", "rejected")):
        return _s("dead", "Withdrawn / refused", "bad")
    granted_ev = any(w in ev_text for w in ("granted", "grant of patent", "patent granted",
                                            "certificate of grant"))

    # 2) kind-code family
    if kc.startswith("S"):
        return _s("design", "Design", "secret")
    if kc.startswith("U"):
        return _age_adjust(_s("utility", "Utility model", "good"), priority_date, filing_date, today_year)
    if kc[:1] in ("B", "C"):
        return _age_adjust(_s("granted", "Granted", "good"), priority_date, filing_date, today_year)
    if kc.startswith("A"):
        # US grants issued before 2001 carry kind 'A'; elsewhere (and modern US) 'A*' = application.
        if country == "US" and kc == "A" and (_yr(publication_date) or 9999) < 2001:
            return _age_adjust(_s("granted", "Granted", "good"), priority_date, filing_date, today_year)
        if granted_ev:
            return _age_adjust(_s("granted", "Granted", "good"), priority_date, filing_date, today_year)
        return _s("application", "Application", "accent")

    # 3) fallbacks
    if granted_ev:
        return _age_adjust(_s("granted", "Granted", "good"), priority_date, filing_date, today_year)
    return _s("unknown", "Status n/a", "muted")
