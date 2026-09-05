"""Designs and trademarks on the docket: where each row comes from, and what can be filed against it.

The patent docket answers "which of this company's cases is still open, and through which door".
A company also holds registered designs and trademarks, and those have doors of their own: an
opposition window that runs from publication, an invalidity or cancellation action that runs for
as long as the right is on the register, and a letter of protest that only works before an
examiner has finished. This module is the same idea for those two kinds of right, evaluated per
row, with the sources that actually answer from this box.

SOURCES, AND WHY THESE

  EU marks     The EUIPO trademark-search API, same account as the designs: status, applicants,
               the bulletin publications the opposition period runs from.
  Other marks  TMview (www.tmdn.org), the EUIPO-run aggregator: USPTO, DPMA, WIPO Madrid and sixty
               other offices in one search, with the office's own status and, on the detail
               record, the publication dates and the opposition period. Reached through builder's
               nginx relay at rotem.ai/tmdn/, because tmdn.org times out from this box. tmdn.org
               answers a plain client for a while and then serves a bot-challenge page instead;
               when it does, the sweep reports "TMview search ... Problem detected" and the EU
               rows still come in through the API.
  EU designs   The EUIPO design-search API, the account this app already holds for prior-art
               design searches. Status, dates, product indication, applicants.
  US designs   The USPTO Open Data Portal, which lists design applications by applicant with
               their status, grant date and patent number.

DesignView (DPMA and Hague designs) is on the same aggregator but its search endpoint could not
be identified from its bundle on 2026-09-05, so German and international designs are not on the
docket yet. Everything here says which office it covers rather than pretending to cover all.

THE ROWS SHARE THE PATENT DOCKET'S SHAPE. `kind` says which of the three it is; `publication` is
the docket key (an ST13 number for a mark, an EUIPO design number or a US application number for
a design); `posture` is pending, registered or lapsed; the instrument table is a list of the
same entries `observation_actions` produces, so the page, the filter and the countdown treat all
three kinds alike.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import observation_actions as acts
import observation_refresh as R

KINDS = ("patent", "design", "trademark")
KIND_LABEL = {"patent": "Patents", "design": "Designs", "trademark": "Trademarks"}

#  Builder's relay to www.tmdn.org. The path after it is tmdn.org's own.
TMDN_BASE = os.environ.get("OBS_TMDN_BASE", "https://rotem.ai/tmdn").rstrip("/")
TMDN_TIMEOUT = float(os.environ.get("OBS_TMDN_TIMEOUT", "45"))
EUIPO_TM = "https://api.euipo.europa.eu/trademark-search"
EUIPO_DS = "https://api.euipo.europa.eu/design-search"
#  TMview office codes for the docket's office list. EP has no trademarks; EUIPO is the
#  European register a target's EP offices map to.
TM_OFFICE_FOR = {"EP": "EM", "DE": "DE", "US": "US", "WO": "WO"}
OFFICE_NAME = {"EM": "EUIPO", "US": "USPTO", "DE": "DPMA", "WO": "WIPO"}
MAX_TM_PAGES = 4
MAX_DESIGN_PAGES = 4

#  Where a design's first view is kept once fetched, and served from by the docket. EU views come
#  from DesignView through ScrapingBee (tmdn.org will not talk to a GCP address, see the module
#  docstring); US views are the first sheet of the DRW paper in the file wrapper, rendered.
IMAGE_DIR = Path(os.environ.get("OBS_IMAGE_DIR", str(Path(__file__).resolve().parent.parent / "data" / "observations" / "images")))
SCRAPINGBEE_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "")
#  How many images one sweep may fetch. Each EU view is one ScrapingBee credit.
MAX_IMAGES_PER_SWEEP = int(os.environ.get("OBS_MAX_IMAGES_PER_SWEEP", "80"))

OPPOSITION_MONTHS_EM = 3
OPPOSITION_MONTHS_DE = 3
OPPOSITION_DAYS_US = 30
PROTEST_GRACE_DAYS_US = 30

# ---------------------------------------------------------------------------------------------
# the stages, per kind
# ---------------------------------------------------------------------------------------------

TM_STAGES = (
    ("pre_reg_observation", "Pre-registration observation"),
    ("opposition", "Opposition on publication"),
    ("cancellation", "Cancellation of a registration"),
)
DESIGN_STAGES = (
    ("pre_grant", "Pre-grant submission"),
    ("post_grant_now", "Immediate post-grant challenge"),
    ("invalidity", "Invalidity at any time"),
)
STAGES_FOR = {"trademark": TM_STAGES, "design": DESIGN_STAGES}
STAGE_LABEL = dict(TM_STAGES + DESIGN_STAGES)


def _entry(stage, instrument, statute, fee, **kw):
    e = acts._entry(stage, instrument, statute, fee, **kw)
    e["stage_label"] = STAGE_LABEL.get(stage, e["stage_label"])
    return e


def _d(value):
    return acts._date(str(value or "")[:10])


def _iso(value):
    d = _d(value)
    return d.isoformat() if d else None


# ---------------------------------------------------------------------------------------------
# trademarks: instruments
# ---------------------------------------------------------------------------------------------

def _tm_posture(status):
    s = str(status or "").lower().replace("_", " ")
    #  "accepted" is what the EUIPO calls a Madrid designation that has been granted protection.
    if any(w in s for w in ("registered", "renewed", "accepted")):
        return "registered"
    if any(w in s for w in ("ended", "expired", "cancelled", "canceled", "withdrawn", "refused",
                            "abandoned", "dead", "lapsed", "invalid", "revoked", "surrendered")):
        return "lapsed"
    return "pending"


def _tm_euipo(row, today):
    posture = row.get("posture") or _tm_posture(row.get("status"))
    opp_start, opp_end = _d(row.get("opposition_start")), _d(row.get("opposition_end"))
    if opp_start and not opp_end:
        opp_end = acts.plus_months(opp_start, OPPOSITION_MONTHS_EM)
    out = []
    if posture == "pending":
        out.append(_entry("pre_reg_observation", "Third-party observations", "Art. 45 EUTMR", "€0",
                          status="open",
                          note=("Free, in writing, on absolute grounds only: descriptiveness, lack of "
                                "distinctiveness, bad faith is not one of them. Filed any time before "
                                "registration; the examiner may reopen examination on the strength of it. "
                                "The observer does not become a party and is not told the outcome.")))
    else:
        out.append(_entry("pre_reg_observation", "Third-party observations", "Art. 45 EUTMR", "€0",
                          status="closed" if posture == "registered" else "na",
                          note="Only while the application is pending."))
    if posture == "lapsed":
        out.append(_entry("opposition", "Opposition", "Art. 46 EUTMR", "€320", status="na",
                          note="The mark is no longer live."))
    elif opp_end:
        left = acts.days_until(opp_end, today)
        out.append(_entry("opposition", "Opposition", "Art. 46 EUTMR", "€320",
                          status="open" if left is not None and left >= 0 else "closed",
                          deadline=opp_end, today=today,
                          note=("Three months from publication of the application in the EUTM Bulletin"
                                + (" on %s" % opp_start.isoformat() if opp_start else "")
                                + ". Relative grounds only: an earlier mark or right of the opponent. "
                                  "The fee is paid within the same period or the opposition is deemed "
                                  "not entered; a two-month cooling-off follows admissibility.")))
    elif posture == "pending":
        out.append(_entry("opposition", "Opposition", "Art. 46 EUTMR", "€320", status="not_yet",
                          note="Opens on publication of the application and runs three months."))
    else:
        out.append(_entry("opposition", "Opposition", "Art. 46 EUTMR", "€320", status="closed",
                          note="Registered; the opposition period has run."))
    out.append(_entry("cancellation", "Application for a declaration of invalidity or revocation",
                      "Art. 58, 59, 60 EUTMR", "€630",
                      status="open" if posture == "registered" else ("na" if posture == "lapsed" else "not_yet"),
                      note=("Any time while the mark is registered. Invalidity for absolute grounds or an "
                            "earlier right, revocation for five years' non-use, which is the cheap ground "
                            "against a mark that was never put on the market in the EU.")))
    return out


def _tm_uspto(row, today):
    posture = row.get("posture") or _tm_posture(row.get("status"))
    pub = _d(row.get("publication_date")) or _d(row.get("opposition_start"))
    reg = _d(row.get("registration_date"))
    out = []
    if posture == "pending":
        if pub:
            grace = pub + datetime.timedelta(days=PROTEST_GRACE_DAYS_US)
            left = acts.days_until(grace, today)
            out.append(_entry("pre_reg_observation", "Letter of protest", "37 CFR 2.149", "$150",
                              status="open" if left is not None and left >= 0 else "closed",
                              deadline=grace, today=today,
                              note=("Published for opposition on %s. A letter of protest filed within "
                                    "thirty days of publication is still considered; after that only "
                                    "with a showing that the evidence could not have been filed earlier."
                                    % pub.isoformat())))
        else:
            out.append(_entry("pre_reg_observation", "Letter of protest", "37 CFR 2.149", "$150",
                              status="open",
                              note=("Not yet published. Evidence of a likelihood of confusion with a "
                                    "registered mark, descriptiveness or genericness goes to the "
                                    "examining attorney through the Deputy Commissioner; no argument, "
                                    "no party status, and a reply is not owed. Best filed before "
                                    "publication.")))
    else:
        out.append(_entry("pre_reg_observation", "Letter of protest", "37 CFR 2.149", "$150",
                          status="closed" if posture == "registered" else "na",
                          note="Only while the application is pending."))
    if posture == "lapsed":
        out.append(_entry("opposition", "Opposition before the TTAB", "15 U.S.C. 1063, 37 CFR 2.101",
                          "$600 per class", status="na", note="The mark is no longer live."))
    elif pub:
        end = pub + datetime.timedelta(days=OPPOSITION_DAYS_US)
        left = acts.days_until(end, today)
        out.append(_entry("opposition", "Opposition before the TTAB", "15 U.S.C. 1063, 37 CFR 2.101",
                          "$600 per class",
                          status="open" if (left is not None and left >= 0 and posture != "registered") else "closed",
                          deadline=end, today=today,
                          note=("Thirty days from publication in the Official Gazette on %s. Extensions "
                                "of up to ninety days more on request ($200 to $400), and further only for "
                                "good cause. Any ground of refusal may be pleaded." % pub.isoformat())))
    elif posture == "pending":
        out.append(_entry("opposition", "Opposition before the TTAB", "15 U.S.C. 1063, 37 CFR 2.101",
                          "$600 per class", status="not_yet",
                          note="Opens on publication in the Official Gazette and runs thirty days."))
    else:
        out.append(_entry("opposition", "Opposition before the TTAB", "15 U.S.C. 1063, 37 CFR 2.101",
                          "$600 per class", status="closed", note="Registered; the period has run."))
    if posture == "registered":
        years = ((today - reg).days / 365.25) if reg else None
        note = ("Petition to cancel before the TTAB, $600 per class, any time. Within five years of "
                "registration any ground; after five years only genericness, abandonment, fraud and "
                "a few others.")
        if years is not None and years >= 3:
            note += (" Ex parte expungement ($400 per class) for goods the mark was never used on runs "
                     "from three to ten years after registration; reexamination for non-use at "
                     "filing runs to the fifth year.")
        out.append(_entry("cancellation", "Petition to cancel, expungement or reexamination",
                          "15 U.S.C. 1064, 1066a, 1066b", "$400 to $600 per class", status="open",
                          note=note))
    else:
        out.append(_entry("cancellation", "Petition to cancel, expungement or reexamination",
                          "15 U.S.C. 1064, 1066a, 1066b", "$400 to $600 per class",
                          status="na" if posture == "lapsed" else "not_yet",
                          note="Only against a registration."))
    return out


def _tm_dpma(row, today):
    posture = row.get("posture") or _tm_posture(row.get("status"))
    reg = _d(row.get("registration_date"))
    opp_start = _d(row.get("opposition_start")) or reg
    opp_end = _d(row.get("opposition_end")) or (acts.plus_months(opp_start, OPPOSITION_MONTHS_DE) if opp_start else None)
    out = [_entry("pre_reg_observation", "No pre-registration instrument", "-", "-", status="na",
                  note=("The DPMA registers first and publishes afterwards; the Widerspruch is the "
                        "first door, and it opens on publication of the registration."))]
    if posture == "lapsed":
        out.append(_entry("opposition", "Widerspruch", "§ 42 MarkenG", "€250", status="na",
                          note="The mark is no longer live."))
    elif opp_end:
        left = acts.days_until(opp_end, today)
        out.append(_entry("opposition", "Widerspruch", "§ 42 MarkenG", "€250",
                          status="open" if left is not None and left >= 0 else "closed",
                          deadline=opp_end, today=today,
                          note=("Three months from publication of the registration"
                                + (" on %s" % opp_start.isoformat() if opp_start else "")
                                + ". €250 for one earlier right, €50 for each further one. Earlier marks "
                                  "and earlier rights only; a non-German opponent needs a German "
                                  "representative (§ 96 MarkenG).")))
    else:
        out.append(_entry("opposition", "Widerspruch", "§ 42 MarkenG", "€250", status="not_yet",
                          note="Opens on publication of the registration and runs three months."))
    out.append(_entry("cancellation", "Löschungsantrag (Verfall or Nichtigkeit)", "§§ 49, 50, 53 MarkenG",
                      "€100 Verfall, €400 Nichtigkeit",
                      status="open" if posture == "registered" else ("na" if posture == "lapsed" else "not_yet"),
                      note=("Any time while registered. Revocation for five years' non-use costs €100 at "
                            "the DPMA and shifts the burden to the proprietor unless it objects, in which "
                            "case the action moves to court.")))
    return out


def _tm_wipo(row, today):
    posture = row.get("posture") or _tm_posture(row.get("status"))
    dc = ", ".join(row.get("designated") or []) or "each designated office"
    live = posture != "lapsed"
    return [
        _entry("pre_reg_observation", "Observations at the designated offices", "per office", "varies",
               status="conditional" if live else "na",
               note=("An international registration is examined by each designated office under its "
                     "own law: EUIPO takes Art. 45 observations, the USPTO a letter of protest. "
                     "Designations: %s." % dc)),
        _entry("opposition", "Opposition at the designated offices", "per office", "varies",
               status="conditional" if live else "na",
               note=("Each designated office publishes and opens its own opposition period: three "
                     "months at the EUIPO from republication, thirty days at the USPTO from the "
                     "Official Gazette, three months at the DPMA from publication. The dates are on "
                     "each office's register, not on the international one. Designations: %s." % dc)),
        _entry("cancellation", "Invalidation at the designated offices, or central attack",
               "per office; Art. 6(3) Madrid Protocol", "varies",
               status="open" if posture == "registered" else "na",
               note=("Each designation falls with its office's own cancellation. For five years from "
                     "the international registration date the whole registration also falls if the "
                     "basic mark falls, which is the central attack.")),
    ]


def _tm_other(row, today):
    posture = row.get("posture") or _tm_posture(row.get("status"))
    live = posture != "lapsed"
    return [
        _entry("pre_reg_observation", "Observations, if the office takes them", "national law", "varies",
               status="conditional" if posture == "pending" else "na",
               note="Office %s is on the docket by name only; its procedure is not modelled here." % (row.get("office") or "?")),
        _entry("opposition", "Opposition", "national law", "varies",
               status="conditional" if live else "na", note="Read the period off the office's register."),
        _entry("cancellation", "Cancellation", "national law", "varies",
               status="conditional" if posture == "registered" else "na", note=""),
    ]


_TM_OFFICES = {"EM": _tm_euipo, "US": _tm_uspto, "DE": _tm_dpma, "WO": _tm_wipo}


# ---------------------------------------------------------------------------------------------
# designs: instruments
# ---------------------------------------------------------------------------------------------

def _ds_posture(status, granted=None):
    s = str(status or "").lower()
    if granted:
        return "registered"
    if "registered" in s or "patented" in s or "granted" in s:
        return "registered"
    if any(w in s for w in ("expired", "lapsed", "cancelled", "invalid", "surrender", "abandon", "withdrawn", "refused", "ended")):
        return "lapsed"
    return "pending"


def _ds_euipo(row, today):
    posture = row.get("posture") or _ds_posture(row.get("status"))
    out = [_entry("pre_grant", "No pre-registration instrument", "-", "-", status="na",
                  note=("A Community design is registered on a formalities check within days of filing; "
                        "there is no examination for novelty and nothing to write to. Deferred "
                        "publication can keep it out of sight for up to thirty months.")),
           _entry("post_grant_now", "No opposition", "-", "-", status="na",
                  note="The EUIPO has no opposition against designs. Invalidity is the only door.")]
    out.append(_entry("invalidity", "Application for a declaration of invalidity", "Art. 52 CDR, Art. 25",
                      "€350",
                      status="open" if posture == "registered" else ("na" if posture == "lapsed" else "not_yet"),
                      note=("Any time while the design is registered, before the Invalidity Division. "
                            "Lack of novelty or individual character over anything made available to "
                            "the public before the filing or priority date, including the target's own "
                            "earlier products, is the usual ground. A GRABO product on sale before that "
                            "date is prior art.")))
    return out


def _ds_uspto(row, today):
    posture = row.get("posture") or _ds_posture(row.get("status"), row.get("patent_number"))
    grant = _d(row.get("grant_date"))
    out = []
    if posture == "pending":
        out.append(_entry("pre_grant", "Preissuance submission", "35 U.S.C. 122(e), 37 CFR 1.290",
                          "$0 / $195 / $78", status="open",
                          note=("Design applications are not published, so the six-month date never "
                                "occurs and the window runs until the notice of allowance is mailed "
                                "(MPEP 1134.01). Patents and printed publications only, each with a "
                                "concise description of relevance.")))
    else:
        out.append(_entry("pre_grant", "Preissuance submission", "35 U.S.C. 122(e), 37 CFR 1.290",
                          "$0 / $195 / $78", status="closed" if posture == "registered" else "na",
                          note="Only while the application is pending."))
    if posture == "registered" and grant:
        end = acts.plus_months(grant, 9)
        left = acts.days_until(end, today)
        out.append(_entry("post_grant_now", "Post-grant review", "35 U.S.C. 321, 37 CFR 42.200",
                          "$25,000 + $34,375",
                          status="open" if left is not None and left >= 0 else "closed",
                          deadline=end, today=today,
                          note="Nine months from issue on %s. Any ground, including ornamentality." % grant.isoformat()))
    else:
        out.append(_entry("post_grant_now", "Post-grant review", "35 U.S.C. 321, 37 CFR 42.200",
                          "$25,000 + $34,375", status="na" if posture == "lapsed" else "not_yet",
                          note="Opens on issue and runs nine months."))
    if posture == "registered":
        pgr_over = bool(grant and acts.days_until(acts.plus_months(grant, 9), today) < 0)
        out.append(_entry("invalidity", "Ex parte reexamination, § 301 citation, or inter partes review",
                          "35 U.S.C. 302, 301, 311", "$6,000 reexam ($2,400 small); $0 citation; $23,750 IPR",
                          status="open",
                          note=("Any time the patent is enforceable. A § 301 citation is free and puts the "
                                "art on the file; reexamination needs a substantial new question of "
                                "patentability; inter partes review opens once the post-grant window "
                                "shuts%s." % ("" if pgr_over else " (not yet)"))))
    else:
        out.append(_entry("invalidity", "Ex parte reexamination, § 301 citation, or inter partes review",
                          "35 U.S.C. 302, 301, 311", "$6,000 reexam; $0 citation; $23,750 IPR",
                          status="na" if posture == "lapsed" else "not_yet", note="Only against a patent."))
    return out


def _ds_dpma(row, today):
    posture = row.get("posture") or _ds_posture(row.get("status"))
    return [
        _entry("pre_grant", "No pre-registration instrument", "-", "-", status="na",
               note="Registered on formalities; no examination for novelty."),
        _entry("post_grant_now", "No opposition", "-", "-", status="na", note=""),
        _entry("invalidity", "Nichtigkeitsantrag", "§ 34a DesignG", "€300",
               status="open" if posture == "registered" else ("na" if posture == "lapsed" else "not_yet"),
               note=("Any time while registered, before the DPMA. Lack of novelty or individual "
                     "character, or an earlier right. A non-German applicant needs a German "
                     "representative (§ 58 DesignG).")),
    ]


def _ds_wipo(row, today):
    posture = row.get("posture") or _ds_posture(row.get("status"))
    return [
        _entry("pre_grant", "Per designated office", "Hague Agreement", "varies",
               status="conditional" if posture == "pending" else "na",
               note="Offices that examine (the USPTO among them) can refuse within their period."),
        _entry("post_grant_now", "Per designated office", "Hague Agreement", "varies", status="na", note=""),
        _entry("invalidity", "Invalidation at each designated office", "per office", "varies",
               status="open" if posture == "registered" else "na",
               note="An international design falls office by office: EUIPO invalidity for the EU "
                    "designation, DPMA Nichtigkeit for Germany, reexamination for the United States."),
    ]


_DS_OFFICES = {"EUIPO": _ds_euipo, "USPTO": _ds_uspto, "DPMA": _ds_dpma, "WIPO": _ds_wipo}


def actions_for(row, today=None):
    """Every instrument for one design or trademark row, most actionable first."""
    today = today or datetime.date.today()
    kind = row.get("kind")
    office = str(row.get("office") or "").upper()
    try:
        if kind == "trademark":
            fn = _TM_OFFICES.get(row.get("office_code") or {v: k for k, v in OFFICE_NAME.items()}.get(office, office), _tm_other)
            out = fn(row, today)
        elif kind == "design":
            fn = _DS_OFFICES.get(office)
            out = fn(row, today) if fn else []
        else:
            return []
    except Exception:
        return []
    out.sort(key=lambda a: (acts.STATUS_ORDER.get(a["status"], 9),
                            a["days_left"] if a["days_left"] is not None else 10 ** 6))
    return out


def headline(row, today=None):
    return acts.headline_from(actions_for(row, today))


def reference_matrix(kind, today=None):
    """The instrument table for one kind, by office, with nothing evaluated."""
    today = today or datetime.date.today()
    if kind == "trademark":
        offices = (("EM", "EUIPO"), ("US", "USPTO"), ("DE", "DPMA"), ("WO", "WIPO Madrid"))
        rows = {"EM": {"kind": "trademark", "office": "EUIPO", "office_code": "EM", "posture": "pending"},
                "US": {"kind": "trademark", "office": "USPTO", "office_code": "US", "posture": "pending"},
                "DE": {"kind": "trademark", "office": "DPMA", "office_code": "DE", "posture": "pending"},
                "WO": {"kind": "trademark", "office": "WIPO", "office_code": "WO", "posture": "pending"}}
        windows = {("EM", "pre_reg_observation"): "until registration", ("EM", "opposition"): "3 months from publication",
                   ("EM", "cancellation"): "while registered", ("US", "pre_reg_observation"): "before publication, 30 days after",
                   ("US", "opposition"): "30 days from the Official Gazette", ("US", "cancellation"): "while registered",
                   ("DE", "opposition"): "3 months from publication of the registration", ("DE", "cancellation"): "while registered",
                   ("WO", "opposition"): "per designated office", ("WO", "cancellation"): "per office; central attack 5 years"}
    else:
        offices = (("EUIPO", "EUIPO"), ("USPTO", "USPTO"), ("DPMA", "DPMA"), ("WIPO", "WIPO Hague"))
        rows = {"EUIPO": {"kind": "design", "office": "EUIPO", "posture": "pending"},
                "USPTO": {"kind": "design", "office": "USPTO", "posture": "pending"},
                "DPMA": {"kind": "design", "office": "DPMA", "posture": "pending"},
                "WIPO": {"kind": "design", "office": "WIPO", "posture": "pending"}}
        windows = {("USPTO", "pre_grant"): "until the notice of allowance", ("USPTO", "post_grant_now"): "9 months from issue",
                   ("USPTO", "invalidity"): "while enforceable", ("EUIPO", "invalidity"): "while registered",
                   ("DPMA", "invalidity"): "while registered", ("WIPO", "invalidity"): "per designated office"}
    per = {code: {a["stage"]: a for a in actions_for(dict(rows[code]), today)} for code, _ in offices}
    out = []
    for stage, label in STAGES_FOR.get(kind, ()):
        cells = {}
        for code, _ in offices:
            e = per[code].get(stage) or {}
            available = bool(e) and e.get("status") != "na"
            cells[code] = {"instrument": e.get("instrument", "") if available else "",
                           "statute": e.get("statute", "") if available else "",
                           "fee": e.get("fee", "") if available else "",
                           "window": windows.get((code, stage), "") if available else "",
                           "available": available}
        out.append({"stage": stage, "stage_label": label, "cells": cells})
    return out, offices


# ---------------------------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------------------------

def _http_json(url, body=None, headers=None, timeout=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Accept": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) patent-results",
        **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout or TMDN_TIMEOUT) as fh:
        return json.loads(fh.read().decode("utf-8", "replace") or "{}")


def tmview_search(text, offices, page_size=100, max_pages=MAX_TM_PAGES):
    """Every mark TMview has for a name at these offices. -> [search hit dicts]"""
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        j = _http_json("%s/tmview/api/search/results" % TMDN_BASE, {
            "page": str(page), "pageSize": str(page_size), "criteria": "C",
            "basicSearch": text, "offices": list(offices)})
        hits = j.get("tradeMarks") or []
        for h in hits:
            if h.get("ST13") and h["ST13"] not in seen:
                seen.add(h["ST13"])
                out.append(h)
        if len(hits) < page_size or page >= int(j.get("totalPages") or 1):
            break
    return out


def tmview_detail(st13):
    return _http_json("%s/tmview/api/trademark/detail/%s" % (TMDN_BASE, urllib.parse.quote(str(st13))))


def _euipo_get(url):
    """One EUIPO API call through the adapter this app already has, synchronously."""
    from sources import euipo
    import httpx

    async def go():
        ad = euipo.EUIPO()
        if not ad.enabled():
            raise RuntimeError("EUIPO_KEY / EUIPO_SECRET not set")
        async with httpx.AsyncClient(timeout=40) as c:
            tok = await ad._get_token(c)
            r = await c.get(url, headers=ad._headers(tok))
            if r.status_code != 200:
                raise RuntimeError("EUIPO HTTP %s: %s" % (r.status_code, r.text[:160]))
            return r.json()
    return asyncio.run(go())


def euipo_designs(word, page_size=100, max_pages=MAX_DESIGN_PAGES):
    """Registered Community designs whose applicant name carries a word. -> [design dicts]"""
    out = []
    for page in range(0, max_pages):
        j = _euipo_get("%s/designs?query=%s&size=%d&page=%d" % (
            EUIPO_DS, urllib.parse.quote('applicants.name==*%s*' % word), page_size, page))
        items = j.get("designs") or []
        out.extend(items)
        if len(items) < page_size or page + 1 >= int(j.get("totalPages") or 1):
            break
    return out


def euipo_design_detail(number):
    return _euipo_get("%s/designs/%s" % (EUIPO_DS, urllib.parse.quote(str(number))))


def euipo_trademarks(word, page_size=100, max_pages=MAX_TM_PAGES):
    """EU trademarks (and Madrid designations of the EU) whose applicant name carries a word."""
    out = []
    for page in range(0, max_pages):
        j = _euipo_get("%s/trademarks?query=%s&size=%d&page=%d" % (
            EUIPO_TM, urllib.parse.quote('applicants.name==*%s*' % word), page_size, page))
        items = j.get("trademarks") or []
        out.extend(items)
        if len(items) < page_size or page + 1 >= int(j.get("totalPages") or 1):
            break
    return out


def euipo_trademark_detail(number):
    return _euipo_get("%s/trademarks/%s" % (EUIPO_TM, urllib.parse.quote(str(number))))


def _euipo_tm_fields(t):
    """The fields the docket keeps from an EUIPO trademark record, search hit or detail."""
    pubs = [p for p in (t.get("publications") or []) if isinstance(p, dict)]
    app_pub = None
    for p in pubs:
        if str(p.get("publicationSection") or "").startswith("A.1"):
            app_pub = _iso(p.get("publicationDate")) or app_pub
    apps = [a.get("name") for a in (t.get("applicants") or []) if isinstance(a, dict) and a.get("name")]
    status = str(t.get("status") or "")
    word = ((t.get("wordMarkSpecification") or {}).get("verbalElement") or "").strip()
    out = {
        "status": status, "register_status": status.replace("_", " ").lower(),
        "posture": _tm_posture(status),
        "filing_date": _iso(t.get("applicationDate")), "registration_date": _iso(t.get("registrationDate")),
        "expiry_date": _iso(t.get("expiryDate")), "publication_date": app_pub,
        "opposition_start": app_pub,
        "opposition_end": acts.plus_months(_d(app_pub), OPPOSITION_MONTHS_EM).isoformat() if _d(app_pub) else None,
        "classes": [str(c) for c in (t.get("niceClasses") or [])],
        "mark_type": str(t.get("markFeature") or "").capitalize(),
        "designated": [], "opposition_pending": "OPPOSITION" in status.upper(),
    }
    if apps:
        out["applicants"], out["applicant"] = apps, "; ".join(apps)
    if word:
        out["title"], out["title_full"] = word, word
    return out


def euipo_tm_row(t, target_name=""):
    num = str(t.get("applicationNumber") or "")
    row = {
        "kind": "trademark", "publication": "EM" + num, "office": "EUIPO", "office_code": "EM",
        "application": num, "registration": num, "title": "(figurative)", "title_full": "",
        "applicant": "", "applicants": [], "image": "",
        "register_url": "https://euipo.europa.eu/eSearch/#details/trademarks/%s" % num,
        "google": "", "family": [], "filed": False, "refresh_source": "EUIPO trademark-search API",
        "found_by": "EUIPO trademark search, %s." % datetime.date.today().isoformat(),
        "why_new": "Found by the trademark sweep for %s." % (target_name or "the target"),
        "basis": str(t.get("markBasis") or ""),
    }
    row.update(_euipo_tm_fields(t))
    return row


def euipo_tm_refresh(row):
    num = str(row.get("application") or row.get("publication") or "").replace("EM", "", 1)
    try:
        t = euipo_trademark_detail(num)
    except Exception as exc:
        return {"_error": "EUIPO trademark detail: %s" % str(exc)[:120]}
    out = _euipo_tm_fields(t)
    out["refresh_source"] = "EUIPO trademark-search API"
    out["register_updated"] = datetime.date.today().isoformat()
    out["deadline"], out["deadline_kind"] = None, "none"
    return out


def odp_designs(words, limit=100):
    """US design applications by an applicant, from the Open Data Portal. -> [wrapper dicts]"""
    q = " AND ".join('applicationMetaData.applicantBag.applicantNameText:%s' % w for w in words)
    q += ' AND applicationMetaData.applicationTypeLabelName:Design'
    out, offset = [], 0
    while True:
        p = R._odp("patent/applications/search", {"q": q, "pagination": {"offset": offset, "limit": limit}})
        bag = (p or {}).get("patentFileWrapperDataBag") or []
        out.extend(bag)
        if len(bag) < limit or offset >= 300:
            break
        offset += limit
    return out


# ---------------------------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------------------------

def _english_indication(indications):
    for ind in indications or []:
        if isinstance(ind, dict) and ind.get("language") == "en":
            return "; ".join(ind.get("terms") or [])
    for ind in indications or []:
        if isinstance(ind, dict) and ind.get("terms"):
            return "; ".join(ind.get("terms") or [])
    return ""


def tm_row(hit, target_name=""):
    """A TMview search hit -> a docket row. The detail is read on refresh, not here."""
    st13 = str(hit.get("ST13") or "")
    code = str(hit.get("tmOffice") or "")[:2].upper()
    return {
        "kind": "trademark",
        "publication": st13,
        "office": OFFICE_NAME.get(code, code),
        "office_code": code,
        "title": str(hit.get("tmName") or "").strip() or "(figurative)",
        "title_full": str(hit.get("tmName") or ""),
        "applicant": "; ".join(hit.get("applicantName") or []),
        "applicants": list(hit.get("applicantName") or []),
        "application": str(hit.get("applicationNumber") or ""),
        "registration": str(hit.get("registrationNumber") or ""),
        "classes": [str(c) for c in (hit.get("niceClass") or [])],
        "mark_type": str(hit.get("tradeMarkType") or ""),
        "status": str(hit.get("tradeMarkStatus") or ""),
        "posture": _tm_posture(hit.get("tradeMarkStatus")),
        "filing_date": _iso(hit.get("applicationDate")),
        "registration_date": _iso(hit.get("registrationDate")),
        "expiry_date": _iso(hit.get("expirationDate")),
        "designated": list(hit.get("tProtection") or []),
        "image": str(hit.get("markImageURI") or ""),
        "register_url": str(hit.get("tmOfficeURL") or ""),
        "google": "",
        "family": [],
        "filed": False,
        "found_by": "TMview search, %s." % datetime.date.today().isoformat(),
        "why_new": "Found by the trademark sweep for %s." % (target_name or "the target"),
    }


def tm_refresh(row):
    """Read the mark's detail record: status, publication and the opposition period."""
    try:
        j = tmview_detail(row.get("publication") or "")
    except ValueError:
        return {"_error": "TMview answered with its challenge page instead of JSON"}
    tm = j.get("tradeMark") or {}
    if not tm:
        return {"_error": "TMview has no detail for %s" % row.get("publication")}
    pubs = [p for p in (j.get("publication") or []) if isinstance(p, dict)]
    app_pub = None
    for p in pubs:
        sec = str(p.get("section") or "")
        if sec.startswith("A.1") or sec.lower() == "application":
            app_pub = _iso(p.get("date")) or app_pub
    status = str(tm.get("markCurrentStatusCode") or row.get("status") or "")
    out = {
        "refresh_source": "TMview detail",
        "status": status,
        "register_status": status + ((" since %s" % _iso(tm.get("markCurrentStatusDate"))) if _iso(tm.get("markCurrentStatusDate")) else ""),
        "posture": _tm_posture(status),
        "filing_date": _iso(tm.get("applicationDate")) or row.get("filing_date"),
        "registration_date": _iso(tm.get("codeRegistrationDate")) or row.get("registration_date"),
        "expiry_date": _iso(tm.get("expiryDate")) or row.get("expiry_date"),
        "publication_date": app_pub,
        "opposition_start": _iso(tm.get("oppositionPeriodStartDate")) or _iso(tm.get("oppositionStartDate")) or app_pub,
        "opposition_end": _iso(tm.get("oppositionPeriodEndDate")) or _iso(tm.get("oppositionEndDate")),
        "classes": [c.strip() for c in str(tm.get("niceClass") or "").replace(";", ",").split(",") if c.strip()] or row.get("classes") or [],
        "register_url": str(j.get("officeUrl") or row.get("register_url") or ""),
        "oppositions": [o for o in (j.get("oppositions") or []) if isinstance(o, dict)][:10],
        "cancellations": [o for o in (j.get("cancellations") or []) if isinstance(o, dict)][:10],
        "register_updated": _iso(j.get("officeLastUpdateDate")),
    }
    apps = [a.get("fullName") for a in (j.get("applicants") or []) if isinstance(a, dict) and a.get("fullName")]
    if apps:
        out["applicants"] = apps
        out["applicant"] = "; ".join(apps)
    #  A pending opposition is the fact the instrument table turns on for a mark, as it is for a
    #  patent: it means somebody else is already in, and an intervention or a joinder may exist.
    out["opposition_pending"] = any(str(o.get("status") or "").lower() not in ("closed", "ended", "decided", "")
                                    for o in out["oppositions"])
    #  The docket's own deadline is the nearest open window; recount() takes it from the headline.
    out["deadline"] = None
    out["deadline_kind"] = "none"
    return out


