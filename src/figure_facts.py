"""What is actually on the sheet, and whether the specification agrees with it.

THE PROBLEM THIS SOLVES.  This product stopped drawing in August 2026: sheets are uploaded, and
what arrives is whatever the draftsperson sent.  From that moment the drawing stopped being
something the text could be assumed to match, and the specification became the half that has to
move.  Every drawing defect a human reviewer found on the first real filing was of that shape:

  * a port the claims recited "in fluid communication with the chamber" that carried no numeral
    and appeared on no sheet - 37 CFR 1.83(a);
  * a Brief Description that hedged, "an enlarged portion of the same figure may illustrate",
    about a view that existed on the sheet and had no number of its own - 37 CFR 1.84(u);
  * two arrangements printed side by side under one FIG. 3 with the word "OR" between them;
  * a numeral pair, 14 and 16, swapped on one sheet against the definition in the text;
  * four cross-references in the detailed description that named a figure which does not show the
    thing the sentence is about;
  * a reference numeral key table printed on a sheet, and descriptive labels duplicating numerals.

None of those is findable in the text alone, and none of them needs a human once you know what is
on the sheet.  So: one vision pass per uploaded image producing a plain inventory of legends,
numerals, lead lines, labels and geometry, cached on the image's own hash; then a set of
deterministic comparisons between that inventory and the draft.  The vision pass is asked only for
what it can see.  Every judgment is made here, in code that can be read and tested.

COST.  One call per distinct image, cached for ever on the sha256 of the bytes, under the app's
daily metered-spend cap.  Re-uploading the same sheet costs nothing.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
from typing import Any, Iterable, Mapping, Sequence

import filing_rules

VISION_MODEL = os.environ.get("FILING_FIGURE_VISION_MODEL", "gemini-2.5-pro").strip()
PROMPT_VERSION = "sheet-inventory-v2-per-view"
MAX_OUTPUT_TOKENS = max(
    8_000, min(int(os.environ.get("FILING_FIGURE_VISION_MAX_TOKENS", "48000")), 64_000))
SPEND_APP = "patent-drafting"
#  Which provider reads a sheet first. Vertex by default: it needs no key from this VM, and the
#  Anthropic key on this host shares a usage window with everything else that holds it.
PREFER_ANTHROPIC = os.environ.get(
    "FILING_FIGURE_VISION_PREFER_ANTHROPIC", "0").strip().lower() in {"1", "true", "yes", "on"}

_FIG_RE = re.compile(r"\bFIGS?\.?\s*(\d{1,3})([A-Za-z]?)\b", re.IGNORECASE)
#  A whole citation, including the list or range that follows the word: "FIGS. 1 and 2",
#  "FIGS. 2-4", "FIGS. 3A, 3B and 3C".
_FIG_GROUP_RE = re.compile(
    r"\bFIGS?\.?\s*\d{1,3}[A-Za-z]?"
    r"(?:\s*(?:,|and|or|through|to|-|–|—)\s*\d{1,3}[A-Za-z]?)*",
    re.IGNORECASE)
_FIG_ITEM_RE = re.compile(r"(\d{1,3})([A-Za-z]?)")
_RANGE_JOIN_RE = re.compile(r"(?:-|–|—|through|to)", re.IGNORECASE)
_NUMERAL_RE = re.compile(r"(?<![\w.])(\d{1,4}[a-z]?)(?![\w%])")
_SENTENCE_RE = re.compile(r"(?<=[.;:])\s+(?=[A-Z(])")

#  A Brief Description that gestures at a view instead of numbering it. Each of these was written
#  by a model that knew the sheet held something extra and had no number to give it.
_HEDGED_VIEW_RES = (
    re.compile(r"\ban?\s+(?:enlarged|magnified|detail(?:ed)?|inset|broken[- ]out)\s+"
               r"(?:portion|part|view|region|area|detail)\b[^.]{0,120}?\b(?:of|from)\s+the\s+same\s+"
               r"(?:figure|view|sheet)\b", re.IGNORECASE),
    re.compile(r"\b(?:may|can|could)\s+(?:also\s+)?(?:illustrate|show|depict|be\s+shown)\b",
               re.IGNORECASE),
    re.compile(r"\ban\s+(?:undesignated|unnumbered|additional)\s+view\b", re.IGNORECASE),
    re.compile(r"\bnot\s+separately\s+(?:numbered|designated)\b", re.IGNORECASE),
)
#  Two arrangements under one number. "FIG. 3 shows A or, alternatively, B" is two views.
_ALTERNATIVE_IN_ONE_VIEW_RE = re.compile(
    r"\b(?:alternative(?:ly)?|or\s+alternatively|either\b[^.]{0,60}\bor\b)\b", re.IGNORECASE)

_NUMERAL_ITEM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "lead_lines", "points_at", "designates_one_part"],
    "properties": {
        "value": {"type": "string", "description": "The reference numeral as printed."},
        "lead_lines": {"type": "integer",
                       "description": "How many separate lead lines carry this numeral IN THIS "
                                      "VIEW. Almost always 1."},
        "points_at": {"type": "string",
                      "description": "What each lead line lands on, in a few words. When there "
                                     "is more than one, describe every one of them."},
        "designates_one_part": {
            "type": "boolean",
            "description": "True when every lead line for this numeral lands on the same part, "
                           "or on several instances of the same part. False when they land on "
                           "different parts, which is the defect: one numeral must never name "
                           "two different things. Always true when there is one lead line."},
        "matches_declared_part": {
            "type": "string",
            "enum": ["yes", "no", "unclear", "not_declared"],
            "description": "Compare what the lead line lands on against the part this numeral is "
                           "declared to be in the table you were given. 'no' only when the "
                           "drawing plainly contradicts the table, for instance the numeral for "
                           "the first side pointing at the second side."},
    },
}

VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["views", "text_labels", "numeral_key_table", "divider_rules",
                 "sheet_number_text", "smallest_reference_character_height_fraction"],
    "properties": {
        "views": {
            "type": "array",
            "description": "Every separate picture on this sheet. A picture with no FIG. number "
                           "of its own is still a view: report it with an empty legend.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["legend", "printed_caption", "kind", "bbox", "numerals",
                             "smallest_reference_character_height_fraction"],
                "properties": {
                    "legend": {"type": "string",
                               "description": "The figure number exactly as printed, e.g. "
                                              "'FIG. 2'. Empty when this picture has none."},
                    "printed_caption": {
                        "type": "string",
                        "description": "Any other words printed as this picture's title, such as "
                                       "'ENLARGED DETAIL'. Empty when there are none."},
                    "kind": {"type": "string",
                             "description": "perspective, sectional, plan, elevation, enlarged "
                                            "detail, flow chart, or other."},
                    "bbox": {"type": "array", "minItems": 4, "maxItems": 4,
                             "items": {"type": "number"},
                             "description": "x0,y0,x1,y1 as fractions of the image, covering the "
                                            "whole view including its legend and lead lines."},
                    "numerals": {"type": "array", "items": _NUMERAL_ITEM,
                                 "description": "Every reference numeral printed in this view."},
                    "smallest_reference_character_height_fraction": {
                        "type": "number",
                        "description": "Cap height of the smallest reference numeral IN THIS "
                                       "VIEW, as a fraction of the WHOLE image height."},
                },
            },
        },
        "text_labels": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "duplicates_numeral"],
                "properties": {
                    "text": {"type": "string"},
                    "duplicates_numeral": {
                        "type": "string",
                        "description": "The reference numeral this words-label names the same "
                                       "part as, or an empty string."},
                },
            },
        },
        "numeral_key_table": {
            "type": "boolean",
            "description": "True if a list or table of reference numerals and part names is "
                           "printed anywhere on the sheet."},
        "divider_rules": {
            "type": "integer",
            "description": "How many horizontal or vertical rules separate one view from another "
                           "on this sheet."},
        "sheet_number_text": {
            "type": "string",
            "description": "A sheet number already drawn into the artwork, such as '1/5', or an "
                           "empty string."},
        "smallest_reference_character_height_fraction": {
            "type": "number",
            "description": "Cap height of the smallest reference numeral on the sheet, as a "
                           "fraction of the image height."},
    },
}

VISION_SYSTEM = (
    "You are inspecting one patent drawing sheet and reporting an inventory of it. Report only "
    "what you can see. Do not infer anything from what a patent drawing usually contains, do not "
    "correct what you see, and never leave a defect out because it looks like a mistake: the "
    "mistakes are the point.\n\n"
    "A VIEW is one separate picture. Two pictures side by side are two views even when only one "
    "of them carries a FIG. number, even when a word like OR sits between them, and even when "
    "one of them is a magnified circle labelled ENLARGED DETAIL. Report every picture, and give "
    "the ones with no figure number an empty legend.\n\n"
    "Attribute every reference numeral to the view it is printed in, and count its lead lines "
    "WITHIN THAT VIEW only. A numeral that appears once in each of two views has one lead line "
    "in each. A numeral placed twice inside one view has two, and you must say what each of them "
    "lands on.\n\n"
    "You are given the draft's own reference numeral table. For each numeral on the sheet, say "
    "whether what its lead line lands on is consistent with the part the table declares it to "
    "be. Answer 'no' only for a plain contradiction, such as the numeral for the first side "
    "pointing at the second side, and 'unclear' whenever the view does not settle it.\n\n"
    "Measure the smallest reference numeral's cap height as a fraction of the whole image height "
    "and report it as a decimal."
)


# =============================================================================================
# The vision pass
# =============================================================================================
def sheet_key(png: bytes) -> str:
    return hashlib.sha256(bytes(png or b"")).hexdigest()


def numeral_context(numerals: Sequence[Mapping[str, str]]) -> str:
    rows = [f"{str(row.get('numeral') or '').strip()} = {str(row.get('part') or '').strip()}"
            for row in numerals or [] if str(row.get("numeral") or "").strip()]
    return "\n".join(rows)


def inspect_sheet(png: bytes, *, label: str = "",
                  numerals: Sequence[Mapping[str, str]] = (),
                  cache: bool = True, project_id: int = 0) -> dict[str, Any]:
    """One inventory of one sheet, cached on the image bytes and the numeral table together.

    The table is part of the key because it is part of the question: "is numeral 14 pointing at
    the part the draft says 14 is" has a different answer after the draft renames a part.
    """
    digest = sheet_key(png)
    context = numeral_context(numerals)
    if cache:
        stored = _cache_get(digest, context)
        if stored:
            return stored
    result = _vision_call(png, label=label, context=context, project_id=project_id)
    result["sha256"] = digest
    result["label"] = str(label or "")
    if cache:
        _cache_put(digest, context, result)
    return result


def _cache_key(digest: str, context: str) -> str:
    import draft_figures
    return draft_figures._analysis_cache_key(
        "filing-sheet-inventory", digest.encode("ascii"), context, VISION_MODEL, PROMPT_VERSION)


def _cache_get(digest: str, context: str) -> dict[str, Any] | None:
    try:
        import draft_figures
        return draft_figures._analysis_cache_get(_cache_key(digest, context))
    except Exception:                                              # noqa: BLE001 - cache only
        return None


def _cache_put(digest: str, context: str, result: Mapping[str, Any]) -> None:
    try:
        import draft_figures
        draft_figures._analysis_cache_put(
            _cache_key(digest, context), stage="filing-sheet-inventory", provider="vertex",
            model=VISION_MODEL, prompt_version=PROMPT_VERSION, result=dict(result))
    except Exception:                                              # noqa: BLE001 - cache only
        pass


def _guard():
    try:
        from llm_spend_guard import SpendGuard
        return SpendGuard(SPEND_APP)
    except Exception:                                              # noqa: BLE001
        return None


def _vision_call(png: bytes, *, label: str, context: str = "",
                 project_id: int = 0) -> dict[str, Any]:
    """Vertex first, because it needs no key from this VM; Anthropic when a key is configured."""
    guard = _guard()
    if guard is not None:
        guard.check()
    user = (f"Inspect this sheet. The draft calls it {label or 'an uploaded drawing'}. "
            "Report what is printed on it.\n\n"
            + (f"The draft's reference numeral table:\n{context}\n"
               if context else "The draft supplied no reference numeral table.\n"))
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    #  TWO PROVIDERS, AND EITHER ONE MAY BE DOWN. Vertex leads because it needs no key from this
    #  VM at all. Whichever runs first, a failure falls through to the other rather than
    #  propagating: on 2026-08-30 the Anthropic key was inside its usage window and every sheet
    #  in a live draft came back as "this sheet could not be inspected", which reads as a defect
    #  in the applicant's drawings and is nothing of the kind.
    routes = [("vertex", lambda: _vertex_inventory(png, user=user))]
    if api_key:
        route = ("anthropic", lambda: _anthropic_inventory(png, user=user, api_key=api_key))
        routes = [route] + routes if PREFER_ANTHROPIC else routes + [route]
    failures = []
    for name, call in routes:
        try:
            payload, usage, model = call()
        except Exception as exc:                                   # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        if guard is not None:
            try:
                from llm_spend_guard import price
                guard.record(usd=price(model, usage), model=model, usage=usage,
                             detail="filing sheet inventory")
            except Exception:                                      # noqa: BLE001
                pass
        #  Only on a cache MISS, which is the only place a sheet actually costs anything: the
        #  caller reaches this function once per distinct image and never again.
        if project_id:
            try:
                import draft_usage
                draft_usage.record(int(project_id), source="figures", model=model, usage=usage)
            except Exception:                                      # noqa: BLE001
                pass
        result = normalize(payload)
        result["provider"] = name
        result["model"] = model
        return result
    raise RuntimeError("No vision provider could read this sheet. " + " / ".join(failures))


def _vertex_inventory(png: bytes, *, user: str) -> tuple[dict[str, Any], dict[str, int], str]:
    import llm
    from google.genai.types import GenerateContentConfig, HttpOptions, Part
    response = llm._client().models.generate_content(
        model=VISION_MODEL,
        contents=[Part.from_bytes(data=bytes(png), mime_type="image/png"), user],
        config=GenerateContentConfig(
            system_instruction=VISION_SYSTEM, response_mime_type="application/json",
            response_json_schema=VISION_SCHEMA, temperature=0,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            http_options=HttpOptions(timeout=300_000)))
    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, dict):
        text = str(getattr(response, "text", "") or "")
        try:
            parsed = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            #  Almost always the output ceiling rather than a malformed answer, and reporting it
            #  as "invalid JSON" sends the next person looking in the wrong place.
            raise RuntimeError(
                "The sheet inventory came back incomplete after "
                f"{len(text)} characters, which is the output ceiling rather than a bad answer. "
                f"Raise FILING_FIGURE_VISION_MAX_TOKENS above {MAX_OUTPUT_TOKENS}.") from exc
    meta = getattr(response, "usage_metadata", None)
    usage = {"input_tokens": int(getattr(meta, "prompt_token_count", 0) or 0) if meta else 0,
             "output_tokens": int(getattr(meta, "candidates_token_count", 0) or 0) if meta else 0}
    return parsed, usage, VISION_MODEL


def _anthropic_inventory(png: bytes, *, user: str,
                         api_key: str) -> tuple[dict[str, Any], dict[str, int], str]:
    import draft_figures
    model = os.environ.get("FILING_FIGURE_VISION_MODEL_ANTHROPIC", "claude-opus-5").strip()
    payload = {
        "model": model, "max_tokens": MAX_OUTPUT_TOKENS, "system": VISION_SYSTEM,
        "tools": [{"name": "report", "description": "Report the inventory of this sheet.",
                   "input_schema": VISION_SCHEMA}],
        "tool_choice": {"type": "tool", "name": "report"},
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(bytes(png)).decode("ascii")}},
            {"type": "text", "text": user}]}],
    }
    response = draft_figures._anthropic_endpoint_message(payload, api_key=api_key)
    block = next((item for item in (response.get("content") or [])
                  if item.get("type") == "tool_use"), None)
    return (dict(block.get("input") or {}) if block else {},
            dict(response.get("usage") or {}), model)


def normalize(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Take the model's answer down to the shape the checks below expect."""
    source = dict(payload or {})

    def _bbox(value: Any) -> list[float]:
        try:
            numbers = [max(0.0, min(1.0, float(item))) for item in list(value)[:4]]
        except (TypeError, ValueError):
            return [0.0, 0.0, 1.0, 1.0]
        if len(numbers) != 4:
            return [0.0, 0.0, 1.0, 1.0]
        x0, y0, x1, y1 = numbers
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]

    def _numerals(rows: Any) -> list[dict[str, Any]]:
        out = []
        for item in rows or []:
            value = re.sub(r"[^0-9a-zA-Z]", "", str((item or {}).get("value") or "")).lower()
            if not value or not value[0].isdigit():
                continue
            out.append({"value": value,
                        "lead_lines": max(1, int((item or {}).get("lead_lines") or 1)),
                        "points_at": " ".join(
                            str((item or {}).get("points_at") or "").split())[:220],
                        "designates_one_part": bool(
                            (item or {}).get("designates_one_part", True)),
                        "matches_declared_part": str(
                            (item or {}).get("matches_declared_part") or "unclear")})
        return out

    def _height(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value or 0.0)))
        except (TypeError, ValueError):
            return 0.0

    views = []
    for item in source.get("views") or []:
        views.append({
            "legend": canonical_legend(str((item or {}).get("legend") or "")),
            "printed_caption": " ".join(
                str((item or {}).get("printed_caption") or "").split())[:120],
            "kind": str((item or {}).get("kind") or "")[:60],
            "bbox": _bbox((item or {}).get("bbox")),
            "numerals": _numerals((item or {}).get("numerals")),
            "character_height": _height(
                (item or {}).get("smallest_reference_character_height_fraction")),
        })
    #  The sheet's numerals are the union of its views', with the lead-line counts kept per view.
    numerals: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for view in views:
        for item in view["numerals"]:
            row = seen.get(item["value"])
            if row is None:
                row = dict(item)
                seen[item["value"]] = row
                numerals.append(row)
            else:
                row["lead_lines"] = max(row["lead_lines"], item["lead_lines"])
                row["designates_one_part"] = (row["designates_one_part"] and
                                              item["designates_one_part"])
                if item["matches_declared_part"] == "no":
                    row["matches_declared_part"] = "no"
                    row["points_at"] = item["points_at"]
    numerals += [item for item in _numerals(source.get("numerals"))
                 if item["value"] not in seen]
    labels = [{"text": " ".join(str((item or {}).get("text") or "").split())[:120],
               "duplicates_numeral": re.sub(
                   r"[^0-9a-zA-Z]", "", str((item or {}).get("duplicates_numeral") or "")).lower()}
              for item in source.get("text_labels") or [] if str((item or {}).get("text") or "")]
    try:
        height = float(source.get("smallest_reference_character_height_fraction") or 0.0)
    except (TypeError, ValueError):
        height = 0.0
    return {
        "views": [view for view in views if view["legend"]],
        "unlabelled_views": [{"description": view["printed_caption"] or view["kind"] or "a view",
                              "bbox": view["bbox"], "numerals": view["numerals"],
                              "character_height": view["character_height"]}
                             for view in views if not view["legend"]],
        "numerals": numerals,
        "text_labels": labels,
        "numeral_key_table": bool(source.get("numeral_key_table")),
        "divider_rules": max(0, int(source.get("divider_rules") or 0)),
        "sheet_number_text": str(source.get("sheet_number_text") or "")[:20],
        "smallest_reference_character_height_fraction": max(0.0, min(1.0, height)),
    }


