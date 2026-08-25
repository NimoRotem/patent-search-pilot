"""What has to be true before a document goes in a 37 CFR 1.290 envelope.

A concise description of relevance is filed into a live examination and is DISCARDED, not amended,
if it argues the merits. It is also useless if it cites something that is not prior art, wastes a
document slot on a second member of a family already cited, quotes a passage the reference does not
contain, or relies on a foreign-language passage no examiner can read. Each of those is a check
here, and each one either fixes the document or refuses to ship it.

The checks, in the order they run:

  qualify   Does this reference actually predate the target? Uses the app's own jurisdiction-
            neutral date engine (search_modes.classify_basis), so the answer is the same one the
            search itself used. NOT_PRIOR_ART is a hard block.
  exceptions 35 USC 102(b)(2)(C): a US application-publication reference commonly owned with the
            target is disqualified as 102(a)(2) art. The EPO has NO equivalent — an applicant's own
            earlier filing is full Article 54(3) novelty art against their later EP case. So a
            self-collision is reported as jurisdiction-split rather than simply dropped: dead in the
            US, lethal in Europe.
  family    One document per DOCDB family. Two members of the same family are the same disclosure
            and citing both spends two of the three free documents to say one thing.
  neutral   Strip argument. "Reads on", "anticipates", "renders obvious", "teaches away",
            "corresponds to the claimed" are all conclusions about patentability, and 1.290(a) is a
            description of relevance, not a rejection.
  quotes    Every quotation is checked against the reference's own stored text. A quotation that is
            not found is dropped, not softened.
  language  A relied-on passage in CN/DE/JP/KR gets a machine translation and a note saying so, per
            1.290(d)(3)'s translation requirement.

Nothing here silently improves a document: every intervention lands in `doc["compliance"]` so the
practitioner sees what was changed and why before signing.
"""
from __future__ import annotations

import datetime
import os
import re
import traceback

#  Module level, not inside qualify(): the forum rule below is read after the date block, so a
#  name bound only on the success path of a try is a NameError waiting for the first odd input.
import search_modes

MACHINE_TRANSLATION_NOTE = ("Machine translation of the relied-on passage; the original-language "
                            "text is reproduced above it.")

#  Conclusions about patentability. A submission under 1.290 may point at what a document says; it
#  may not tell the examiner what that means for the claims. Each pattern is paired with the neutral
#  phrasing that says the same factual thing.
_ARGUMENT = [
    (re.compile(r"\breads?\s+on\b", re.I), "corresponds in subject matter to"),
    (re.compile(r"\banticipat(?:es|ed|ing|ion)\b", re.I), "discloses"),
    (re.compile(r"\brenders?\s+(?:it\s+)?obvious\b", re.I), "discloses"),
    (re.compile(r"\bwould\s+have\s+been\s+obvious\b", re.I), "is disclosed"),
    (re.compile(r"\bteaches?\s+away\b", re.I), "describes"),
    (re.compile(r"\binvalidat(?:es|ing|ion)\b", re.I), "is relevant to"),
    (re.compile(r"\bunpatentable\b", re.I), "disclosed"),
    (re.compile(r"\bfails?\s+to\s+(?:be\s+)?patentab\w*\b", re.I), "is disclosed"),
    (re.compile(r"\banalogous\s+art\b", re.I), "the same field"),
    (re.compile(r"\bmotivation\s+to\s+combine\b", re.I), "the described arrangement"),
    (re.compile(r"\bone\s+of\s+ordinary\s+skill\b", re.I), "the description"),
    (re.compile(r"\bthe\s+claimed\s+invention\b", re.I), "the recited subject matter"),
    (re.compile(r"\bshould\s+be\s+rejected\b", re.I), "is described"),
    (re.compile(r"\bis\s+not\s+novel\b", re.I), "is described"),
    (re.compile(r"\blacks?\s+(?:novelty|inventive\s+step)\b", re.I), "is described"),
]

#  Words that make a sentence an assertion about the application rather than about the document.
_ARGUMENT_SENTENCE = re.compile(
    r"[^.]*\b(?:therefore|thus|accordingly|hence)\b[^.]*\b(?:claim|patentab\w+|novel\w*|obvious\w*)"
    r"\b[^.]*\.", re.I)