def euipo_design_row(d, target_name=""):
    num = str(d.get("designNumber") or "")
    detail = {}
    try:
        detail = euipo_design_detail(num) if num else {}
    except Exception:
        detail = {}
    apps = [a.get("name") for a in (detail.get("applicants") or d.get("applicants") or []) if isinstance(a, dict) and a.get("name")]
    pubs = [p for p in (detail.get("publications") or []) if isinstance(p, dict)]
    status = str(detail.get("status") or d.get("status") or "")
    return {
        "kind": "design",
        "publication": "RCD" + num,
        "office": "EUIPO",
        "title": _english_indication(detail.get("productIndications")) or ("Locarno " + ", ".join(d.get("locarnoClasses") or [])),
        "title_full": _english_indication(detail.get("productIndications")),
        "applicant": "; ".join(apps),
        "applicants": apps,
        "inventors": [a.get("name") for a in (detail.get("designers") or []) if isinstance(a, dict) and a.get("name")],
        "application": str(d.get("applicationNumber") or ""),
        "registration": num,
        "classes": list(detail.get("locarnoClasses") or d.get("locarnoClasses") or []),
        "status": status,
        "register_status": status.replace("_", " ").lower(),
        "posture": _ds_posture(status),
        "filing_date": _iso(d.get("applicationDate")),
        "registration_date": _iso(d.get("registrationDate")),
        "expiry_date": _iso(d.get("expiryDate")),
        "publication_date": min((x for x in (_iso(p.get("publicationDate")) for p in pubs) if x), default=None),
        "deferred": bool(detail.get("publicationDefermentIndicator") or d.get("publicationDefermentIndicator")),
        "register_url": "https://euipo.europa.eu/eSearch/#details/designs/%s" % num,
        "google": "",
        "family": [],
        "filed": False,
        "refresh_source": "EUIPO design-search API",
        "found_by": "EUIPO design search, %s." % datetime.date.today().isoformat(),
        "why_new": "Found by the design sweep for %s." % (target_name or "the target"),
    }