def measure_character_height(png: bytes) -> dict[str, Any]:
    """How tall the reference characters actually are, in pixels, measured from the artwork.

    37 CFR 1.84(p)(3) sets a hard floor of 0.32 cm, and whether a sheet clears it is decided by
    the artwork's own resolution and the scale it is placed at. A model asked to estimate that as
    a fraction of the image gets it wrong by a factor of two in both directions, which is fatal
    for a check that blocks a filing, so it is measured here instead.

    METHOD. Label the connected components of dark pixels and keep the ones shaped like a printed
    digit: taller than a few pixels, no taller than 6% of the sheet, not much wider than tall, not
    a long thin stroke. What remains is digits, legend letters, hatching flecks and stray dots.
    The flecks are small and numerous and the digits are the tall end of that distribution, so the
    90th percentile is the digit height. On the one sheet a human measured by hand this returns
    19 pixels against their 19.
    """
    try:
        import numpy
        import scipy.ndimage as ndimage
        from PIL import Image
    except Exception:                                              # noqa: BLE001
        return {"pixels": 0.0, "fraction": 0.0, "samples": 0, "height": 0}
    with Image.open(io.BytesIO(bytes(png))) as image:
        grey = numpy.asarray(image.convert("L"))
    height, _width = grey.shape
    labels, _count = ndimage.label(grey < 128)
    heights = []
    for box in ndimage.find_objects(labels):
        tall = box[0].stop - box[0].start
        wide = box[1].stop - box[1].start
        if tall < 4 or wide < 2 or tall > height * 0.06:
            continue
        if wide > tall * 2.2 or tall > wide * 6:
            continue
        heights.append(tall)
    if len(heights) < 12:
        return {"pixels": 0.0, "fraction": 0.0, "samples": len(heights), "height": int(height)}
    pixels = float(numpy.percentile(numpy.array(heights), 90))
    return {"pixels": pixels, "fraction": pixels / max(1, height),
            "samples": len(heights), "height": int(height)}


