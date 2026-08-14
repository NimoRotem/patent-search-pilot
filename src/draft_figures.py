"""Patent figures for a draft application: generate them, then change them.

A US application needs drawings, and a drafted specification already contains the text that
describes them — "Brief Description of the Drawings" names each figure, and the detailed
description numbers every part. Turning that into an actual figure was the one step of the
drafting workflow with no support at all here: the draft said "FIG. 1 is a side elevation view of
the vacuum lifter" and then produced nothing.

What this does:

  * **reads the figures out of the draft** rather than asking the user to describe them again.
    The "Brief Description of the Drawings" section is the list of figures; the detailed
    description supplies the parts and their reference numerals;
  * **generates one figure at a time** with an instruction tuned for patent drawings — uniform
    black line art, no shading, no colour, reference numerals with lead lines, a figure label;
  * **edits by re-generating with the previous figure as input**, so "make the pump smaller and
    add the sealing lip at 12" changes THAT drawing instead of producing an unrelated one;
  * **keeps every version**, because the useful workflow is generate → look → adjust → compare,
    and a version that is thrown away the moment the next one arrives cannot be compared.

**What it is not.** These are drafting aids, not formal drawings. 37 CFR 1.84 governs paper size,
margins, line weight, shading, numbering and lettering, and nothing here checks any of that. The
UI and the export both say so. A model also miscounts and duplicates reference numerals — it did
on the first figure this was tested with — which is exactly why the numerals used are extracted
from the draft and listed beside the figure for checking.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
import os
import re
import random
import threading
import time

import db
import llm
from pydantic import BaseModel, Field

MAX_FIGURES = 40
MAX_VERSIONS_PER_FIGURE = 20
MAX_PROMPT_CHARS = 4000
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_PIXELS = 24_000_000
ALLOWED_SOURCE_FORMATS = ("PNG", "JPEG", "WEBP")
DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"
FIGURE_PROMPT_VERSION = "figure-v2-numeral-audit"


class _NumeralInspection(BaseModel):
    numerals: list[str] = Field(default_factory=list, max_length=120)


def image_model() -> str:
    """Deployment-selected image role. Model ids do not belong in feature code."""
    return os.environ.get("PATENT_FIGURE_IMAGE_MODEL", DEFAULT_IMAGE_MODEL).strip() or \
        DEFAULT_IMAGE_MODEL


def vision_model() -> str:
    return os.environ.get("PATENT_FIGURE_VISION_MODEL", llm.AGENT_MODEL).strip() or llm.AGENT_MODEL

#  The instruction that makes the difference between a product render and a patent figure. Stated
#  as prohibitions because that is what the model gets wrong by default: it reaches for shading,
#  perspective and colour, none of which belong in a utility patent drawing.
DRAWING_SYSTEM = (
    "You produce UTILITY PATENT DRAWINGS in the United States Patent and Trademark Office style. "
    "Output ONE figure as a black-and-white LINE DRAWING on a plain white background. "
    "Uniform-weight black outlines only. NO shading, NO hatching except conventional section "
    "hatching where a sectional view is requested, NO greyscale fills, NO colour, NO "
    "photorealism, NO drop shadows, NO background scenery, NO text other than the reference "
    "numerals and the figure label. "
    "Label each identified part with a straight lead line touching the part, ending at the "
    "REFERENCE NUMERAL ALONE. Write the numeral and nothing else — never the part's name, never "
    "an equals sign, never a description. The list you are given maps each numeral to the part it "
    "names so you know WHERE to put it; those words must not appear in the drawing. Use only the "
    "numerals given, use each exactly once, and do not invent numerals. "
    "Place the figure label centred beneath the drawing."
)

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS app_draft_figures (
         id bigserial PRIMARY KEY,
         project_id bigint NOT NULL,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         figure_label text NOT NULL DEFAULT 'FIG. 1',
         caption text NOT NULL DEFAULT '',
         sort_order integer NOT NULL DEFAULT 0,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS app_draft_figures_project_idx "
    "ON app_draft_figures (project_id, sort_order, id)",
    """CREATE TABLE IF NOT EXISTS app_draft_figure_versions (
         id bigserial PRIMARY KEY,
         figure_id bigint NOT NULL REFERENCES app_draft_figures(id) ON DELETE CASCADE,
         version_no integer NOT NULL,
         prompt text NOT NULL DEFAULT '',
         instruction text NOT NULL DEFAULT '',
         numerals text NOT NULL DEFAULT '',
         png bytea,
         mime text NOT NULL DEFAULT 'image/png',
         status text NOT NULL DEFAULT 'ready',
         error text NOT NULL DEFAULT '',
         created_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (figure_id, version_no))""",
    "CREATE INDEX IF NOT EXISTS app_draft_figure_versions_fig_idx "
    "ON app_draft_figure_versions (figure_id, version_no DESC)",
    "ALTER TABLE app_draft_figures ADD COLUMN IF NOT EXISTS active_version integer NOT NULL DEFAULT 0",
    "ALTER TABLE app_draft_figure_versions ADD COLUMN IF NOT EXISTS detected_numerals "
    "jsonb NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE app_draft_figure_versions ADD COLUMN IF NOT EXISTS numeral_audit "
    "jsonb NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE app_draft_figure_versions ADD COLUMN IF NOT EXISTS source_kind "
    "text NOT NULL DEFAULT 'generated'",
    """CREATE TABLE IF NOT EXISTS app_draft_figure_cache (
         cache_key char(64) PRIMARY KEY,
         model_name text NOT NULL,
         prompt_version text NOT NULL,
         png bytea NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now())""",
)

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def ensure_schema(force: bool = False) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return
        with db.cursor(autocommit=True) as cur:
            for statement in _SCHEMA:
                cur.execute(statement)
        _SCHEMA_READY = True


def reset_schema_cache_for_tests() -> None:
    global _SCHEMA_READY
    _SCHEMA_READY = False


# ---------------------------------------------------------------------------
# reading the figure list out of the draft itself
# ---------------------------------------------------------------------------
_FIG_LINE = re.compile(
    r"(?im)^\W*(FIG(?:URE)?S?\.?\s*\d+[A-Za-z]?(?:\s*(?:and|,|-|–|to)\s*\d+[A-Za-z]?)*)\s*"
    r"(?:is|are|shows?|illustrates?|depicts?|:|—|-)?\s*(.{0,400})$")
_NUMERAL = re.compile(r"\b([A-Za-z]?\d{1,4}[A-Za-z]?)\b")
#  Words that can precede a part name but are not part of it. Trimmed from the FRONT only, so
#  "flexible sealing lip" survives intact while "and a rechargeable battery" becomes the battery.
_STOPWORDS = frozenset((
    "a", "an", "the", "and", "or", "of", "to", "with", "for", "is", "are", "was", "were", "by",
    "at", "in", "on", "from", "into", "through", "that", "which", "said", "such", "one", "each",
    "further", "comprising", "including", "having", "carries", "drives", "monitors", "powers",
    "draws", "shows", "illustrates", "depicts", "provides", "defines", "receives", "between",
    "wherein", "whereby", "also", "may", "can", "be", "as", "its", "their", "this", "these"))


def figures_from_draft(sections):
    """The draft's own figure list -> ``[{label, caption}]``.

    Read from "Brief Description of the Drawings", which is where a US specification is required
    to list them. Returns [] when the section is absent rather than guessing, so a draft with no
    drawings section does not silently acquire invented figures.
    """
    text = str((sections or {}).get("drawing_descriptions") or "")
    out = []
    seen = set()
    for m in _FIG_LINE.finditer(text):
        label = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".")
        label = re.sub(r"(?i)^figures?", "FIG.", label)
        label = re.sub(r"(?i)^fig\.?\s*", "FIG. ", label).strip()
        caption = re.sub(r"\s+", " ", m.group(2) or "").strip(" .;:")
        key = label.lower()
        if key in seen or not caption:
            continue
        seen.add(key)
        out.append({"label": label, "caption": caption[:400]})
        if len(out) >= MAX_FIGURES:
            break
    return out


def numerals_for(sections, caption="", disclosure=""):
    """Reference numerals the draft actually uses, with the words they attach to.

    Taken from the detailed description, where "a suction cup 10" establishes the numeral, AND
    from the inventor's own disclosure — which is the only source that exists before the
    specification has been generated, and is where the numbering usually originates.

    Passing these to the image model instead of letting it choose is the whole point. Measured
    without them on a real draft: the model invented numerals 18 and 20, used 16 twice, and
    labelled one part with the word "sensor" instead of a numeral. A figure whose numerals do not
    match the specification is not a drafting aid, it is a defect.
    """
    text = " ".join(str((sections or {}).get(k) or "")
                    for k in ("detailed_description", "summary", "drawing_descriptions"))
    text = (str(disclosure or "") + "\n" + text)
    pairs = {}
    #  Take the words IMMEDIATELY before the numeral, not a greedy run: "grip vacuum and drives a
    #  warning indicator 32" names the warning indicator, not the whole clause. Four words is
    #  enough for "flexible sealing lip" and short enough to exclude the verb before it.
    for m in re.finditer(r"((?:[A-Za-z][A-Za-z\-]*\s+){1,4})(\d{1,4}[A-Za-z]?)\b", text):
        words = [w for w in re.sub(r"\s+", " ", m.group(1)).strip().split(" ") if w]
        num = m.group(2)
        while words and words[0].lower() in _STOPWORDS:
            words.pop(0)
        term = " ".join(words).strip(" ,;:.").lower()
        if len(term) < 3 or term.split(" ")[0] in ("claim", "figure", "fig", "step"):
            continue
        pairs.setdefault(num, term)
    ordered = sorted(pairs.items(), key=lambda kv: (len(kv[0]), kv[0]))
    return [f"{num} = {term}" for num, term in ordered][:40]


def build_prompt(label, caption, numerals, instruction="", spec_context=""):
    """Assemble the text handed to the image model for one figure."""
    parts = [f"{label} — {caption}".strip(" —")]
    if spec_context:
        parts.append("Context from the specification: " + spec_context[:1200])
    if numerals:
        #  Phrased as prose, not "10 = suction cup": given the equals form the model copied the
        #  whole string onto the drawing, so the figure read "10 = suction cup" instead of "10".
        lines = []
        for entry in numerals[:30]:
            num, _, term = str(entry).partition(" = ")
            lines.append(f"place numeral {num.strip()} on the {term.strip()}" if term
                         else str(entry))
        parts.append("Where each reference numeral goes (write ONLY the numeral on the drawing):"
                     "\n- " + "\n- ".join(lines))
    else:
        #  With no numerals established anywhere in the draft, an invented set would be worse
        #  than none: it would have to be renumbered by hand against the specification later.
        parts.append("The specification establishes no reference numerals yet. Draw the structure "
                     "WITHOUT any reference numerals or lead lines.")
    if instruction:
        parts.append("CHANGE REQUESTED — apply this to the drawing supplied, keeping everything "
                     "else the same: " + instruction[:1000])
    parts.append(f"Place the label \"{label}\" centred beneath the drawing.")
    return "\n\n".join(parts)[:MAX_PROMPT_CHARS]


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------
class FigureError(RuntimeError):
    pass


def _model_call(prompt, previous_png=None):
    """One transport attempt. Logical refusals are handled after transport succeeds."""
    try:
        from google.genai.types import GenerateContentConfig, Part
    except ModuleNotFoundError:
        # Unit-test/minimal environments can supply a fake client without installing the provider
        # SDK. Production has the SDK and always uses its typed image Part objects.
        if previous_png:
            raise
        return llm._client().models.generate_content(
            model=image_model(), contents=[DRAWING_SYSTEM + "\n\n" + prompt],
            config={"response_modalities": ["TEXT", "IMAGE"], "temperature": 0.35})
    contents = []
    if previous_png:
        contents.append(Part.from_bytes(data=previous_png, mime_type="image/png"))
    contents.append(DRAWING_SYSTEM + "\n\n" + prompt)
    return llm._client().models.generate_content(
        model=image_model(), contents=contents,
        config=GenerateContentConfig(response_modalities=["TEXT", "IMAGE"], temperature=0.35))


def generate_png(prompt, previous_png=None):
    """Prompt (+ the previous figure, when editing) -> PNG bytes.

    Passing the previous image back in is what makes an edit an EDIT: without it, "make the pump
    smaller" produces a new and unrelated drawing, and the user loses the parts of the figure they
    were happy with.
    """
    started = time.time()
    last_error = None
    for attempt in range(3):
        try:
            resp = _model_call(prompt, previous_png)
            break
        except Exception as exc:                         # transport only, maximum three attempts
            last_error = exc
            if attempt < 2:
                time.sleep((0.35 * (2 ** attempt)) + random.uniform(0, 0.2))
    else:
        print(json.dumps({"event": "draft_figure_llm", "provider": "vertex",
                          "model": image_model(), "prompt_version": FIGURE_PROMPT_VERSION,
                          "latency_ms": int((time.time() - started) * 1000),
                          "cache_hit": False, "success": False}), flush=True)
        raise FigureError(f"the image model could not draw this figure: {str(last_error)[:200]}") \
            from last_error
    um = getattr(resp, "usage_metadata", None)
    llm._record_usage(getattr(um, "prompt_token_count", 0) if um else 0,
                      getattr(um, "candidates_token_count", 0) if um else 0)
    try:
        parts = resp.candidates[0].content.parts
    except Exception:
        raise FigureError("the image model returned nothing")
    for p in parts:
        blob = getattr(p, "inline_data", None)
        if blob and blob.data:
            if len(blob.data) > MAX_PNG_BYTES:
                raise FigureError("the generated figure is unexpectedly large")
            print(json.dumps({"event": "draft_figure_llm", "provider": "vertex",
                              "model": image_model(), "prompt_version": FIGURE_PROMPT_VERSION,
                              "latency_ms": int((time.time() - started) * 1000),
                              "cache_hit": False, "success": True}), flush=True)
            return bytes(blob.data)
    #  A refusal comes back as text rather than an image; surface it instead of "no image".
    said = " ".join(str(getattr(p, "text", "") or "") for p in parts).strip()
    raise FigureError(said[:300] or "the image model returned no image")


def normalize_source_image(data: bytes, content_type: str = "") -> bytes:
    """Validate an uploaded/product image and return a bounded, white-backed PNG."""
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise FigureError(
            f"Choose an image smaller than {MAX_SOURCE_BYTES // (1024 * 1024)} MB.")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
        # Restrict plugin dispatch itself, not merely the filename or browser MIME type. This
        # prevents an uploaded PSD, FITS, PDF, font, or specialist raster from reaching a decoder
        # we neither need nor intend to expose.
        source = Image.open(io.BytesIO(data), formats=list(ALLOWED_SOURCE_FORMATS))
        width, height = source.size
        if width < 20 or height < 20 or width * height > MAX_SOURCE_PIXELS:
            raise FigureError("That image has unsupported dimensions.")
        source.verify()
        source = Image.open(io.BytesIO(data), formats=list(ALLOWED_SOURCE_FORMATS))
        source = ImageOps.exif_transpose(source)
        source.thumbnail((2400, 2400))
        if source.mode in ("RGBA", "LA"):
            rgba = source.convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            source = white.convert("RGB")
        else:
            source = source.convert("RGB")
        out = io.BytesIO()
        source.save(out, format="PNG", optimize=True)
        normalized = out.getvalue()
        if len(normalized) > MAX_PNG_BYTES:
            raise FigureError("That image remains too large after normalization.")
        return normalized
    except FigureError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise FigureError("That file is not a readable PNG, JPEG, or WebP image.") from exc


def edit_region_png(source_png: bytes, instruction: str,
                    region: tuple[int, int, int, int], allowed_numerals=()) -> bytes:
    """AI-edit a crop and composite it back, guaranteeing pixels outside it never change."""
    from PIL import Image
    instruction = str(instruction or "").strip()
    if not instruction:
        raise FigureError("Describe what should change in the selected area.")
    source_png = normalize_source_image(source_png, "image/png")
    image = Image.open(io.BytesIO(source_png)).convert("RGB")
    try:
        x1, y1, x2, y2 = (int(value) for value in region)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FigureError("Select a rectangular area to edit.") from exc
    x1, x2 = sorted((max(0, min(image.width, x1)), max(0, min(image.width, x2))))
    y1, y2 = sorted((max(0, min(image.height, y1)), max(0, min(image.height, y2))))
    if x2 - x1 < 5 or y2 - y1 < 5:
        raise FigureError("Select a larger area to edit.")
    crop = image.crop((x1, y1, x2, y2))
    crop_bytes = io.BytesIO()
    crop.save(crop_bytes, format="PNG")
    prompt = (
        "Edit only this selected crop from an existing patent drawing. Preserve the crop's white "
        "background, line weight, geometry, and reference-numeral style except for this requested "
        f"change: {instruction[:1000]} The only reference numerals permitted "
        "anywhere in the edited crop are: " +
        (", ".join(filter(None, (_clean_numeral(value) for value in allowed_numerals))) or
         "none. Do not invent a numeral."))
    changed = generate_png(prompt, previous_png=crop_bytes.getvalue())
    changed_image = Image.open(io.BytesIO(changed)).convert("RGB").resize(crop.size)
    image.paste(changed_image, (x1, y1))
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _clean_numeral(value) -> str:
    match = re.search(r"\b([A-Za-z]?\d{1,4}[A-Za-z]?)\b", str(value or ""))
    return match.group(1).upper() if match else ""


def numeral_audit(expected, detected) -> dict:
    expected_values = [_clean_numeral(value) for value in (expected or [])]
    detected_values = [_clean_numeral(value) for value in (detected or [])]
    expected_values = [value for value in expected_values if value]
    detected_values = [value for value in detected_values if value]
    expected_set, detected_set = set(expected_values), set(detected_values)
    counts = Counter(detected_values)
    missing = sorted(expected_set - detected_set, key=lambda n: (int(re.sub(r"\D", "", n) or 0), n))
    unexpected = sorted(detected_set - expected_set,
                        key=lambda n: (int(re.sub(r"\D", "", n) or 0), n))
    duplicates = sorted((n for n, count in counts.items() if count > 1),
                        key=lambda n: (int(re.sub(r"\D", "", n) or 0), n))
    return {"ok": not missing and not unexpected and not duplicates,
            "expected": sorted(expected_set), "detected": detected_values,
            "missing": missing, "unexpected": unexpected, "duplicates": duplicates}


def inspect_numerals(png: bytes) -> dict:
    """Vision-read actual labels from the returned pixels; never infer from the prompt."""
    from google.genai.types import GenerateContentConfig, Part, ThinkingConfig
    instruction = (
        "Inspect this patent drawing pixel by pixel. Return JSON with one key, numerals, whose "
        "value is every visible REFERENCE NUMERAL in reading order, including duplicates. Exclude "
        "the figure number in labels such as FIG. 1, dimensions, page numbers, and other text. "
        "Do not infer a number that is not visibly printed. Example: {\"numerals\":[\"10\",\"12\"]}.")
    last_error = None
    for attempt in range(3):
        try:
            response = llm._client().models.generate_content(
                model=vision_model(),
                contents=[Part.from_bytes(data=png, mime_type="image/png"), instruction],
                config=GenerateContentConfig(
                    response_mime_type="application/json", response_schema=_NumeralInspection,
                    temperature=0,
                    max_output_tokens=300, thinking_config=ThinkingConfig(thinking_budget=0)))
            usage = getattr(response, "usage_metadata", None)
            llm._record_usage(getattr(usage, "prompt_token_count", 0) if usage else 0,
                              getattr(usage, "candidates_token_count", 0) if usage else 0)
            parsed = getattr(response, "parsed", None)
            payload = parsed if isinstance(parsed, _NumeralInspection) else \
                _NumeralInspection.model_validate_json(
                    str(getattr(response, "text", "") or "{}"))
            values = [_clean_numeral(value) for value in payload.numerals]
            return {"ok": True, "numerals": [value for value in values if value]}
        except Exception as exc:                         # an unread audit must be visible, not guessed
            last_error = exc
            if attempt < 2:
                time.sleep((0.25 * (2 ** attempt)) + random.uniform(0, 0.15))
    return {"ok": False, "numerals": [],
            "error": f"Could not inspect drawing numerals: {str(last_error)[:160]}"}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def create_figure(project_id, user_id, label, caption="", sort_order=0):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("INSERT INTO app_draft_figures (project_id,user_id,figure_label,caption,"
                    "sort_order) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                    (int(project_id), int(user_id), str(label)[:80], str(caption)[:400],
                     int(sort_order)))
        return dict(cur.fetchone())


def add_version(figure_id, *, prompt, instruction, numerals, png, mime="image/png",
                detected_numerals=(), audit=None, source_kind="generated"):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT coalesce(max(version_no),0)+1 AS n FROM app_draft_figure_versions "
                    "WHERE figure_id=%s", (int(figure_id),))
        n = int(cur.fetchone()["n"])
        cur.execute("INSERT INTO app_draft_figure_versions "
                    "(figure_id,version_no,prompt,instruction,numerals,png,mime,"
                    "detected_numerals,numeral_audit,source_kind) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) "
                    "RETURNING id,version_no,created_at",
                    (int(figure_id), n, str(prompt)[:MAX_PROMPT_CHARS], str(instruction)[:1000],
                     "\n".join(numerals or [])[:4000], png, mime,
                     json.dumps(list(detected_numerals or [])), json.dumps(dict(audit or {})),
                     str(source_kind or "generated")[:40]))
        row = dict(cur.fetchone())
        cur.execute("UPDATE app_draft_figures SET active_version=%s, updated_at=now() WHERE id=%s",
                    (n, int(figure_id)))
        #  Keep the history bounded: a figure iterated twenty times is a workflow, two hundred is
        #  a stuck loop, and each version is a megabyte of PNG.
        cur.execute("DELETE FROM app_draft_figure_versions WHERE figure_id=%s AND version_no <= %s",
                    (int(figure_id), n - MAX_VERSIONS_PER_FIGURE))
    return row


def set_active(figure_id, user_id, version_no):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM app_draft_figure_versions v JOIN app_draft_figures f "
                    "ON f.id=v.figure_id WHERE v.figure_id=%s AND v.version_no=%s AND f.user_id=%s",
                    (int(figure_id), int(version_no), int(user_id)))
        if not cur.fetchone():
            return False
        cur.execute("UPDATE app_draft_figures SET active_version=%s, updated_at=now() "
                    "WHERE id=%s AND user_id=%s",
                    (int(version_no), int(figure_id), int(user_id)))
        return True


def delete_figure(figure_id, user_id) -> bool:
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("DELETE FROM app_draft_figures WHERE id=%s AND user_id=%s",
                    (int(figure_id), int(user_id)))
        return cur.rowcount > 0


def listing(project_id, user_id):
    """Every figure of a project with its version list — no image bytes."""
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_draft_figures WHERE project_id=%s AND user_id=%s "
                    "ORDER BY sort_order, id", (int(project_id), int(user_id)))
        figs = [dict(r) for r in cur.fetchall()]
        if not figs:
            return []
        cur.execute("SELECT figure_id,version_no,instruction,numerals,status,error,created_at,"
                    "detected_numerals,numeral_audit,source_kind "
                    "FROM app_draft_figure_versions WHERE figure_id = ANY(%s) "
                    "ORDER BY figure_id, version_no DESC", ([f["id"] for f in figs],))
        versions = {}
        for r in cur.fetchall():
            version = dict(r)
            for key, fallback in (("detected_numerals", []), ("numeral_audit", {})):
                value = version.get(key)
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        value = fallback
                version[key] = value if value is not None else fallback
            versions.setdefault(r["figure_id"], []).append(version)
    for f in figs:
        f["versions"] = versions.get(f["id"], [])
        f["n_versions"] = len(f["versions"])
    return figs


def png_bytes(figure_id, user_id, version_no=None):
    """(mime, bytes) for one version — the active one unless a version is named."""
    ensure_schema()
    with db.cursor() as cur:
        if version_no is None:
            cur.execute("SELECT v.mime, v.png FROM app_draft_figure_versions v "
                        "JOIN app_draft_figures f ON f.id=v.figure_id "
                        "WHERE f.id=%s AND f.user_id=%s AND v.version_no=f.active_version",
                        (int(figure_id), int(user_id)))
        else:
            cur.execute("SELECT v.mime, v.png FROM app_draft_figure_versions v "
                        "JOIN app_draft_figures f ON f.id=v.figure_id "
                        "WHERE f.id=%s AND f.user_id=%s AND v.version_no=%s",
                        (int(figure_id), int(user_id), int(version_no)))
        r = cur.fetchone()
    if not r or not r.get("png"):
        return None, None
    return r["mime"], bytes(r["png"])


def get_figure(figure_id, user_id):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_draft_figures WHERE id=%s AND user_id=%s",
                    (int(figure_id), int(user_id)))
        row = cur.fetchone()
    return dict(row) if row else None


def _cache_key(prompt: str, previous: bytes | None) -> str:
    digest = hashlib.sha256()
    digest.update(FIGURE_PROMPT_VERSION.encode())
    digest.update(image_model().encode())
    digest.update(str(prompt).encode("utf-8"))
    digest.update(previous or b"")
    return digest.hexdigest()


def _cached_generate(prompt: str, previous: bytes | None = None) -> bytes:
    """Content-addressed reuse before a paid image call; cache failure never blocks drawing."""
    key = _cache_key(prompt, previous)
    try:
        ensure_schema()
        with db.cursor() as cur:
            cur.execute("SELECT png FROM app_draft_figure_cache WHERE cache_key=%s", (key,))
            row = cur.fetchone()
        if row and row.get("png"):
            print(json.dumps({"event": "draft_figure_llm", "provider": "vertex",
                              "model": image_model(), "prompt_version": FIGURE_PROMPT_VERSION,
                              "latency_ms": 0, "cache_hit": True, "success": True}), flush=True)
            return bytes(row["png"])
    except Exception:
        pass
    png = generate_png(prompt, previous_png=previous)
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO app_draft_figure_cache (cache_key,model_name,prompt_version,png) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (cache_key) DO NOTHING",
                (key, image_model(), FIGURE_PROMPT_VERSION, png))
    except Exception:
        pass
    return png


def _audited_version(figure_id: int, *, prompt: str, instruction: str, numerals,
                     png: bytes, source_kind: str, inspection=None) -> dict:
    inspection = inspection or inspect_numerals(png)
    audit = numeral_audit(numerals, inspection.get("numerals") or [])
    audit["inspected"] = bool(inspection.get("ok"))
    if inspection.get("error"):
        audit["error"] = inspection["error"]
    version = add_version(
        figure_id, prompt=prompt, instruction=instruction, numerals=numerals, png=png,
        detected_numerals=inspection.get("numerals") or [], audit=audit,
        source_kind=source_kind)
    return {**version, "audit": audit, "detected_numerals": inspection.get("numerals") or []}


def save_manual_version(project_id: int, user_id: int, figure_id: int, png: bytes, *,
                        instruction: str = "Manual drawing edit", numerals=()) -> dict:
    figure = get_figure(figure_id, user_id)
    if not figure or int(figure.get("project_id") or 0) != int(project_id):
        raise FigureError("no such figure")
    normalized = normalize_source_image(png, "image/png")
    version = _audited_version(
        figure_id, prompt="Manual canvas edit", instruction=instruction,
        numerals=numerals, png=normalized, source_kind="manual")
    return {"figure_id": figure_id, "label": figure["figure_label"],
            "caption": figure["caption"], "version_no": version["version_no"],
            "numerals": list(numerals or []), "numeral_audit": version["audit"],
            "detected_numerals": version["detected_numerals"]}


def render_figure(project_id, user_id, *, label, caption, sections=None, instruction="",
                  figure_id=None, base_version=None, disclosure="", source_png=None,
                  region=None, numerals=None):
    """Generate (or re-generate) one figure and store the result as a new version.

    With `figure_id` this is an EDIT: the currently active image is passed back to the model with
    the instruction, so the change applies to that drawing rather than producing a new one.
    """
    sections = sections or {}
    numerals = (numerals_for(sections, caption, disclosure) if numerals is None
                else list(numerals))
    previous = source_png
    if figure_id:
        fig = get_figure(figure_id, user_id)
        if not fig or int(fig.get("project_id") or 0) != int(project_id):
            raise FigureError("no such figure")
        label = label or fig["figure_label"]
        caption = caption or fig["caption"]
        _, previous = png_bytes(figure_id, user_id, base_version)
    context = str(sections.get("summary") or disclosure or "")[:1200]
    prompt = build_prompt(label, caption, numerals, instruction, context)
    inspection = None
    if region:
        if not previous:
            raise FigureError("Draw the figure before editing one area of it.")
        png = edit_region_png(previous, instruction, region, numerals)
        source_kind = "region_edit"
    else:
        png = _cached_generate(prompt, previous)
        source_kind = "photo_to_sketch" if source_png else "generated"
        # One bounded self-correction pass. The inspection reads the returned pixels rather than
        # trusting the prompt, and a second mismatch remains visible instead of looping/spending.
        first_inspection = inspect_numerals(png)
        first_audit = numeral_audit(numerals, first_inspection.get("numerals") or [])
        if first_inspection.get("ok") and not first_audit["ok"]:
            qa_instruction = (
                "NUMERAL QA FAILED. Redraw the supplied image while preserving its "
                "geometry. The only reference numerals permitted are " +
                (", ".join(first_audit["expected"]) or "none") + ". Add missing numerals, remove "
                "unexpected numerals, and show each permitted numeral exactly once.")
            retained = max(0, MAX_PROMPT_CHARS - len(qa_instruction) - 2)
            correction = prompt[:retained] + "\n\n" + qa_instruction
            png = _cached_generate(correction, png)
        else:
            inspection = first_inspection
    if not figure_id:
        fig = create_figure(project_id, user_id, label, caption)
        figure_id = fig["id"]
    version = _audited_version(
        figure_id, prompt=prompt, instruction=instruction, numerals=numerals, png=png,
        source_kind=source_kind, inspection=inspection)
    return {"figure_id": figure_id, "label": label, "caption": caption,
            "version_no": version["version_no"], "numerals": numerals,
            "detected_numerals": version["detected_numerals"],
            "numeral_audit": version["audit"]}