def euipo_design_refresh(row):
    num = str(row.get("registration") or row.get("publication") or "").replace("RCD", "", 1)
    try:
        detail = euipo_design_detail(num)
    except Exception as exc:
        return {"_error": "EUIPO design detail: %s" % str(exc)[:120]}
    status = str(detail.get("status") or "")
    apps = [a.get("name") for a in (detail.get("applicants") or []) if isinstance(a, dict) and a.get("name")]
    out = {"refresh_source": "EUIPO design-search API", "status": status,
           "register_status": status.replace("_", " ").lower(), "posture": _ds_posture(status),
           "expiry_date": _iso(detail.get("expiryDate")) or row.get("expiry_date"),
           "registration_date": _iso(detail.get("registrationDate")) or row.get("registration_date"),
           "deadline": None, "deadline_kind": "none"}
    if apps:
        out["applicants"], out["applicant"] = apps, "; ".join(apps)
    title = _english_indication(detail.get("productIndications"))
    if title:
        out["title"], out["title_full"] = title, title
    return out


def odp_design_row(w, target_name=""):
    md = w.get("applicationMetaData") or {}
    app = re.sub(r"\D", "", str(w.get("applicationNumberText") or ""))
    pn = str(md.get("patentNumber") or "")
    return {
        "kind": "design",
        "publication": "US" + app,
        "granted_as": ("USD" + pn) if pn else "",
        "patent_number": pn,
        "office": "USPTO",
        "title": str(md.get("inventionTitle") or "").strip().title() or "Design application",
        "title_full": str(md.get("inventionTitle") or ""),
        "applicant": str(md.get("firstApplicantName") or ""),
        "applicants": [n for n in ([md.get("firstApplicantName") or ""] + [a.get("applicantNameText") or "" for a in (md.get("applicantBag") or [])]) if n],
        "inventors": [i.get("inventorNameText") or "" for i in (md.get("inventorBag") or []) if i.get("inventorNameText")],
        "application": app,
        "registration": pn,
        "classes": [c.get("cpcClassificationText") or "" for c in (md.get("cpcClassificationBag") or [])][:3] if isinstance(md.get("cpcClassificationBag"), list) else [],
        "status": str(md.get("applicationStatusDescriptionText") or ""),
        "register_status": str(md.get("applicationStatusDescriptionText") or ""),
        "posture": _ds_posture(md.get("applicationStatusDescriptionText"), pn),
        "filing_date": _iso(md.get("filingDate")),
        "grant_date": _iso(md.get("grantDate")),
        "registration_date": _iso(md.get("grantDate")),
        "register_url": "https://patentcenter.uspto.gov/applications/%s" % app,
        "google": ("https://patents.google.com/patent/USD%s" % pn) if pn else "",
        "family": [],
        "filed": False,
        "refresh_source": "USPTO ODP",
        "found_by": "USPTO ODP design search, %s." % datetime.date.today().isoformat(),
        "why_new": "Found by the design sweep for %s." % (target_name or "the target"),
    }