def canonical_legend(value: str) -> str:
    match = _FIG_RE.search(str(value or ""))
    if not match:
        return ""
    return f"FIG. {int(match.group(1))}{match.group(2).upper()}"


def figure_numbers(text: Any) -> list[str]:
    """Every figure a passage names, as canonical legends, in the order they appear.

    "FIGS. 1 and 2" names two figures and "FIGS. 2-4" names three. Reading only the first number
    after the word FIGS is how a cross-reference check reports that a sentence citing FIGS. 1 and
    2 mentions a part FIG. 2 shows: a false positive on the one check whose whole value is that
    it does not have any.
    """
    body = str(text or "")
    out: list[str] = []

    def add(number: int, suffix: str = "") -> None:
        legend = f"FIG. {number}{suffix.upper()}"
        if legend not in out:
            out.append(legend)

    for match in _FIG_GROUP_RE.finditer(body):
        run = match.group(0)
        parts = list(_FIG_ITEM_RE.finditer(run))
        for index, item in enumerate(parts):
            add(int(item.group(1)), item.group(2))
            joiner = run[item.end():parts[index + 1].start()] if index + 1 < len(parts) else ""
            if index + 1 < len(parts) and _RANGE_JOIN_RE.fullmatch(joiner.strip()):
                start, end = int(item.group(1)), int(parts[index + 1].group(1))
                if 0 < end - start <= 30:
                    for value in range(start + 1, end):
                        add(value)
    return out


