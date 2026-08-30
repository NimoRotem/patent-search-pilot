"""Who is filing, where they live, and who to write to: the data an ADS and a declaration need.

WHY THIS EXISTS AS ITS OWN THING.  A drafting project knows the title, a list of inventor names and
sometimes an applicant.  That is enough to write a specification and nowhere near enough to file
one.  37 CFR 1.76 wants a residence and a mailing address for every inventor, a correspondence
address, an application type and a docket number; 37 CFR 1.63 wants the same names and addresses
again on a paper somebody signs; 37 CFR 1.27 wants entity status certified rather than assumed.

Until this module existed, the package printed "(not supplied)" into those fields and called
itself complete.  It was not complete, and worse, it was silent about which field was missing, so
the gap surfaced at the Office rather than on the page.  ``gaps()`` is therefore the important
function here: it names the exact field, in the words the ADS uses, so the answer is one line of
typing rather than an investigation.

STORED IN THE PROJECT'S ``settings`` BLOB under ``filing``.  No migration, and a project saved
before a field existed gets today's default instead of a NULL.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

MAX_INVENTORS = 20

#  37 CFR 1.27 and 1.29. Status is certified, never assumed: claiming small entity fees without
#  being entitled to them is an improper payment, and the fee difference is large enough that
#  guessing is not a kindness.
ENTITY_CHOICES = (
    {"id": "undiscounted", "label": "Undiscounted (no discount claimed)"},
    {"id": "small", "label": "Small entity (37 CFR 1.27)"},
    {"id": "micro_income", "label": "Micro entity, gross income basis (37 CFR 1.29(a))"},
    {"id": "micro_institution",
     "label": "Micro entity, institution of higher education basis (37 CFR 1.29(d))"},
)
ENTITY_IDS = frozenset(item["id"] for item in ENTITY_CHOICES)

APPLICATION_TYPES = (
    {"id": "nonprovisional", "label": "Utility, nonprovisional (37 CFR 1.53(b))"},
    {"id": "provisional", "label": "Provisional (37 CFR 1.53(c))"},
)
APPLICATION_TYPE_IDS = frozenset(item["id"] for item in APPLICATION_TYPES)

INVENTOR_FIELDS = (
    ("given_name", "Given name", True),
    ("middle_name", "Middle name", False),
    ("family_name", "Family name", True),
    ("city", "City of residence", True),
    ("state", "State or province of residence", False),
    ("country", "Country of residence", True),
    ("mailing_address", "Mailing address", True),
    ("mailing_city", "Mailing address city", True),
    ("mailing_state", "Mailing address state or province", False),
    ("mailing_postcode", "Mailing address postal code", True),
    ("mailing_country", "Mailing address country", True),
)

TOP_FIELDS = (
    ("correspondence_name", "Correspondence name", True),
    ("correspondence_address", "Correspondence address", True),
    ("correspondence_city", "Correspondence city", True),
    ("correspondence_state", "Correspondence state or province", False),
    ("correspondence_postcode", "Correspondence postal code", True),
    ("correspondence_country", "Correspondence country", True),
    ("correspondence_email", "Correspondence email", True),
    ("correspondence_phone", "Correspondence telephone", False),
    ("customer_number", "USPTO customer number", False),
    ("docket_number", "Attorney docket number", False),
    ("applicant_kind", "Who the applicant is", False),
    ("applicant_name", "Applicant name, where the applicant is not the inventor", False),
    ("assignee_name", "Assignee to be printed on the patent", False),
    ("assignee_address", "Assignee address", False),
    ("domestic_benefit", "Domestic benefit or continuity claim (37 CFR 1.78)", False),
    ("foreign_priority", "Foreign priority claim (37 CFR 1.55)", False),
    ("prior_art_statement", "Information disclosure note", False),
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def defaults() -> dict[str, Any]:
    out: dict[str, Any] = {key: "" for key, _label, _required in TOP_FIELDS}
    out["entity_status"] = "undiscounted"
    out["application_type"] = "nonprovisional"
    out["applicant_kind"] = "inventor"
    out["inventors"] = []
    return out


def _text(value: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", "")).strip()[:limit]


def resolve(stored: Mapping[str, Any] | None,
            project: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Today's defaults, overridden by what is saved, seeded from the project where it is empty."""
    out = defaults()
    for key, value in dict(stored or {}).items():
        if key == "inventors":
            out["inventors"] = [_clean_inventor(row) for row in (value or [])][:MAX_INVENTORS]
        elif key in out:
            out[key] = value
    if not out["inventors"]:
        out["inventors"] = [_seed_inventor(name)
                            for name in split_names((project or {}).get("inventors"))]
    if not out["applicant_name"]:
        out["applicant_name"] = _text((project or {}).get("applicant"), 200)
    return out