def odp_design_refresh(row):
    app = re.sub(r"\D", "", str(row.get("application") or ""))
    if not app:
        return {"_error": "no application number"}
    try:
        p = R._odp("patent/applications/%s" % app)
    except R.OdpUnavailable as exc:
        return {"_error": "USPTO ODP unreachable (%s)" % exc}
    bag = (p or {}).get("patentFileWrapperDataBag") or []
    if not bag:
        return {"_error": "ODP has no wrapper for %s" % app}
    md = bag[0].get("applicationMetaData") or {}
    pn = str(md.get("patentNumber") or "")
    status = str(md.get("applicationStatusDescriptionText") or "")
    return {"refresh_source": "USPTO ODP", "status": status, "register_status": status,
            "register_updated": _iso(md.get("applicationStatusDate")),
            "posture": _ds_posture(status, pn), "patent_number": pn, "granted_as": ("USD" + pn) if pn else "",
            "grant_date": _iso(md.get("grantDate")), "registration_date": _iso(md.get("grantDate")),
            "deadline": None, "deadline_kind": "none"}


def refresh_case(row):
    """One design or mark row, re-read from its source. Never raises."""
    try:
        if row.get("kind") == "trademark":
            if (row.get("office_code") or "") == "EM" and not str(row.get("publication") or "").startswith("EM5"):
                return euipo_tm_refresh(row)
            return tm_refresh(row)
        if row.get("office") == "EUIPO":
            return euipo_design_refresh(row)
        if row.get("office") == "USPTO":
            return odp_design_refresh(row)
    except Exception as exc:
        return {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    return {"_skipped": "no live source for %s" % (row.get("office") or "an unknown office")}


#  What a refresh may overwrite on a mark or design row. Everything else is the sweep's own
#  provenance or the person's.
MERGE_FIELDS = (
    "status", "register_status", "register_updated", "posture", "filing_date", "registration_date",
    "expiry_date", "publication_date", "opposition_start", "opposition_end", "classes",
    "register_url", "oppositions", "cancellations", "opposition_pending", "applicants", "applicant",
    "title", "title_full", "patent_number", "granted_as", "grant_date", "refresh_source",
    "deadline", "deadline_kind", "refreshed_at",
)


# ---------------------------------------------------------------------------------------------
# design images
# ---------------------------------------------------------------------------------------------

def design_st13(number):
    """DesignView's key for an EUIPO design: EM7 plus the application and design numbers with
    the dash dropped, zero-filled to fourteen digits. 015132217-0001 -> EM700151322170001."""
    digits = re.sub(r"\D", "", str(number or "").replace("RCD", "", 1))
    return "EM7" + digits.zfill(14) if digits else ""


def image_key(publication):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(publication or ""))[:80]