def numerals_in(text: Any, *, known: Iterable[str] = ()) -> list[str]:
    """Reference numerals used in a passage.

    Restricted to numerals the draft has actually declared where a table is available, because a
    specification is full of numbers that are not reference characters: "37 CFR 1.84", "at least
    two", "2 mm". Without the table, the shape of a reference numeral is the best we have.
    """
    allowed = {str(value).lower() for value in known}
    out = []
    for match in _NUMERAL_RE.finditer(str(text or "")):
        value = match.group(1).lower()
        if allowed and value not in allowed:
            continue
        if value not in out:
            out.append(value)
    return out


# =============================================================================================
# The comparisons - every one of these is deterministic and testable without a model
# =============================================================================================
def reconcile(*, sheets: Sequence[Mapping[str, Any]], sections: Mapping[str, str],
              numerals: Sequence[Mapping[str, str]] = (),
              claim_terms: Sequence[str] = ()) -> list[filing_rules.Finding]:
    """Does the specification describe the sheets that were actually supplied?"""
    out: list[filing_rules.Finding] = []
    table = {str(row.get("numeral") or "").lower(): str(row.get("part") or "")
             for row in numerals if str(row.get("numeral") or "").strip()}
    brief = str(sections.get("drawing_descriptions") or "")
    detail = str(sections.get("detailed_description") or "")
    summary = str(sections.get("summary") or "")
    spec_text = "\n".join(str(sections.get(key) or "") for key in
                          ("field", "background", "summary", "drawing_descriptions",
                           "detailed_description"))

    out += _sheet_form_findings(sheets)
    out += _legend_findings(sheets, brief)
    out += _numeral_findings(sheets, table, spec_text)
    out += _cross_reference_findings(sheets, detail, table)
    out += _hedged_view_findings(brief, summary, detail)
    out += _claimed_but_unshown(sheets, sections, table, claim_terms)
    return out


