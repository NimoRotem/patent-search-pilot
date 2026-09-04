"""What can still be filed against ONE case, given its office and where it stands today.

The docket already answers "when does the door shut". It never answered the question that follows,
which is "which door". Those are not the same question and the second one is office-specific: the
same technical objection is an Art. 115 observation at the EPO, an Einwendung under § 43(3) at
the DPMA and a preissuance submission under 37 CFR 1.290 at the USPTO, with three different fees,
three different windows and, after grant, three instruments that have nothing in common but a
motive. Counsel knows the table by heart. Nobody else does, and the cost of not knowing it is a
window spent on the wrong remedy.

So this module is that table, evaluated. Feed it a docket row and it returns one entry per stage
for that office, each carrying the statute, the fee as the office charges it, the date the window
closes and whether it is open TODAY. Six stages, four offices:

                            Europe / EPO           Germany national       United States
  Pre-grant passive         Art. 115, EUR 0        Para. 43(3), EUR 0     122(e)/1.290, $0/195/78
  Pre-grant protest         Art. 115, EUR 0        Para. 43(3), EUR 0     1.291 protest, $0 first
  Force examination         none                   Para. 44(2), 150/350   none
  Post-grant passive art    Art. 115, only with    none                   35 USC 301, $0
                            a proceeding pending
  Immediate post-grant      Opposition, EUR 880    Einspruch, EUR 200     PGR, $25k + $34,375
                            9 months from grant    9 months from grant    9 months from issue
  Later administrative      Art. 105 intervention  Para. 59(2) Beitritt   IPR, $23,750 + $28,125
                            EUR 880                EUR 200                no end date

WHAT IT WILL NOT DO. It never says "file this". Availability here is the arithmetic of dates and
posture, and three of the cells turn on a fact no register publishes on the case's own page:
whether an opposition is already pending (Art. 105, § 59(2)), whether a request for examination
is already on file (§ 44(2)), and whether the patent is AIA-eligible (PGR). Those come back as
`conditional` with the precondition named, which is an instruction to go and look, not a verdict.
"""
from __future__ import annotations

import datetime
import re

#  The six rows of the table, in the order a case moves through them.
STAGES = (
    ("pre_grant_passive", "Pre-grant passive submission"),
    ("pre_grant_protest", "Pre-grant broader protest"),
    ("force_exam", "Force examination"),
    ("post_grant_passive", "Passive post-grant prior-art filing"),
    ("post_grant_now", "Immediate post-grant challenge"),
    ("post_grant_later", "Later administrative challenge"),
)
STAGE_LABEL = dict(STAGES)

#  How an entry reads on the page. `open` and `closing` are the only two you can act on today.
#  `conditional` means the instrument exists but turns on a fact this data cannot see.
STATUS_ORDER = {"closing": 0, "open": 1, "conditional": 2, "not_yet": 3, "closed": 4, "na": 5}

#  § 44(2) PatG: a third party may demand examination, but only within seven years of the
#  filing date of the application.
DE_EXAM_REQUEST_YEARS = 7
#  The AIA cut-off. A patent whose earliest effective filing date is before this is not eligible
#  for post-grant review at all, only for inter partes review.
AIA_FIRST_TO_FILE = datetime.date(2013, 3, 16)

OPPOSITION_MONTHS = 9

#  Repeated in four cells, because Art. 115 IS the pre-grant and the post-grant instrument at
#  the EPO; only whether anyone reads it changes.
EPC_115 = "Art. 115 EPC, Rule 114 EPC"