def split_names(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[\n;]+", str(value or "")) if part.strip()][
        :MAX_INVENTORS]


def _seed_inventor(name: str) -> dict[str, str]:
    """Split a plain name into given and family so the ADS rows start half filled, not empty."""
    parts = [part for part in _text(name, 200).split() if part]
    row = {key: "" for key, _label, _required in INVENTOR_FIELDS}
    if len(parts) == 1:
        row["family_name"] = parts[0]
    elif parts:
        row["given_name"] = parts[0]
        row["family_name"] = parts[-1]
        row["middle_name"] = " ".join(parts[1:-1])
    return row


def _clean_inventor(row: Mapping[str, Any] | None) -> dict[str, str]:
    source = dict(row or {})
    return {key: _text(source.get(key), 300) for key, _label, _required in INVENTOR_FIELDS}


def full_name(row: Mapping[str, Any]) -> str:
    return " ".join(part for part in (str(row.get("given_name") or ""),
                                      str(row.get("middle_name") or ""),
                                      str(row.get("family_name") or "")) if part.strip()).strip()


def residence(row: Mapping[str, Any]) -> str:
    return ", ".join(part for part in (str(row.get("city") or ""), str(row.get("state") or ""),
                                       str(row.get("country") or "")) if part.strip())


def mailing_address(row: Mapping[str, Any]) -> str:
    line = str(row.get("mailing_address") or "").strip()
    tail = ", ".join(part for part in (str(row.get("mailing_city") or ""),
                                       str(row.get("mailing_state") or ""),
                                       str(row.get("mailing_postcode") or ""),
                                       str(row.get("mailing_country") or "")) if part.strip())
    return ", ".join(part for part in (line, tail) if part)


def correspondence_block(profile: Mapping[str, Any]) -> list[str]:
    lines = [str(profile.get("correspondence_name") or ""),
             str(profile.get("correspondence_address") or "")]
    lines.append(", ".join(part for part in (
        str(profile.get("correspondence_city") or ""),
        str(profile.get("correspondence_state") or ""),
        str(profile.get("correspondence_postcode") or ""),
        str(profile.get("correspondence_country") or "")) if part.strip()))
    for key in ("correspondence_email", "correspondence_phone"):
        if profile.get(key):
            lines.append(str(profile[key]))
    return [line for line in lines if line.strip()]


