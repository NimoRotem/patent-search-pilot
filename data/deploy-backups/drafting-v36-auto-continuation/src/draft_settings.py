"""Advanced settings for one drafting project.

EVERY KNOB HERE DOES SOMETHING. A settings page whose switches are decorative is worse than no
settings page: it invites someone to change a value, watch nothing happen, and conclude the whole
product is unreliable. So this file is deliberately short, and it grows only when the thing behind
a new field is actually wired. Where a control belongs to a subsystem that cannot honour it yet, it
is absent rather than present and inert.

Stored as one jsonb column on the project so a new field needs no migration, read through
``resolve`` so a project saved before a field existed still gets today's default.
"""
from __future__ import annotations

from typing import Any, Mapping

import draft_agent

#  The reviewer is a different job from the drafter and is allowed a different tier: it reads and
#  judges rather than writes, and a cheaper model there changes cost far more than quality.
FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "draft_model", "kind": "model", "default": "",
     "label": "Drafting model",
     "help": "The tier that writes the application. Takes effect on the next turn."},
    {"key": "review_model", "kind": "model", "default": "",
     "label": "Review model",
     "help": "The independent reviewer and the source-fidelity check. It reads and judges rather "
             "than writes, so a cheaper tier here changes cost far more than quality."},
    {"key": "finalization_rounds", "kind": "int", "default": 6, "min": 2, "max": 6,
     "label": "Repair rounds per turn",
     "help": "How many times a turn may fix what the review found before it gives up. Fewer means "
             "a turn fails sooner and costs less; more means it tries harder on a hard draft."},
    {"key": "research_depth", "kind": "choice", "default": "deep",
     "choices": [{"id": "deep", "label": "Deep (reads the art, charts the claims)"},
                 {"id": "quick", "label": "Quick (ranks the art, no reading)"}],
     "label": "Re-search depth",
     "help": "Deep reads each reference against your claims and is what produces the novelty "
             "measurement. Quick is minutes rather than tens of minutes and cannot measure."},
    {"key": "research_references", "kind": "int", "default": 5, "min": 1, "max": 10,
     "label": "References attached per round",
     "help": "How many of the nearest references each re-search round adds to the project. Every "
             "one of them must then be addressed in the Background."},
    {"key": "style_notes", "kind": "text", "default": "", "max_chars": 4000,
     "label": "Drafting style",
     "help": "Added to the drafting agent's instructions on every turn. House conventions, "
             "claim style, terminology to prefer or avoid. It never overrides the disclosure."},
    {"key": "prior_art_notes", "kind": "text", "default": "", "max_chars": 4000,
     "label": "How to treat prior art",
     "help": "Added to the drafting agent's instructions on every turn. How aggressively to "
             "distinguish, which families matter, what to say about a reference you must cite."},
)
BY_KEY = {item["key"]: item for item in FIELDS}


def defaults() -> dict[str, Any]:
    return {item["key"]: item["default"] for item in FIELDS}


def resolve(stored: Mapping[str, Any] | None) -> dict[str, Any]:
    """Today's defaults, overridden by whatever this project has actually saved."""
    out = defaults()
    for key, value in dict(stored or {}).items():
        if key in BY_KEY:
            out[key] = value
    return out


def clean(supplied: Mapping[str, Any] | None,
          stored: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate what a request asked for. An unusable value is REFUSED, never silently corrected.

    Silently substituting a value is how a settings page starts lying: the field shows what you
    typed, the system uses something else, and the difference surfaces as inexplicable behaviour
    three turns later.
    """
    out = resolve(stored)
    for key, value in dict(supplied or {}).items():
        field = BY_KEY.get(key)
        if not field:
            raise ValueError(f"{key!r} is not a setting of this project.")
        kind = field["kind"]
        if kind == "model":
            name = str(value or "").strip().lower()
            if name and not draft_agent.normalize_model(name):
                raise ValueError(f"{field['label']}: {value!r} is not a model this server runs.")
            out[key] = draft_agent.normalize_model(name)
        elif kind == "int":
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{field['label']} must be a whole number.") from None
            if not field["min"] <= number <= field["max"]:
                raise ValueError(
                    f"{field['label']} must be between {field['min']} and {field['max']}.")
            out[key] = number
        elif kind == "choice":
            allowed = {choice["id"] for choice in field["choices"]}
            if str(value) not in allowed:
                raise ValueError(f"{field['label']}: {value!r} is not one of the choices.")
            out[key] = str(value)
        else:
            text = str(value or "").replace("\x00", "").strip()
            if len(text) > field["max_chars"]:
                raise ValueError(
                    f"{field['label']} is longer than {field['max_chars']} characters.")
            out[key] = text
    return out


def prompt_additions(settings: Mapping[str, Any] | None) -> str:
    """The operator's own standing instructions, appended to the drafting system prompt.

    Placed AFTER the built-in rules and labelled as the operator's, so a house convention cannot
    read as permission to override the one rule that outranks everything: the disclosure decides
    what the invention is.
    """
    resolved = resolve(settings)
    blocks = []
    if resolved.get("style_notes"):
        blocks.append("DRAFTING STYLE SET BY THE OPERATOR OF THIS PROJECT\n"
                      + str(resolved["style_notes"]).strip())
    if resolved.get("prior_art_notes"):
        blocks.append("HOW THIS PROJECT WANTS PRIOR ART TREATED\n"
                      + str(resolved["prior_art_notes"]).strip())
    if not blocks:
        return ""
    return ("\n\n" + "\n\n".join(blocks) +
            "\n\nThese are house conventions. They never override the inventor's disclosure, the "
            "filing-clean rules, or the instruction not to state a legal conclusion.")


def public(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """The settings and their descriptions, for a page that has to explain itself."""
    resolved = resolve(settings)
    fields = []
    for item in FIELDS:
        entry = {key: item[key] for key in ("key", "kind", "label", "help")}
        entry["value"] = resolved[item["key"]]
        entry["default"] = item["default"]
        for extra in ("min", "max", "max_chars"):
            if extra in item:
                entry[extra] = item[extra]
        if item["kind"] == "model":
            entry["choices"] = ([{"id": "", "label": "Server default"}] +
                                [{"id": m["id"], "label": m["label"], "help": m["detail"]}
                                 for m in draft_agent.MODEL_CHOICES])
        if item["kind"] == "choice":
            entry["choices"] = item["choices"]
        fields.append(entry)
    return {"values": resolved, "fields": fields}