def _date(value):
    """Anything the docket stores as a date -> date, or None. Accepts YYYY-MM-DD and YYYYMMDD."""
    if isinstance(value, datetime.date):
        return value
    s = str(value or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def plus_months(d, n):
    """The office's own arithmetic: the same day-of-month n months on, clamped to month end."""
    if not d:
        return None
    y, m = d.year, d.month + n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    last = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return datetime.date(y, m, min(d.day, last))


def days_until(when, today):
    if not when:
        return None
    return (when - today).days


def _entry(stage, instrument, statute, fee, *, status, deadline=None, today=None,
           opens=None, note="", url="", weak=False):
    """One cell of the table, ready to render.

    `weak` marks an instrument that formally exists and that nobody would actually use here: a
    1.291 protest on an application that has already published needs the applicant's written
    consent, and post-grant Art. 115 observations with no proceeding pending are filed and never
    read. They belong in the table, because the table is the answer to "is there anything at
    all". They must never be the one line the docket row carries, which was reporting "Protest"
    beside four dead US applications as though something could be done about them.
    """
    left = days_until(deadline, today) if (deadline and today) else None
    if status == "open" and left is not None and 0 <= left <= 30:
        status = "closing"
    return {
        "stage": stage,
        "stage_label": STAGE_LABEL.get(stage, stage),
        "instrument": instrument,
        "statute": statute,
        "fee": fee,
        "status": status,
        "deadline": deadline.isoformat() if deadline else None,
        "opens": opens.isoformat() if opens else None,
        "days_left": left,
        "note": note,
        "url": url,
        "weak": bool(weak),
    }


# ---------------------------------------------------------------------------------------------
# the three offices
# ---------------------------------------------------------------------------------------------

def _epo(row, today):
    posture = (row.get("posture") or "").lower()
    granted = posture == "granted"
    dead = posture == "lapsed"
    grant_pub = _date(row.get("grant_published"))
    #  The shipped baseline carries the opposition date in the row's own `deadline` and nothing
    #  else, because it predates this module. Falling back to it means the table is right on a
    #  docket that has never been refreshed, which is the state every reader sees first.
    opp_deadline = (_date(row.get("opposition_deadline"))
                    or (plus_months(grant_pub, OPPOSITION_MONTHS) if grant_pub else None)
                    or (_date(row.get("deadline")) if granted
                        and row.get("deadline_kind") == "hard" else None))
    #  A register status of "Grant of patent is intended" is the Rule 71(3) communication: the
    #  examining division has stopped examining, so an observation filed now arrives after the
    #  person it was addressed to has finished.
    status_text = (row.get("register_status") or "").lower()
    intended = bool(row.get("closing_soon")) or "intended" in status_text
    opp_pending = bool(row.get("opposition_pending"))
    out = []

    if dead:
        gone = ("The application is no longer pending, so there is no examining "
                "division to write to.")
        out.append(_entry("pre_grant_passive", "Third-party observations", EPC_115, "€0",
                          status="closed", note=gone))
        out.append(_entry("pre_grant_protest", "Third-party observations, full submission",
                          EPC_115, "€0", status="closed", note=gone))
    elif granted:
        shut = ("Examination is over. Art. 115 observations after grant are only looked at while "
                "opposition, limitation or revocation proceedings are pending.")
        out.append(_entry("pre_grant_passive", "Third-party observations", EPC_115, "€0",
                          status="closed", note=shut))
        out.append(_entry("pre_grant_protest", "Third-party observations, full submission",
                          EPC_115, "€0", status="closed", note=shut))
    else:
        note = ("No deadline. The window runs for as long as examination is pending, and the EPO "
                "undertakes to act within three months only on SUBSTANTIATED observations. "
                "Anonymous observations have been held inadmissible, so sign them.")
        if intended:
            note = ("The Rule 71(3) intention to grant has already issued, so the examining "
                    "division has finished with the file and an observation is unlikely to reach "
                    "it. The remedy worth planning is the Art. 99 opposition. " + note)
        out.append(_entry("pre_grant_passive", "Third-party observations", EPC_115, "€0",
                          status="conditional" if intended else "open", note=note))
        out.append(_entry("pre_grant_protest", "Third-party observations, full submission",
                          EPC_115, "€0", status="conditional" if intended else "open",
                          note=("Same instrument and the same nil fee as the passive route. Art. 115 "
                                "sets no limit on scope, so novelty, inventive step, sufficiency and "
                                "added matter can all go in one submission; only the length of the "
                                "argument changes, not the filing.")))

    out.append(_entry("force_exam", "No third-party instrument", "-", "-", status="na",
                      note=("Examination at the EPO is requested by the applicant. A third party "
                            "cannot force it, and Art. 115 carries no right to be heard.")))

    out.append(_entry(
        "post_grant_passive", "Third-party observations", EPC_115, "€0",
        status=("open" if (granted and opp_pending) else "conditional" if granted else "not_yet"),
        weak=bool(granted and not opp_pending),
        note=("Worth filing only while an opposition, a limitation or a revocation request is "
              "already pending, because that is the only proceeding left for the observations to "
              "enter. On a granted patent with nothing pending they are filed and not read.")))

    if dead:
        out.append(_entry("post_grant_now", "Opposition", "Art. 99, 100 EPC", "€880",
                          status="na", note="The application never granted."))
    elif granted and opp_deadline:
        left = days_until(opp_deadline, today)
        out.append(_entry(
            "post_grant_now", "Opposition", "Art. 99, 100 EPC", "€880",
            status="open" if left is not None and left >= 0 else "closed",
            deadline=opp_deadline, today=today,
            note=("Nine months from the mention of grant in the European Patent Bulletin"
                  + (" on %s" % grant_pub.isoformat() if grant_pub else "")
                  + ". The fee must be paid within the same period or the opposition is deemed "
                    "not filed. After this date the only route left is national revocation, "
                    "country by country.")))
    elif granted:
        out.append(_entry("post_grant_now", "Opposition", "Art. 99, 100 EPC", "€880",
                          status="conditional",
                          note=("The patent is granted but the mention-of-grant date is not on "
                                "the record here, so the nine months cannot be counted. Read it "
                                "off the European Patent Register before relying on any date.")))
    else:
        out.append(_entry(
            "post_grant_now", "Opposition", "Art. 99, 100 EPC", "€880", status="not_yet",
            opens=_date(row.get("opposition_opens_est")),
            note=("Opens on the mention of grant and runs nine months from it."
                  + (" On the current file that is expected around %s."
                     % (row.get("opposition_opens_est") or "") if row.get("opposition_opens_est")
                     else ""))))

    out.append(_entry(
        "post_grant_later", "Intervention of the assumed infringer", "Art. 105 EPC, Rule 89 EPC", "€880",
        status="open" if (granted and opp_pending) else "conditional" if granted else "not_yet",
        weak=bool(granted and not opp_pending),
        note=("Only available while somebody else's opposition is still pending, and only to a "
              "party against whom the proprietor has started infringement proceedings, or who has "
              "started a declaration of non-infringement action. Three months from the date those "
              "proceedings were instituted. It is the way back in after the nine months, and it "
              "depends on a fact the register does not put on this page.")))
    return out


def _dpma(row, today):
    posture = (row.get("posture") or "").lower()
    granted = posture == "granted"
    dead = posture == "lapsed"
    grant_pub = _date(row.get("grant_published"))
    #  The shipped baseline carries the opposition date in the row's own `deadline` and nothing
    #  else, because it predates this module. Falling back to it means the table is right on a
    #  docket that has never been refreshed, which is the state every reader sees first.
    opp_deadline = (_date(row.get("opposition_deadline"))
                    or (plus_months(grant_pub, OPPOSITION_MONTHS) if grant_pub else None)
                    or (_date(row.get("deadline")) if granted
                        and row.get("deadline_kind") == "hard" else None))
    scheduled = _date(row.get("scheduled_grant"))
    decision = _date(row.get("decision_on"))
    refused = _date(row.get("refused_on"))
    filing = _date(row.get("filing_date")) or _date(row.get("filing_date_office"))
    exam_requested = row.get("exam_requested")
    out = []

    if granted or dead:
        shut = ("The examination section is done with the file, so § 43(3) has nothing left to "
                "reach.") if granted else "The application is no longer pending."
        out.append(_entry("pre_grant_passive", "Einwendungen Dritter", "§ 43(3) PatG", "€0",
                          status="closed", note=shut))
        out.append(_entry("pre_grant_protest", "Einwendungen Dritter, full submission",
                          "§ 43(3) PatG", "€0", status="closed", note=shut))
    else:
        soon = scheduled or decision
        note = ("No deadline and no fee. The objections go on the file and the examining section "
                "must take them into account, but a third party acquires no party status and is "
                "not told what happened.")
        if soon:
            note = ("Grant is already %s for %s, so the examining section has finished. An "
                    "Einwendung filed now would very likely never be read. %s"
                    % ("scheduled" if scheduled else "decided", soon.isoformat(), note))
        elif refused:
            #  Refused but not yet final: DPMAregister still says pending, and so does every
            #  database that copies it. There is only an examining section to write to if an
            #  appeal is actually running.
            note = ("The application was refused on %s and the refusal is not final on the "
                    "register, so it still reads as pending everywhere. Worth filing only if a "
                    "Beschwerde turns out to be on foot. %s" % (refused.isoformat(), note))
        blocked = bool(soon or refused)
        out.append(_entry("pre_grant_passive", "Einwendungen Dritter", "§ 43(3) PatG", "€0",
                          status="conditional" if blocked else "open", deadline=soon, today=today,
                          note=note))
        out.append(_entry("pre_grant_protest", "Einwendungen Dritter, full submission",
                          "§ 43(3) PatG", "€0",
                          status="conditional" if blocked else "open", deadline=soon, today=today,
                          note=("The same free filing. § 43(3) does not limit the grounds, so "
                                "novelty, inventive step and sufficiency all go in one paper.")))

    #  § 44(2): anyone may demand examination, within seven years of filing. This is the one
    #  offensive move available BEFORE grant: it starts the clock on a case the applicant was
    #  content to leave asleep, and it is the reason a German application sitting unexamined for
    #  six years is an opportunity rather than a dead row.
    if granted or dead:
        out.append(_entry("force_exam", "Prüfungsantrag by a third party", "§ 44(1), (2) PatG",
                          "€350, or €150 after a § 43 search request", status="na",
                          note="Examination is over."))
    else:
        limit = datetime.date(filing.year + DE_EXAM_REQUEST_YEARS, filing.month,
                              filing.day) if filing else None
        if exam_requested is True:
            #  No deadline on this branch. A seven-year limit printed beside "closed" reads as a
            #  contradiction, and it is: the seven years have nothing to do with why this is spent.
            limit = None
            status, note = "closed", (
                "A request for examination is already on the register, so there is nothing to "
                "force. The case is in examination and § 43(3) is the route.")
        elif limit and days_until(limit, today) is not None and days_until(limit, today) < 0:
            status, note = "closed", (
                "More than seven years from the filing date of %s. § 44(2) is spent and the "
                "application is deemed withdrawn if nobody requested examination."
                % filing.isoformat())
        else:
            status = "open" if exam_requested is False else "conditional"
            note = ("Anybody may demand examination, not only the applicant. €350, or €150 "
                    "where a § 43 search request has already been filed. It forces a case the "
                    "applicant was content to leave unexamined into examination, where § 43(3) "
                    "objections can then be put in front of an examiner. Seven years from the "
                    "filing date"
                    + (", so until %s." % limit.isoformat() if limit else "."))
            if exam_requested is None:
                note = ("Whether examination has already been requested is not on the record "
                        "here: check DPMAregister before paying. " + note)
        out.append(_entry("force_exam", "Prüfungsantrag by a third party", "§ 44(1), (2) PatG",
                          "€350, or €150 after a § 43 search request", status=status,
                          deadline=limit, today=today, note=note))

    out.append(_entry("post_grant_passive", "No passive post-grant instrument", "-", "-",
                      status="na",
                      note=("German practice has no equivalent of 35 U.S.C. 301: there is no way to put "
                            "prior art on a granted patent's file without becoming a party. After "
                            "the nine months the routes are Nichtigkeitsklage at the Bundes"
                            "patentgericht, or an opposition somebody else has already filed.")))

    if dead:
        out.append(_entry("post_grant_now", "Einspruch", "§ 59(1), § 21 PatG", "€200", status="na",
                          note="The application never granted."))
    elif granted and opp_deadline:
        left = days_until(opp_deadline, today)
        out.append(_entry(
            "post_grant_now", "Einspruch", "§ 59(1), § 21 PatG", "€200",
            status="open" if left is not None and left >= 0 else "closed",
            deadline=opp_deadline, today=today,
            note=("Nine months from publication of the grant in the Patentblatt"
                  + (" on %s" % grant_pub.isoformat() if grant_pub else "")
                  + ". €200, and the opponent becomes a party with a right to be heard, which "
                    "is the whole difference from § 43(3). After it closes the only route is a "
                    "nullity action, which costs orders of magnitude more.")))
    elif granted:
        out.append(_entry("post_grant_now", "Einspruch", "§ 59(1), § 21 PatG", "€200",
                          status="conditional",
                          note=("Granted, but the Patentschrift publication date is not on the "
                                "record here, so the nine months cannot be counted. Read it off "
                                "DPMAregister.")))
    else:
        out.append(_entry(
            "post_grant_now", "Einspruch", "§ 59(1), § 21 PatG", "€200", status="not_yet",
            opens=scheduled,
            note=("Opens on publication of the Patentschrift and runs nine months."
                  + (" That publication is already scheduled for %s, which puts the deadline at "
                     "%s." % (scheduled.isoformat(),
                              plus_months(scheduled, OPPOSITION_MONTHS).isoformat())
                     if scheduled else ""))))

    out.append(_entry(
        "post_grant_later", "Beitritt to a pending opposition", "§ 59(2) PatG", "€200",
        status="open" if (granted and row.get("opposition_pending")) else
               "conditional" if granted else "not_yet",
        weak=bool(granted and not row.get("opposition_pending")),
        note=("Only while somebody else's opposition is still pending, and only for a party sued "
              "for infringement or who has been asked to stop. Three months from service. It is "
              "the German mirror of Art. 105 and it depends on a fact the register does not show "
              "on this page.")))
    return out


def _uspto(row, today):
    posture = (row.get("posture") or "").lower()
    granted = posture == "granted" or bool(row.get("patent_number"))
    dead = posture == "lapsed" or "abandon" in (row.get("register_status") or "").lower()
    six = _date(row.get("six_months"))
    #  The publication date decides 1.291, and half the baseline rows do not carry it. It is
    #  recoverable: the six-month date IS publication plus six months. Guessing "not published"
    #  from a missing field made a protest read as open on an application that published a year
    #  ago, which is the one answer that is never right.
    pub = _date(row.get("pubDate")) or (plus_months(six, -6) if six else None)
    six = six or plus_months(pub, 6)
    first_rej = _date(row.get("first_rejection"))
    allowance = _date(row.get("allowance"))
    quayle = _date(row.get("quayle"))
    grant_date = _date(row.get("grant_date"))
    priority = _date(row.get("priority_date")) or _date(row.get("filing_date"))
    out = []

    #  37 CFR 1.290(b): the earlier of (a) the notice of allowance, and (b) the LATER of six
    #  months from publication and the first rejection. The word that costs windows is "later":
    #  a case with no rejection stays open past the six-month date, and a case with an early
    #  rejection closes on the six-month date, not on the rejection.
    if allowance:
        tps_close, kind = allowance, "closed"
    elif first_rej:
        tps_close = max(d for d in (six, first_rej) if d) if six else first_rej
        kind = "hard"
    else:
        tps_close, kind = six, "open_ended"

    if dead:
        out.append(_entry("pre_grant_passive", "Preissuance submission", "35 U.S.C. 122(e), 37 CFR 1.290",
                          "$0 / $195 / $78", status="closed",
                          note="The application is abandoned. There is nothing pending to submit into."))
    elif granted:
        out.append(_entry("pre_grant_passive", "Preissuance submission", "35 U.S.C. 122(e), 37 CFR 1.290",
                          "$0 / $195 / $78", status="closed",
                          note="The patent has issued. § 301 is the post-grant equivalent."))
    else:
        left = days_until(tps_close, today) if tps_close else None
        if kind == "closed":
            status = "closed"
            note = ("A notice of allowance was mailed on %s and that shuts the window on its own, "
                    "whatever the six-month date says." % allowance.isoformat())
        elif kind == "open_ended":
            #  THE ORDER OF THESE TWO BRANCHES IS THE RULE. A six-month date in the past does NOT
            #  close 1.290 while no rejection has issued, because the window runs to the LATER of
            #  the two and the later one has not happened yet. Testing "is the deadline past"
            #  first, which is the obvious way to write this, silently reports a live window as
            #  spent. The date is dropped from the entry once it is behind us so that the page
            #  never shows a countdown that is both negative and open.
            status = "open"
            if left is not None and left < 0:
                tps_close = None
                note = ("The six-month date of %s has passed and no rejection has issued, so the "
                        "window is still open: 1.290(b) closes on the LATER of the six-month date "
                        "and the first rejection. It shuts the day either a rejection or a notice "
                        "of allowance is mailed, with no warning and no grace."
                        % six.isoformat())
            else:
                note = ("No rejection has issued yet. Because the rule closes on the LATER of the "
                        "six-month date and the first rejection, it will stay open past %s until "
                        "a rejection issues. A notice of allowance would shut it the day it is "
                        "mailed, without warning. Treat the date shown as the working deadline, "
                        "not a guarantee."
                        % (six.isoformat() if six else "the six-month date"))
            if quayle:
                note = ("An Ex parte Quayle action issued on %s: prosecution on the merits is "
                        "closed and the claims are already indicated allowable. A Quayle action is "
                        "not a rejection under 1.290(b)(2)(ii), so the window is arguably still "
                        "open, but treat it as expiring any day. " % quayle.isoformat()) + note
        elif left is not None and left < 0:
            status = "closed"
            note = ("The first rejection issued %s and the six-month date was %s, so the window "
                    "closed on the later of the two."
                    % (first_rej.isoformat() if first_rej else "none",
                       six.isoformat() if six else "none"))
        else:
            status = "open"
            note = ("The later of six months from publication (%s) and the first rejection (%s)."
                    % (six.isoformat() if six else "none",
                       first_rej.isoformat() if first_rej else "none yet"))
        out.append(_entry(
            "pre_grant_passive", "Preissuance submission", "35 U.S.C. 122(e), 37 CFR 1.290",
            "$0 / $195 / $78", status=status, deadline=tps_close, today=today,
            note=(note + " Every document needs a concise description of relevance, and that "
                         "description may not argue patentability; without it the whole "
                         "submission is non-compliant and is discarded rather than returned.")))

    #  1.291 is the older, wider instrument and it closes EARLIER than 1.290, which is the part
    #  that surprises people: after publication it needs the applicant's written consent, so in
    #  practice it is only live on an unpublished application.
    protest_close = min([d for d in (pub, allowance) if d], default=None)
    if dead or granted:
        out.append(_entry("pre_grant_protest", "Protest", "37 CFR 1.291", "$0 first protest",
                          status="closed",
                          note="Only available while an application is pending."))
    else:
        published = bool(pub and pub <= today)
        if published:
            note = ("The application published on %s, so a protest is only entered with the "
                    "applicant's written consent, which is not going to be given. In practice "
                    "1.290 is the live route and 1.291 is the one for an application that has "
                    "not published yet." % pub.isoformat())
        elif pub is None:
            note = ("Whether this application has published is not on the record here, and that "
                    "is what decides it: after publication a protest needs the applicant's "
                    "written consent. Check Patent Center before preparing one.")
        else:
            note = ("Before publication or before a notice of allowance, whichever is earlier. "
                    "Wider than 1.290: any ground of unpatentability, not only documents. A "
                    "first protest by a member of the public carries no fee.")
        out.append(_entry(
            "pre_grant_protest", "Protest", "37 CFR 1.291", "$0 first protest",
            status="open" if (pub and not published) else "conditional",
            weak=published, deadline=protest_close, today=today, note=note))

    out.append(_entry("force_exam", "No third-party instrument", "-", "-", status="na",
                      note=("Every US application is examined. There is nothing to force, and no "
                            "US equivalent of § 44(2) PatG.")))

    out.append(_entry(
        "post_grant_passive", "Citation of prior art in a patent file", "35 U.S.C. 301, 37 CFR 1.501",
        "$0", status="open" if granted else "not_yet",
        note=("Free, any time the patent is enforceable, and it can be filed anonymously. The art "
              "goes into the patent's own file and is in front of the examiner in any later "
              "reexamination or reissue. It institutes nothing on its own, which is exactly why "
              "it costs nothing.")))

    if not granted:
        out.append(_entry("post_grant_now", "Post-grant review", "35 U.S.C. 321, 37 CFR 42.200", "$25,000 + $34,375",
                          status="not_yet",
                          note="Opens on issue and runs nine months from it."))
        out.append(_entry("post_grant_later", "Inter partes review", "35 U.S.C. 311, 37 CFR 42.100",
                          "$23,750 + $28,125", status="not_yet",
                          note="Opens nine months after issue, or when a PGR ends."))
    else:
        pgr_close = plus_months(grant_date, OPPOSITION_MONTHS) if grant_date else None
        aia = (priority >= AIA_FIRST_TO_FILE) if priority else None
        left = days_until(pgr_close, today) if pgr_close else None
        if aia is False:
            status, note = "na", (
                "The earliest effective filing date is before 16 March 2013, so this patent is not "
                "eligible for post-grant review at all. Inter partes review is the only route.")
        elif left is None:
            status, note = "conditional", (
                "Granted, but the issue date is not on the record here, so the nine months cannot "
                "be counted.")
        elif left < 0:
            status, note = "closed", (
                "Nine months from issue on %s. Closed; inter partes review is what is left."
                % grant_date.isoformat())
        else:
            status = "open"
            note = ("Nine months from issue on %s. The only US forum that will hear § 101 and "
                    "§ 112 grounds as well as prior art, and the only one where the whole "
                    "specification is in play. The fee shown is the request plus the "
                    "post-institution instalment; counsel is the larger number by far."
                    % grant_date.isoformat())
            if aia is None:
                note = ("Confirm the patent is AIA-eligible, an effective filing date on or after "
                        "16 March 2013, before relying on this. " + note)
        out.append(_entry("post_grant_now", "Post-grant review", "35 U.S.C. 321, 37 CFR 42.200",
                          "$25,000 + $34,375", status=status, deadline=pgr_close, today=today,
                          note=note))
        opens = pgr_close
        out.append(_entry(
            "post_grant_later", "Inter partes review", "35 U.S.C. 311, 37 CFR 42.100", "$23,750 + $28,125",
            status="open" if (opens and today >= opens) or aia is False else "not_yet",
            opens=opens,
            note=("Prior art only, and only patents and printed publications. Opens the day the "
                  "post-grant review window closes"
                  + (" on %s" % opens.isoformat() if opens else "")
                  + ", and there is no end date, but a party served with an infringement "
                    "complaint has one year from service and not a day more.")))
    return out


def _pct(row, today):
    """WIPO. The matrix does not cover the international phase, so it gets the one instrument it
    has plus a pointer at where the real windows open."""
    deadline = _date(row.get("deadline"))
    return [
        _entry("pre_grant_passive", "Third-party observation via ePCT", "PCT Rule 114", "$0",
               status="open" if (deadline and today <= deadline) else "closed",
               deadline=deadline, today=today,
               note=("Free, filed through ePCT, and limited to novelty and inventive step. Until "
                     "28 months from the priority date. It reaches every designated office at "
                     "once, which no national filing does.")),
        _entry("pre_grant_protest", "Third-party observation via ePCT", "PCT Rule 114", "$0",
               status="open" if (deadline and today <= deadline) else "closed",
               deadline=deadline, today=today,
               note="Rule 114 admits novelty and inventive step only. Nothing else."),
        _entry("force_exam", "No third-party instrument", "-", "-", status="na",
               note="Chapter II demand belongs to the applicant."),
        _entry("post_grant_passive", "-", "-", "-", status="na",
               note="There is no international patent to file against."),
        _entry("post_grant_now", "National and regional phase", "-", "-", status="not_yet",
               note=("The instruments that matter open when the application enters the national "
                     "and regional phases, usually at 30 or 31 months. Watch which offices it "
                     "enters: each entry creates its own observation and opposition window.")),
        _entry("post_grant_later", "-", "-", "-", status="na", note=""),
    ]


_OFFICES = {"EPO": _epo, "DPMA": _dpma, "USPTO": _uspto}


def actions_for(row, today=None):
    """Every instrument for one docket row, one per stage, most actionable first."""
    today = today or datetime.date.today()
    office = (row.get("office") or "").upper()
    fn = _OFFICES.get(office)
    if fn is None:
        fn = _pct if office.startswith("WIPO") else None
    if fn is None:
        return []
    try:
        out = fn(row, today)
    except Exception:
        return []
    out.sort(key=lambda a: (STATUS_ORDER.get(a["status"], 9),
                            a["days_left"] if a["days_left"] is not None else 10 ** 6))
    return out


def headline(row, today=None):
    """The one line the docket row should carry: what can be filed TODAY, and by when.

    Returns {"label", "status", "deadline", "days_left", "count"} or None when nothing is open.
    """
    acts = actions_for(row, today)
    live = [a for a in acts if a["status"] in ("open", "closing")]
    if not live:
        conditional = [a for a in acts if a["status"] == "conditional" and not a.get("weak")]
        if conditional:
            a = conditional[0]
            return {"label": a["instrument"], "statute": a["statute"], "fee": a["fee"],
                    "status": "conditional", "deadline": a["deadline"],
                    "days_left": a["days_left"], "count": len(conditional)}
        return None
    a = live[0]
    return {"label": a["instrument"], "statute": a["statute"], "fee": a["fee"],
            "status": a["status"], "deadline": a["deadline"], "days_left": a["days_left"],
            "count": len(live)}