def image_file(publication):
    """The stored view for a docket row, or None."""
    key = image_key(publication)
    if not key:
        return None
    for ext in ("jpg", "png", "gif"):
        p = IMAGE_DIR / ("%s.%s" % (key, ext))
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def scrapingbee_get(url, headers=None, timeout=90):
    """One page through ScrapingBee, headers forwarded. -> (status, content-type, bytes)"""
    if not SCRAPINGBEE_KEY:
        raise RuntimeError("SCRAPINGBEE_API_KEY not set")
    q = urllib.parse.urlencode({"api_key": SCRAPINGBEE_KEY, "url": url, "render_js": "false",
                                "forward_headers": "true"})
    req = urllib.request.Request("https://app.scrapingbee.com/api/v1/?" + q,
                                 headers={("Spb-" + k): v for k, v in (headers or {}).items()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            return fh.status, fh.headers.get("Content-Type") or "", fh.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type") or "", exc.read()


_IMAGE_MAGIC = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"GIF8": "gif"}


def _kind_of_bytes(data):
    for magic, ext in _IMAGE_MAGIC.items():
        if data[:len(magic)] == magic:
            return ext
    return None


def fetch_euipo_view(row):
    """The first view of a Community design, from DesignView. -> path or None."""
    st13 = design_st13(row.get("registration") or row.get("publication"))
    if not st13:
        return None
    st, ctype, data = scrapingbee_get("https://www.tmdn.org/tmview/api/design/image/%s-1" % st13,
                                      headers={"Accept": "image/*", "Referer": "https://www.tmdn.org/tmdsview-web/"})
    ext = _kind_of_bytes(data) if st == 200 else None
    if not ext:
        raise RuntimeError("DesignView image %s: HTTP %s %s" % (st13, st, ctype[:30]))
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    p = IMAGE_DIR / ("%s.%s" % (image_key(row["publication"]), ext))
    p.write_bytes(data)
    return p


def fetch_uspto_drawing(row):
    """The first sheet of the latest drawings in a US design application's file wrapper,
    rendered to a PNG. -> path or None. Unpublished applications have no public wrapper."""
    app = re.sub(r"\D", "", str(row.get("application") or ""))
    if not app:
        return None
    d = R._odp("patent/applications/%s/documents" % app)
    docs = [x for x in ((d or {}).get("documentBag") or []) if str(x.get("documentCode") or "").startswith("DRW")]
    docs.sort(key=lambda x: str(x.get("officialDate") or ""), reverse=True)
    url = None
    for x in docs:
        for opt in x.get("downloadOptionBag") or []:
            if opt.get("mimeTypeIdentifier") == "PDF" and opt.get("downloadUrl"):
                url = opt["downloadUrl"]
                break
        if url:
            break
    if not url:
        return None
    key = os.environ.get("USPTO_ODP_KEY", "") or os.environ.get("ODP_API_KEY", "")
    #  The portal answers the download with a 302 to a short-lived signed URL, and the signed
    #  URL must be fetched WITHOUT the API key header or the signature does not match.
    req = urllib.request.Request(url, headers={"X-API-KEY": key})

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=60) as fh:
            location = fh.headers.get("Location")
            pdf = fh.read() if not location else b""
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307):
            location = exc.headers.get("Location")
            pdf = b""
        else:
            raise
    if location:
        with urllib.request.urlopen(location, timeout=60) as fh:
            pdf = fh.read()
    if not pdf.startswith(b"%PDF"):
        raise RuntimeError("drawing download for %s was not a PDF" % app)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    out = IMAGE_DIR / ("%s.png" % image_key(row["publication"]))
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "drw.pdf"
        src.write_bytes(pdf)
        subprocess.run(["pdftoppm", "-png", "-f", "1", "-l", "1", "-scale-to", "480", "-singlefile",
                        str(src), str(Path(tmp) / "view")], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        data = (Path(tmp) / "view.png").read_bytes()
    out.write_bytes(data)
    return out


def fetch_design_image(row):
    """Fetch and store the first view for one design row if it has none. -> path or None."""
    if row.get("kind") != "design":
        return None
    have = image_file(row.get("publication"))
    if have:
        return have
    if row.get("office") == "EUIPO":
        return fetch_euipo_view(row)
    if row.get("office") == "USPTO":
        return fetch_uspto_drawing(row)
    return None


def fetch_images(rows, limit=None):
    """Images for the rows that lack one, up to the sweep's budget. -> (fetched, errors)"""
    limit = MAX_IMAGES_PER_SWEEP if limit is None else limit
    fetched, errors = 0, []
    for row in rows:
        if fetched >= limit:
            break
        if row.get("kind") != "design" or image_file(row.get("publication")):
            continue
        try:
            if fetch_design_image(row):
                fetched += 1
        except Exception as exc:
            errors.append("%s image: %s" % (row.get("publication"), str(exc)[:100]))
    return fetched, errors


# ---------------------------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------------------------

def _tm_offices(target):
    codes = [TM_OFFICE_FOR.get(o) for o in (target.get("offices") or ("EP", "DE", "US", "WO"))]
    return [c for c in codes if c]


def discover(target, kind, known, progress=None):
    """Every design or mark the target's assignee names hold at the offices it tracks.
    -> ([row], [str rejected], [str errors])"""
    words_list = [R.name_words(n) for n in (target.get("assignees") or [])]
    words_list = [w for w in words_list if w]
    new, rejected, errors = [], [], []
    name = target.get("name") or "the target"
    if not words_list:
        return new, rejected, ["no assignee names on the target; designs and marks are searched by owner"]
    if kind == "trademark":
        offices = _tm_offices(target)
        if "EM" in offices:
            for words in words_list:
                try:
                    for t in euipo_trademarks(words[0]):
                        row = euipo_tm_row(t, name)
                        if not R.name_matches([words], row["applicants"]):
                            rejected.append("EM %s (%s)" % (row["title"], row["applicant"] or "no owner"))
                            continue
                        if row["publication"].upper() not in known:
                            known.add(row["publication"].upper())
                            new.append(row)
                except Exception as exc:
                    errors.append("EUIPO trademarks for %s: %s" % (" ".join(words), str(exc)[:140]))
            if progress:
                progress("EU trademarks by %s" % name)
        others = [o for o in offices if o != "EM"]
        for words in (words_list if others else []):
            try:
                hits = tmview_search(" ".join(words), others)
            except ValueError:
                errors.append("TMview search for %s at %s: tmdn.org answered with its challenge page "
                              "instead of results, so %s marks were not read this time."
                              % (" ".join(words), ", ".join(others), ", ".join(OFFICE_NAME.get(o, o) for o in others)))
                continue
            except Exception as exc:
                errors.append("TMview search for %s: %s" % (" ".join(words), str(exc)[:120]))
                continue
            for h in hits:
                if not R.name_matches([words], h.get("applicantName") or []):
                    rejected.append("%s %s (%s)" % (h.get("tmOffice"), h.get("tmName"), "; ".join(h.get("applicantName") or []) or "no owner"))
                    continue
                row = tm_row(h, name)
                if row["publication"] and row["publication"].upper() not in known:
                    known.add(row["publication"].upper())
                    new.append(row)
        if progress:
            progress("trademarks by %s at %s" % (name, ", ".join(others) or "no other office"))
    else:
        offices = target.get("offices") or ["EP", "DE", "US", "WO"]
        if "EP" in offices:
            for words in words_list:
                try:
                    for d in euipo_designs(words[0]):
                        row = euipo_design_row(d, name)
                        if not R.name_matches([words], row["applicants"]):
                            rejected.append("%s (%s)" % (row["publication"], row["applicant"] or "no owner"))
                            continue
                        if row["publication"].upper() not in known:
                            known.add(row["publication"].upper())
                            new.append(row)
                except Exception as exc:
                    errors.append("EUIPO designs for %s: %s" % (" ".join(words), str(exc)[:140]))
        if progress:
            progress("EU designs by %s" % name)
        if "US" in offices:
            for words in words_list:
                try:
                    for w in odp_designs(words):
                        row = odp_design_row(w, name)
                        if not R.name_matches([words], row["applicants"]):
                            rejected.append("%s (%s)" % (row["publication"], row["applicant"] or "no owner"))
                            continue
                        if re.search(r"abandon|expired", row["status"], re.I):
                            continue
                        if row["publication"].upper() not in known:
                            known.add(row["publication"].upper())
                            new.append(row)
                except R.OdpUnavailable as exc:
                    errors.append("USPTO design discovery could not run (%s)." % exc)
                except Exception as exc:
                    errors.append("USPTO designs for %s: %s" % (" ".join(words), str(exc)[:140]))
        if progress:
            progress("US designs by %s" % name)
    return new, rejected, errors


def sweep(rows, target, kind, progress=None, workers=4, discover_new=True):
    """Re-read every design or mark row and find the ones the docket has never seen.
    Same return shape as observation_refresh.sweep."""
    from concurrent.futures import ThreadPoolExecutor
    today = datetime.date.today()
    patches, errors, changes = {}, [], []
    done = [0]
    total = [len(rows) + (2 if discover_new else 0)]

    def tick(label):
        done[0] += 1
        if progress:
            progress(done[0], total[0], label)

    def one(row):
        patch = refresh_case(row)
        tick(row.get("title") or row.get("publication") or "")
        return row, patch

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, patch in pool.map(one, rows):
            pub = row.get("publication")
            if not pub:
                continue
            if patch.get("_error"):
                errors.append("%s: %s" % (pub, patch["_error"]))
                continue
            if patch.get("_skipped"):
                continue
            patch = {k: v for k, v in patch.items() if k in MERGE_FIELDS}
            patch["refreshed_at"] = today.isoformat()
            for key, label in (("posture", "posture"), ("status", "status"), ("opposition_end", "opposition ends")):
                before, after = row.get(key), patch.get(key)
                if after not in (None, "") and before != after:
                    changes.append("%s %s: %s is now %s." % (row.get("title") or "", pub, label, after)
                                   if before in (None, "") else
                                   "%s %s: %s moved from %s to %s." % (row.get("title") or "", pub, label, before, after))
            patches[pub] = patch

    new = []
    if discover_new:
        known = {str(r.get("publication") or "").upper() for r in rows}
        known.discard("")
        found, rejected, errs = discover(target, kind, known, progress=lambda label: tick(label))
        new.extend(found)
        errors.extend(errs)
        if rejected:
            changes.append("Found and set aside, the owner on the record does not match this target: %s."
                           % ", ".join(rejected[:8]) + (" And %d more." % (len(rejected) - 8) if len(rejected) > 8 else ""))
        #  Two ticks are budgeted for discovery whatever it reports; settle the count.
        while done[0] < len(rows) + 2:
            tick("discovery")
    total[0] += len(new)

    def settle(row):
        if row.get("kind") == "trademark" and row.get("office_code") != "EM":
            patch = refresh_case(row)
            if not patch.get("_error") and not patch.get("_skipped"):
                row.update({k: v for k, v in patch.items() if k in MERGE_FIELDS})
            elif patch.get("_error"):
                row["refresh_error"] = patch["_error"]
        row["refreshed_at"] = today.isoformat()
        tick(row.get("title") or row.get("publication") or "")
        return row

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row in pool.map(settle, new):
            err = row.pop("refresh_error", None)
            if err:
                errors.append("%s: %s" % (row["publication"], err))
            changes.append("New on the docket: %s %s (%s), %s." % (
                row.get("title") or "", row["publication"], row["office"], row.get("status") or "status unread"))

    if kind == "design":
        #  Views for every design that has none yet, the new rows first. Bounded per sweep, since
        #  each EU view is one ScrapingBee credit.
        got, img_errors = fetch_images(list(new) + list(rows))
        errors.extend(img_errors[:10])
        if got:
            changes.append("Fetched the first view of %d design%s." % (got, "" if got == 1 else "s"))

    return {"patches": patches, "new": new, "errors": errors, "changes": changes,
            "as_of": today.isoformat(), "applicants": list(target.get("assignees") or []),
            "sources": ({"EUIPO": "trademark-search API", "TMview": "www.tmdn.org via rotem.ai/tmdn"}
                        if kind == "trademark"
                        else {"EUIPO": "design-search API", "USPTO": "Open Data Portal"})}