_NON_LATIN = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_GERMANIC = re.compile(r"\b(?:der|die|das|und|nicht|eine[rnms]?|mit|auf|ist|wird|dass|"
                       r"Vorrichtung|Verfahren|gekennzeichnet)\b")


# --------------------------------------------------------------------------- dates


def _as_date(v):
    if isinstance(v, datetime.date):
        return v
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(v or ""))
    if not m:
        return None
    try:
        return datetime.date(*[int(x) for x in m.groups()])
    except ValueError:
        return None


def _country_of(b):
    """Two-letter office code for a reference, from the field or from the number itself."""
    c = str(b.get("country") or "").strip().upper()
    if len(c) == 2:
        return c
    m = re.match(r"^\s*([A-Z]{2})", str(b.get("pub") or b.get("publication_number") or "").upper())
    return m.group(1) if m else ""


def qualify(doc, subject_efd, mode="novelty", forum="US"):
    """Is this reference prior art against the target's effective filing date, IN THIS FORUM?

    Delegates to the app's own date engine so a document can never be filed on a basis the search
    itself would not have used. NOT_PRIOR_ART blocks the document outright.

    The dates are only half the question and the other half is the office. 35 U.S.C. 102(a)(2)
    reaches US patents, US pre-grant publications and PCT applications designating the United
    States — nothing else. A JP, CN, DE or EP national publication that published AFTER the
    target's effective filing date is therefore not prior art in the United States at all, however
    early it was filed. Reported by counsel on 2026-08-20 against a real report: claim 10 of
    US-2026/0070232 was credited to JP-2026-002795-A, published 2026-01-08 against a target whose
    effective filing date is 2024-09-09. Right on the dates, unavailable at the USPTO, and a
    submission that relies on it is answering a question nobody asked.
    """
    b = doc["biblio"]
    efd = _as_date(subject_efd)
    if efd is None:
        return {"basis": "unknown", "blocked": False,
                "note": "The target's effective filing date was not supplied, so prior-art status "
                        "could not be checked. Confirm it before filing."}
    try:
        s = search_modes.Subject(number="target", efd=efd)
        basis = search_modes.classify_basis(
            {"publication_date": _as_date(b.get("publication_date")),
             "earliest_priority_date": _as_date(b.get("priority_date")),
             "filing_date": _as_date(b.get("filing_date"))}, s)
        ok = search_modes.usable_for(basis, search_modes.Mode(mode))
    except Exception:
        traceback.print_exc()
        return {"basis": "unknown", "blocked": False,
                "note": "Prior-art status could not be computed; confirm it before filing."}
    out = {"basis": basis.value, "blocked": basis.value == "not_prior_art", "forum": forum}
    if basis.value == "not_prior_art":
        out["note"] = ("This document does not predate the target's effective filing date of %s "
                       "and is not prior art against it." % efd.isoformat())
    elif basis.value == "secret_prior_art":
        country = _country_of(b)
        if not search_modes.secret_art_reaches(country, forum):
            #  Blocked, not merely footnoted: it is not prior art in this forum, and the rule the
            #  document would be filed under does not reach it.
            out["blocked"] = True
            out["forum_bar"] = country
            out["note"] = search_modes.secret_art_note(country, forum)
        else:
            out["note"] = ("Earlier-filed, later-published. Available under 35 U.S.C. 102(a)(2) / "
                           "EPC Art. 54(3) for novelty only, not for obviousness or inventive "
                           "step.")
    elif basis.value == "priority_interval":
        out["note"] = ("Published inside the target's priority interval; it becomes prior art only "
                       "if the priority claim fails. Flagged rather than relied on.")
    elif not ok:
        out["note"] = "Not usable under %s on this basis." % mode
    return out


# --------------------------------------------------------------------------- self-collision


#  Strings that appear in an assignee field INSTEAD of an owner. Two documents both carrying one
#  of these are not commonly owned; they are both unattributed. Matching on them produced a false
#  self-collision on EP-2390518-A1 ("Individual") on 2026-08-19.
_PLACEHOLDER_PARTY = {
    "individual", "individuals", "unassigned", "none", "n a", "na", "unknown", "private",
    "sole inventor", "self", "applicant", "not assigned",
}