def clean(supplied: Mapping[str, Any] | None,
          stored: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate what the filing form sent. An unusable value is refused, never corrected."""
    out = resolve(stored)
    values = dict(supplied or {})
    known = {key for key, _label, _required in TOP_FIELDS} | {
        "entity_status", "application_type", "inventors"}
    for key in values:
        if key not in known:
            raise ValueError(f"{key!r} is not a filing field.")
    if "entity_status" in values:
        value = str(values["entity_status"] or "").strip()
        if value not in ENTITY_IDS:
            raise ValueError(f"{value!r} is not an entity status.")
        out["entity_status"] = value
    if "application_type" in values:
        value = str(values["application_type"] or "").strip()
        if value not in APPLICATION_TYPE_IDS:
            raise ValueError(f"{value!r} is not an application type.")
        out["application_type"] = value
    for key, label, _required in TOP_FIELDS:
        if key in values:
            out[key] = _text(values[key], 2000 if key in
                             ("domestic_benefit", "foreign_priority", "prior_art_statement")
                             else 300)
    if out.get("correspondence_email") and not _EMAIL_RE.match(out["correspondence_email"]):
        raise ValueError("The correspondence email is not a valid address.")
    if "inventors" in values:
        rows = values["inventors"] or []
        if not isinstance(rows, (list, tuple)):
            raise ValueError("The inventor list must be a list.")
        if len(rows) > MAX_INVENTORS:
            raise ValueError(f"An application here supports at most {MAX_INVENTORS} inventors.")
        out["inventors"] = [_clean_inventor(row) for row in rows]
    return out


def gaps(profile: Mapping[str, Any]) -> list[dict[str, str]]:
    """Exactly which field is missing, named the way the ADS names it.

    A missing field is a defect in the record, not a safe default. Saying "inventor information is
    incomplete" costs the reader an investigation; saying "inventor 2: city of residence" costs
    them four seconds.
    """
    out: list[dict[str, str]] = []
    inventors = list(profile.get("inventors") or [])
    if not inventors:
        out.append({"field": "Inventor 1", "rule": "37 CFR 1.41, 1.76(b)(1)",
                    "detail": "No inventor is named. Every application names at least one."})
    for index, row in enumerate(inventors, 1):
        for key, label, required in INVENTOR_FIELDS:
            if required and not str(row.get(key) or "").strip():
                out.append({"field": f"Inventor {index}: {label}",
                            "rule": "37 CFR 1.76(b)(1), 1.63(b)",
                            "detail": "The ADS and the declaration both carry this."})
    for key, label, required in TOP_FIELDS:
        if required and not str(profile.get(key) or "").strip():
            out.append({"field": label, "rule": "37 CFR 1.33(a), 1.76(b)(2)",
                        "detail": "The Office writes to this address about this application."})
    if profile.get("applicant_kind") == "juristic" and not profile.get("applicant_name"):
        out.append({"field": "Applicant name", "rule": "37 CFR 1.46",
                    "detail": "Where the applicant is not the inventor, the ADS must say who it "
                              "is."})
    return out


def public(profile: Mapping[str, Any]) -> dict[str, Any]:
    """The form, its values and the gaps, for a page that has to explain itself."""
    resolved = resolve(profile)
    return {
        "values": resolved,
        "inventor_fields": [{"key": key, "label": label, "required": required}
                            for key, label, required in INVENTOR_FIELDS],
        "fields": [{"key": key, "label": label, "required": required}
                   for key, label, required in TOP_FIELDS],
        "entity_choices": [dict(item) for item in ENTITY_CHOICES],
        "application_types": [dict(item) for item in APPLICATION_TYPES],
        "gaps": gaps(resolved),
    }


def inventor_lines(profile: Mapping[str, Any]) -> list[str]:
    out = []
    for index, row in enumerate(profile.get("inventors") or [], 1):
        out.append(f"{index}. {full_name(row) or '(name not supplied)'} - "
                   f"residence {residence(row) or '(not supplied)'} - "
                   f"mailing {mailing_address(row) or '(not supplied)'}")
    return out


def entity_label(profile: Mapping[str, Any]) -> str:
    value = str(profile.get("entity_status") or "undiscounted")
    return next((item["label"] for item in ENTITY_CHOICES if item["id"] == value), value)


def application_type_label(profile: Mapping[str, Any]) -> str:
    value = str(profile.get("application_type") or "nonprovisional")
    return next((item["label"] for item in APPLICATION_TYPES if item["id"] == value), value)


def summary(profile: Mapping[str, Any]) -> dict[str, Any]:
    resolved = resolve(profile)
    return {"inventor_count": len(resolved.get("inventors") or []),
            "entity_status": entity_label(resolved),
            "application_type": application_type_label(resolved),
            "gaps": gaps(resolved)}


def web_ads_sheet(profile: Mapping[str, Any], *, title: str,
                  drawing_sheets: int) -> str:
    """Every field of Patent Center's web ADS with the value to type into it.

    The web ADS validates on entry, which is why it is the route we recommend over uploading a
    PDF. That only helps if the person filling it in is not also guessing at the values, so this
    is the crib sheet: one line per field, in the order the form asks.
    """
    resolved = resolve(profile)
    lines = ["APPLICATION DATA SHEET - values to enter in Patent Center's web ADS", "",
             "Patent Center: " + "https://patentcenter.uspto.gov/", "",
             "1. INVENTOR INFORMATION (37 CFR 1.76(b)(1))", ""]
    for index, row in enumerate(resolved.get("inventors") or [], 1):
        lines += [f"  Inventor {index}",
                  f"    Given name        {row.get('given_name') or '(missing)'}",
                  f"    Middle name       {row.get('middle_name') or '(none)'}",
                  f"    Family name       {row.get('family_name') or '(missing)'}",
                  f"    Residence city    {row.get('city') or '(missing)'}",
                  f"    Residence state   {row.get('state') or '(none, and not required)'}",
                  f"    Residence country {row.get('country') or '(missing)'}",
                  f"    Mailing address   {row.get('mailing_address') or '(missing)'}",
                  f"    Mailing city      {row.get('mailing_city') or '(missing)'}",
                  f"    Mailing state     {row.get('mailing_state') or '(none, and not required)'}",
                  f"    Mailing postcode  {row.get('mailing_postcode') or '(missing)'}",
                  f"    Mailing country   {row.get('mailing_country') or '(missing)'}", ""]
    lines += ["2. CORRESPONDENCE INFORMATION (37 CFR 1.33(a), 1.76(b)(2))", ""]
    if resolved.get("customer_number"):
        lines.append(f"    Customer number   {resolved['customer_number']}")
    #  An optional field reads "(none)" and a required one reads "(missing)". They looked the
    #  same until a reviewer put the read-me's "every field is filled in" beside a correspondence
    #  telephone marked missing and had to work out which of the two was lying.
    required = {key for key, _label, needed in TOP_FIELDS if needed}
    for label, key in (("Name", "correspondence_name"), ("Address", "correspondence_address"),
                       ("City", "correspondence_city"), ("State", "correspondence_state"),
                       ("Postal code", "correspondence_postcode"),
                       ("Country", "correspondence_country"),
                       ("Email", "correspondence_email"), ("Telephone", "correspondence_phone")):
        empty = "(missing)" if key in required else "(none, and not required)"
        lines.append(f"    {label:<17} {resolved.get(key) or empty}")
    lines += ["", "3. APPLICATION INFORMATION (37 CFR 1.76(b)(3))", "",
              f"    Title             {title}",
              f"    Application type  {application_type_label(resolved)}",
              "    Subject matter    Utility",
              f"    Total drawing sheets  {int(drawing_sheets)}",
              "    Suggested figure for publication  FIG. 1",
              f"    Attorney docket number  {resolved.get('docket_number') or '(none)'}", "",
              "4. REPRESENTATIVE INFORMATION (37 CFR 1.76(b)(4))", "",
              "    None, unless a registered practitioner is appointed by power of attorney.", "",
              "5. DOMESTIC BENEFIT / NATIONAL STAGE INFORMATION (37 CFR 1.78)", "",
              f"    {resolved.get('domestic_benefit') or 'None claimed.'}", "",
              "6. FOREIGN PRIORITY INFORMATION (37 CFR 1.55)", "",
              f"    {resolved.get('foreign_priority') or 'None claimed.'}", "",
              "7. APPLICANT INFORMATION (37 CFR 1.46)", ""]
    if resolved.get("applicant_kind") == "juristic" and resolved.get("applicant_name"):
        lines.append(f"    Applicant  {resolved['applicant_name']}")
    else:
        lines.append("    The inventor is the applicant. Leave this section empty.")
    lines += ["", "8. ASSIGNEE INFORMATION (37 CFR 3.81(a))", "",
              f"    {resolved.get('assignee_name') or 'None. Leave empty.'}",
              f"    {resolved.get('assignee_address') or ''}", "",
              "9. ENTITY STATUS (37 CFR 1.27, 1.29)", "",
              f"    {entity_label(resolved)}", "",
              "A benefit or priority claim is only effective if it appears in the ADS. Putting it",
              "only in the specification does not make it.", ""]
    return "\n".join(lines)