def _drawn_legends(sheets: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for sheet in sheets:
        for view in sheet.get("views") or []:
            legend = str(view.get("legend") or "")
            if legend and legend not in out:
                out.append(legend)
    return out


def _numerals_by_legend(sheets: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Which numerals appear in each numbered view."""
    out: dict[str, set[str]] = {}
    for sheet in sheets:
        sheet_values = {str(item.get("value") or "").lower()
                        for item in sheet.get("numerals") or []}
        for view in sheet.get("views") or []:
            legend = str(view.get("legend") or "")
            if not legend:
                continue
            values = {str(item.get("value") or "").lower()
                      for item in view.get("numerals") or []}
            out.setdefault(legend, set()).update(values or sheet_values)
    return out


def _sheet_form_findings(sheets: Sequence[Mapping[str, Any]]) -> list[filing_rules.Finding]:
    out: list[filing_rules.Finding] = []
    for index, sheet in enumerate(sheets, 1):
        where = str(sheet.get("label") or f"sheet {index}")
        views = sheet.get("views") or []
        unlabelled = sheet.get("unlabelled_views") or []
        if unlabelled:
            out.append(filing_rules.finding(
                "37 CFR 1.84(u)", "blocker", where,
                f"{len(unlabelled)} view(s) on this sheet carry no figure number",
                "; ".join(str(item.get("description") or "a view")[:80] for item in unlabelled)
                + ". Every view must be numbered independently of the sheet. Give it its own "
                  "number, such as FIG. 2A, and describe it in the Brief Description as a view "
                  "in its own right."))
        if len(views) > 1:
            out.append(filing_rules.finding(
                "37 CFR 1.84(u)", "formality", where,
                f"This sheet carries {len(views)} views: " + ", ".join(
                    str(view.get("legend")) for view in views),
                "Several numbered views on one sheet are permitted. They are easier to read, and "
                "easier to scale above the 0.32 cm character minimum, one to a sheet."))
        if sheet.get("divider_rules"):
            out.append(filing_rules.finding(
                "37 CFR 1.84(i)", "formality", where,
                "A rule is drawn between the views on this sheet",
                "Views are separated by white space, not by a printed line. A line on a sheet is "
                "read as part of the drawing."))
        if sheet.get("numeral_key_table"):
            out.append(filing_rules.finding(
                "37 CFR 1.84(o)", "formality", where,
                "A reference numeral key is printed on this sheet",
                "Drawings carry legends, not a parts list. The numeral table belongs in the "
                "specification, where it can be amended without a replacement sheet."))
        for item in sheet.get("text_labels") or []:
            if item.get("duplicates_numeral"):
                out.append(filing_rules.finding(
                    "37 CFR 1.84(o)", "formality", where,
                    f'The words "{item.get("text")}" duplicate reference numeral '
                    f'{item.get("duplicates_numeral")}',
                    "A descriptive legend is permitted and is subject to the Office's approval. A "
                    "legend that repeats what a numeral already says is two names for one part, "
                    "and it leaves a leader pointing at nothing once it is removed."))
        if sheet.get("sheet_number_text"):
            drawn_total = re.search(r"/\s*(\d+)", str(sheet["sheet_number_text"]))
            disagrees = bool(drawn_total) and int(drawn_total.group(1)) != len(sheets)
            out.append(filing_rules.finding(
                "37 CFR 1.84(t)", "formality" if disagrees else "note", where,
                f'The artwork already carries the sheet number "{sheet.get("sheet_number_text")}"'
                + (f", and this application has {len(sheets)} sheet(s)" if disagrees else ""),
                "The filing package numbers every sheet itself, so this sheet goes to the Office "
                "with two numbers on it" +
                (". They already disagree: a figure has been added or removed since the artwork "
                 "was drawn. Take the number off the artwork." if disagrees else
                 ". Take the number off the artwork, or they will disagree the first time a "
                 "figure is added or removed.")))
    return out


def _legend_findings(sheets: Sequence[Mapping[str, Any]],
                     brief: str) -> list[filing_rules.Finding]:
    drawn = _drawn_legends(sheets)
    described = figure_numbers(brief)
    out: list[filing_rules.Finding] = []
    missing = [legend for legend in described if legend not in drawn]
    extra = [legend for legend in drawn if legend not in described]
    if missing:
        out.append(filing_rules.finding(
            "37 CFR 1.83(a)", "blocker", "07-drawings.md",
            f"{len(missing)} described view(s) have no sheet",
            "The Brief Description names " + ", ".join(missing) + " and no uploaded sheet carries "
            "that legend. Either the sheet is missing or the description names a view that does "
            "not exist."))
    if extra:
        out.append(filing_rules.finding(
            "37 CFR 1.74", "blocker", "07-drawings.md",
            f"{len(extra)} drawn view(s) are not described",
            "The sheets carry " + ", ".join(extra) + " and the Brief Description does not mention "
            "them. Every view gets its own sentence saying what kind of view it is and what it "
            "shows."))
    for legend in described:
        if described.count(legend) > 1:
            out.append(filing_rules.finding(
                "37 CFR 1.74", "formality", "07-drawings.md",
                f"{legend} is described more than once",
                "One paragraph per view keeps the description amendable."))
            break
    return out


def _numeral_findings(sheets: Sequence[Mapping[str, Any]], table: Mapping[str, str],
                      spec_text: str) -> list[filing_rules.Finding]:
    out: list[filing_rules.Finding] = []
    used_in_text = set(numerals_in(spec_text, known=table.keys())) if table else set()
    drawn: dict[str, list[str]] = {}
    for index, sheet in enumerate(sheets, 1):
        where = str(sheet.get("label") or f"sheet {index}")
        for item in sheet.get("numerals") or []:
            value = str(item.get("value") or "").lower()
            drawn.setdefault(value, []).append(where)
            #  Several leaders from one numeral are ordinary and correct when they land on
            #  several instances of the same part. The defect is one numeral naming two
            #  DIFFERENT things, which is what 1.84(p)(2) forbids.
            if int(item.get("lead_lines") or 1) > 1 and not item.get("designates_one_part", True):
                out.append(filing_rules.finding(
                    "37 CFR 1.84(p)(2)", "blocker", where,
                    f"Reference numeral {value} designates two different parts on one sheet",
                    "The same reference character must never be used to designate different "
                    "parts. Its " + str(item.get("lead_lines")) + " lead lines land on: " +
                    (str(item.get("points_at") or "") or "targets that were not identified") +
                    ". Remove the leader that does not belong to the part the specification "
                    "defines, and repair the linework it crossed."))
            if table and value not in table:
                out.append(filing_rules.finding(
                    "37 CFR 1.84(p)(5)", "blocker", where,
                    f"Reference numeral {value} is on the sheet and is not in the specification",
                    "Reference characters not mentioned in the description must not appear in the "
                    "drawings. Either name the part in the specification and add it to the numeral "
                    "table, or take the numeral off the sheet."))
            #  The check no text-only reviewer can make: the drawing and the specification each
            #  say what a numeral means, and they disagree. A pair swapped on one sheet, 14 and
            #  16 against a specification that defines 14 as the first side, survived every
            #  consistency check ever run on the text because the text was self-consistent.
            if table and item.get("matches_declared_part") == "no" and value in table:
                out.append(filing_rules.finding(
                    "37 CFR 1.84(p)(2)", "blocker", where,
                    f"Reference numeral {value} points at something the specification does not "
                    "call it",
                    f"The specification defines {value} as \"{table[value]}\". On this sheet its "
                    f"lead line lands on: {item.get('points_at') or 'something else'}. One of the "
                    "two is wrong, and a drawing that contradicts the description is a defect in "
                    "both."))
    for value in sorted(used_in_text - set(drawn), key=_numeral_sort):
        out.append(filing_rules.finding(
            "37 CFR 1.84(p)(5)", "blocker", "figures",
            f"Reference numeral {value} is used in the specification and is on no sheet",
            f"The specification calls it \"{table.get(value, '')}\". A numeral mentioned in the "
            "description must appear in the drawings."))
    return out


def _numeral_sort(value: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", value)
    return (int(digits) if digits else 0, value)


def _cross_reference_findings(sheets: Sequence[Mapping[str, Any]], detail: str,
                              table: Mapping[str, str]) -> list[filing_rules.Finding]:
    """A sentence that says "as shown in FIG. N" must be about something FIG. N shows.

    This is the check that catches a stale cross-reference, and stale cross-references are what a
    drafting agent produces every single time the figures are renumbered or split. Four of them
    survived into a real filing draft: a closed loop cited to FIG. 3, a clearance discussion cited
    to FIG. 2 alone, and two embodiment paragraphs both cited to FIG. 3.
    """
    by_legend = _numerals_by_legend(sheets)
    if not by_legend or not table:
        return []
    out: list[filing_rules.Finding] = []
    for sentence in _SENTENCE_RE.split(str(detail or "")):
        cited = figure_numbers(sentence)
        if not cited:
            continue
        known = [legend for legend in cited if legend in by_legend]
        if not known:
            continue
        shown = set()
        for legend in known:
            shown |= by_legend.get(legend, set())
        used = set(numerals_in(sentence, known=table.keys()))
        absent = sorted(used - shown, key=_numeral_sort)
        if not absent:
            continue
        unnumbered = {str(item.get("value") or "").lower()
                      for sheet in sheets for view in sheet.get("unlabelled_views") or []
                      for item in view.get("numerals") or []}
        elsewhere = {value: sorted(legend for legend, values in by_legend.items()
                                   if value in values) for value in absent}
        detail_lines = "; ".join(
            f"{value} ({table.get(value, 'unnamed')}) is on " +
            (", ".join(elsewhere[value]) if elsewhere[value] else
             "a view that has no figure number" if value in unnumbered else "no sheet")
            for value in absent)
        out.append(filing_rules.finding(
            "37 CFR 1.84(p)(5)", "blocker", "08-detailed-description.md",
            f"A cross-reference to {', '.join(known)} names parts that view does not show",
            sentence.strip()[:220] + " ... " + detail_lines +
            ". Point the sentence at the view that shows the part, or add the part to that view."))
    return out


def _hedged_view_findings(brief: str, summary: str,
                          detail: str) -> list[filing_rules.Finding]:
    out: list[filing_rules.Finding] = []
    for label, text in (("07-drawings.md", brief), ("06-summary.md", summary),
                        ("08-detailed-description.md", detail)):
        for sentence in _SENTENCE_RE.split(str(text or "")):
            stripped = sentence.strip()
            if not stripped:
                continue
            for pattern in _HEDGED_VIEW_RES:
                if not pattern.search(stripped):
                    continue
                out.append(filing_rules.finding(
                    "37 CFR 1.84(u)", "blocker", label,
                    "The text describes a view it does not number",
                    stripped[:240] + " A drawing description says what a numbered view IS, in the "
                    "present tense. A view that exists gets a number of its own; a view that does "
                    "not exist is not mentioned. Nothing in a specification 'may' be shown."))
                break
        if label == "07-drawings.md":
            for sentence in _SENTENCE_RE.split(str(text or "")):
                if (_ALTERNATIVE_IN_ONE_VIEW_RE.search(sentence) and
                        len(figure_numbers(sentence)) == 1 and
                        re.search(r"\b(?:arrangements|embodiments|configurations|variants|"
                                  r"alternatives)\b", sentence, re.IGNORECASE)):
                    out.append(filing_rules.finding(
                        "37 CFR 1.84(u)", "formality", label,
                        "One view number is used for two alternative arrangements",
                        sentence.strip()[:240] + " Two arrangements shown side by side are two "
                        "views. Number them separately, for instance FIG. 3A and FIG. 3B."))
                    break
    return out


def _claimed_but_unshown(sheets: Sequence[Mapping[str, Any]], sections: Mapping[str, str],
                         table: Mapping[str, str],
                         claim_terms: Sequence[str]) -> list[filing_rules.Finding]:
    """37 CFR 1.83(a): every feature the claims specify must be shown in the drawing.

    Worked on the numeral table rather than on free text, because that table is the draft's own
    statement of what its parts are called. A claim term that matches a part name whose numeral is
    on no sheet is exactly the port that carried no numeral and appeared in no view while three
    independent claims recited fluid communication through it.
    """
    if not table or not claim_terms:
        return []
    drawn = {str(item.get("value") or "").lower()
             for sheet in sheets for item in sheet.get("numerals") or []}
    detail = str(sections.get("detailed_description") or "")
    out: list[filing_rules.Finding] = []
    for numeral, part in sorted(table.items(), key=lambda kv: _numeral_sort(kv[0])):
        if numeral in drawn:
            continue
        head = _head_words(part)
        if not head:
            continue
        if not any(head in _normal(term) for term in claim_terms):
            continue
        out.append(filing_rules.finding(
            "37 CFR 1.83(a)", "blocker", "figures",
            f"The claims recite \"{part}\" and numeral {numeral} appears on no sheet",
            "A feature specified in the claims must be shown in the drawing. Add the part to the "
            "view that ought to show it, with its numeral and a lead line, or stop reciting it."))
    return out


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower())


def _head_words(part: str) -> str:
    words = [word for word in _normal(part).split() if len(word) > 2]
    return " ".join(words[-2:]) if words else ""


def claim_terms_from(claims_text: str) -> list[str]:
    """The noun phrases a claim introduces, which is what 1.83(a) is about."""
    out: list[str] = []
    for match in re.finditer(
            r"\b(?:a|an|the|at least one|one or more)\s+([a-z][a-z\- ]{3,60}?)"
            r"(?=\s*(?:,|;|\.|configured|adapted|extending|having|comprising|that|which|"
            r"in\s+fluid|operable|arranged|positioned|and\b|or\b))",
            str(claims_text or ""), re.IGNORECASE):
        phrase = re.sub(r"\s+", " ", match.group(1).strip().lower())
        if phrase and phrase not in out:
            out.append(phrase)
    return out[:200]