def _norm_party(name):
    t = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())
    t = re.sub(r"\b(gmbh|ag|kg|co|kgaa|inc|llc|ltd|limited|corp|corporation|company|se|sa|nv|bv|"
               r"holding|holdings|group|und|and)\b", " ", t)
    return " ".join(t.split())


def self_collision(doc, target_assignees):
    """Is this the target owner's OWN earlier filing?

    Worth its own check because the two big jurisdictions disagree completely:

    * US — 35 U.S.C. 102(b)(2)(C) disqualifies a 102(a)(2) reference that was, not later than the
      effective filing date, owned by or subject to an obligation of assignment to the same person.
      A commonly-owned earlier publication is therefore NOT available as prior art, and citing it
      in a 1.290 submission invites the examiner to withdraw it.
    * EPO — there is no common-ownership exception. Art. 54(3) makes an applicant's own earlier
      European filing full novelty art against their later case. Self-collision is real and it is
      the applicant's own document that kills the claim.

    So this never silently drops the document. It reports the split, because the same reference can
    be worthless in Washington and decisive in Munich.
    """
    if not target_assignees:
        return None
    mine = {_norm_party(a) for a in target_assignees if _norm_party(a)}
    mine -= _PLACEHOLDER_PARTY
    theirs = [_norm_party(a) for a in (doc["biblio"].get("assignee") or "").split(",")]
    theirs = [t for t in theirs if t and t not in _PLACEHOLDER_PARTY]
    #  Substring matching is what catches "J. Schmalz GmbH" against "Schmalz AG", but on a short
    #  token it matches everything, so it is only allowed once both sides are a real name.
    hit = [t for t in theirs
           if t in mine or any((t in m or m in t) and min(len(t), len(m)) >= 4 for m in mine)]
    if not hit:
        return None
    basis = (doc.get("compliance", {}).get("qualify") or {}).get("basis")
    us_disqualified = basis == "secret_prior_art"
    return {
        "same_owner": True,
        "us_disqualified": us_disqualified,
        "note": (
            "SELF-COLLISION. This document appears to be commonly owned with the target (%s). "
            % (doc["biblio"].get("assignee") or "same assignee") +
            ("In the United States it is most likely disqualified as prior art entirely under "
             "35 U.S.C. 102(b)(2)(C), because a 102(a)(2) reference commonly owned as of the "
             "effective filing date is excepted; filing it may simply have the reference "
             "withdrawn. At the EPO there is no common-ownership exception and the same document "
             "is full Article 54(3) novelty art against the applicant's own later European case. "
             "Dead in the US, lethal in Europe: confirm ownership as of the effective filing "
             "date before deciding where to use it."
             if us_disqualified else
             "It predates the target as public prior art, so 102(b)(2)(C) does not reach it "
             "(that exception applies only to 102(a)(2) art). It remains citable in both "
             "jurisdictions, but confirm the ownership position before filing.")),
    }


# --------------------------------------------------------------------------- family


def collapse_families(docs):
    """One document per DOCDB family, keeping the member with the most verified evidence.

    Two members of one family are one disclosure. Citing both spends two of the three documents a
    first-time submitter may file free to say a single thing, and an examiner reading the second
    finds nothing the first did not already show.
    """
    best, order = {}, []
    for d in docs:
        fam = str(d.get("family_id") or "").strip() or ("solo:%s" % d["pub"])
        key = (sum(1 for r in d["rows"] if r.get("strong")), len(d["rows"]))
        if fam not in best:
            best[fam] = [key, d, []]
            order.append(fam)
            continue
        prev_key, prev_d, losers = best[fam]
        if key > prev_key:
            best[fam] = [key, d, losers + [prev_d]]
        else:
            losers.append(d)
    kept, notes = [], []
    for fam in order:
        _key, winner, losers = best[fam]
        kept.append(winner)
        for l in losers:
            notes.append("%s dropped: same DOCDB family (%s) as %s, which carries more verified "
                         "evidence. Two members of one family are one disclosure."
                         % (l["pub"], fam, winner["pub"]))
    return kept, notes


# --------------------------------------------------------------------------- neutral language


def neutralise(text):
    """-> (clean_text, [what was changed]). Argument is replaced, not merely flagged."""
    changed = []
    out = str(text or "")
    for pat, repl in _ARGUMENT:
        found = pat.search(out)
        if found:
            changed.append('"%s" -> "%s"' % (found.group(0), repl))
            out = pat.sub(repl, out)
    m = _ARGUMENT_SENTENCE.search(out)
    if m:
        changed.append("removed a concluding sentence about patentability")
        out = _ARGUMENT_SENTENCE.sub("", out)
    return " ".join(out.split()).strip(" ,;"), changed


# --------------------------------------------------------------------------- quotations


def _norm_for_match(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def verify_quotes(doc, source_text):
    """Drop any quotation the reference's own text does not contain.

    The passage was captured by the reader and has been through grounding, but a filed paper is a
    representation that the document says this. Re-checking against the stored full text costs
    nothing and is the difference between a quotation and a paraphrase in quotation marks.
    """
    hay = _norm_for_match(source_text)
    checked, dropped = 0, 0
    if not hay:
        return {"checked": 0, "dropped": 0,
                "note": "No stored full text for this reference, so its quotations could not be "
                        "re-verified against the source. Check them before filing."}
    for r in doc["rows"]:
        q = (r.get("quote") or "").strip()
        if not q:
            continue
        checked += 1
        needle = _norm_for_match(q.rstrip(" …."))
        #  Match on a solid interior run rather than the whole span: the stored passage is capped
        #  mid-sentence and may carry the reader's ellipsis.
        probe = " ".join(needle.split()[:12])
        if probe and probe in hay:
            continue
        r["quote"] = ""
        r["quote_unverified"] = True
        dropped += 1
    return {"checked": checked, "dropped": dropped,
            "note": ("%d of %d quotations could not be found in the stored text of this reference "
                     "and were removed; the finding and its citation remain." % (dropped, checked)
                     if dropped else "")}


# --------------------------------------------------------------------------- language


def needs_translation(text):
    t = str(text or "")
    if _NON_LATIN.search(t):
        return "CJK"
    if len(_GERMANIC.findall(t)) >= 2:
        return "de"
    return ""


_FOREIGN_OFFICE = {"CN": "Chinese", "JP": "Japanese", "KR": "Korean", "DE": "German",
                   "FR": "French", "ES": "Spanish", "RU": "Russian", "TW": "Chinese (TW)"}


def source_language(pub):
    """The office a document issued from, when that means its text is not natively English."""
    code = str(pub or "").strip().upper()[:2]
    return _FOREIGN_OFFICE.get(code, "")


def translate_rows(doc, tier="strong"):
    """Machine-translate any relied-on passage that is not in English, and say that it is one.

    1.290(d)(3) requires an English translation of any non-English document relied on. The passage
    is what is relied on here, so the passage is what gets translated; the original stays above it
    so the examiner can check.
    """
    lang = source_language(doc.get("pub"))
    targets = [r for r in doc["rows"] if r.get("quote") and needs_translation(r["quote"])]
    if not targets:
        #  The corpus already stores English full text for CN/JP/KR via the HimmPat source, so
        #  there is nothing to translate — but what is quoted is still a translation of a
        #  foreign-language document, and 1.290(d)(3) wants that stated rather than left for the
        #  examiner to infer from the country code.
        if lang:
            return {"translated": 0, "pre_translated": True,
                    "note": ("This is a %s-language document. The passages relied on are quoted "
                             "from an English translation held in the search corpus, not from the "
                             "original text. A verified translation of the relied-on portions "
                             "should accompany the filing." % lang)}
        return {"translated": 0}
    try:
        import llm
    except Exception:
        return {"translated": 0, "note": "Translation unavailable; non-English passages were left "
                                         "in the original and must be translated before filing."}
    sys_p = ("Translate each supplied patent passage into English. Return JSON "
             '{"rows":[{"id":<int>,"english":"..."}]}. Translate only, do not summarise, '
             "explain, or add anything not in the source.")
    payload = {"rows": [{"id": i, "text": r["quote"][:900]} for i, r in enumerate(targets)]}
    try:
        import json as _json
        got = llm.chat_json(sys_p, _json.dumps(payload, ensure_ascii=False),
                            max_tokens=4000, tier=tier) or {}
    except Exception:
        traceback.print_exc()
        return {"translated": 0, "note": "Translation call failed; non-English passages were left "
                                         "in the original."}
    by_id = {int(r["id"]): r.get("english") for r in (got.get("rows") or [])
             if isinstance(r, dict) and r.get("id") is not None}
    n = 0
    for i, r in enumerate(targets):
        eng = " ".join(str(by_id.get(i) or "").split())
        if not eng:
            continue
        r["quote_original"] = r["quote"]
        r["quote"] = eng
        r["quote_translated"] = True
        n += 1
    return {"translated": n,
            "note": (MACHINE_TRANSLATION_NOTE if n else
                     "Non-English passages could not be translated and were left in the original.")}


# --------------------------------------------------------------------------- driver


def apply(docs, subject, source_text_for, mode="novelty", target_assignees=None, forum="US"):
    """Run every check over a built document set. -> (docs_to_file, blocked, family_notes).

    `forum` decides which office's rules the date check answers to. A 1.290 submission goes to the
    USPTO, so it defaults there, and a reference available only as later-published secret art from
    an office 102(a)(2) does not reach is blocked rather than footnoted.
    """
    kept, blocked = [], []
    for d in docs:
        c = d.setdefault("compliance", {})
        #  A FILE-WRAPPER DOCUMENT IS NOT PRIOR ART AND IS NOT OFFERED AS ANY. An office action is
        #  a printed publication under 1.290(a) whose content is an examiner's findings on this
        #  very family; date-checking it against the target's own filing date would block the one
        #  document whose whole value is that it POSTDATES the application and discusses it.
        if d.get("not_prior_art_document"):
            c["qualify"] = {
                "basis": "not_a_reference", "blocked": False, "forum": forum,
                "note": ("This is an Office document from the file wrapper of a related "
                         "application, submitted as a printed publication under 37 CFR 1.290(a). "
                         "It is not offered as prior art, so the effective-filing-date comparison "
                         "does not apply to it.")}
            c["quotes"] = {"checked": 0, "note": "No quotation from a reference to verify."}
            c["translation"] = {"translated": 0}
            kept.append(d)
            continue
        c["qualify"] = qualify(d, subject.get("efd"), mode=mode, forum=forum)
        if c["qualify"].get("blocked"):
            blocked.append({"pub": d["pub"], "why": c["qualify"]["note"]})
            continue
        sc = self_collision(d, target_assignees or [])
        if sc:
            c["self_collision"] = sc
        c["quotes"] = verify_quotes(d, source_text_for(d["pub"]))
        #  A QUOTATION THAT FAILED VERIFICATION INVALIDATES THE ROW THAT OFFERED IT.
        #
        #  `verify_quotes` used to blank the quote and keep the row, which left the paper asserting
        #  "this document discloses X" with nothing behind it. That is worse than saying nothing on
        #  two counts: MPEP 1134.01 says a bare assertion of relevance is not a concise description,
        #  and the row came from a pass whose own quotation could not be found in the document, so
        #  the assertion is exactly the one least worth trusting.
        #
        #  Only rows that OFFERED a passage and lost it are dropped. A row whose bar was "taught,
        #  no quotable passage" never claimed one and is untouched.
        before = len(d["rows"])
        d["rows"] = [r for r in d["rows"] if not r.get("quote_unverified")]
        c["rows_dropped"] = before - len(d["rows"])
        if not d["rows"]:
            #  Nothing survived, so there is no description to file. A document with no described
            #  relevance may not be listed: 1.290(d)(2).
            blocked.append({"pub": d["pub"],
                            "why": "every row for this document rested on a quotation that could "
                                   "not be found in the document itself, so there is nothing left "
                                   "to describe"})
            continue
        c["translation"] = translate_rows(d)
        edits = []
        for r in d["rows"]:
            for field in ("disclosure", "note"):
                clean, changed = neutralise(r.get(field))
                if changed:
                    r[field] = clean
                    edits.extend(changed)
        clean_sum, changed_sum = neutralise(d.get("summary"))
        if changed_sum:
            d["summary"] = clean_sum
            edits.extend(changed_sum)
        c["neutralised"] = edits
        kept.append(d)
    kept, family_notes = collapse_families(kept)
    for i, d in enumerate(kept):
        d["n"] = i + 1
    return kept, blocked, family_notes
