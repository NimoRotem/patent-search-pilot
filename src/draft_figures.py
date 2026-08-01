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

import re
import threading

import db
import llm

MAX_FIGURES = 40
MAX_VERSIONS_PER_FIGURE = 20
MAX_PROMPT_CHARS = 4000
MAX_PNG_BYTES = 8 * 1024 * 1024
IMAGE_MODEL = "gemini-2.5-flash-image"

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


def generate_png(prompt, previous_png=None):
    """Prompt (+ the previous figure, when editing) -> PNG bytes.

    Passing the previous image back in is what makes an edit an EDIT: without it, "make the pump
    smaller" produces a new and unrelated drawing, and the user loses the parts of the figure they
    were happy with.
    """
    from google.genai.types import GenerateContentConfig, Part
    contents = []
    if previous_png:
        contents.append(Part.from_bytes(data=previous_png, mime_type="image/png"))
    contents.append(DRAWING_SYSTEM + "\n\n" + prompt)
    try:
        resp = llm._client().models.generate_content(
            model=IMAGE_MODEL, contents=contents,
            config=GenerateContentConfig(response_modalities=["TEXT", "IMAGE"], temperature=0.35))
    except Exception as exc:
        raise FigureError(f"the image model refused this figure: {str(exc)[:200]}") from exc
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
            return bytes(blob.data)
    #  A refusal comes back as text rather than an image; surface it instead of "no image".
    said = " ".join(str(getattr(p, "text", "") or "") for p in parts).strip()
    raise FigureError(said[:300] or "the image model returned no image")


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


def add_version(figure_id, *, prompt, instruction, numerals, png, mime="image/png"):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT coalesce(max(version_no),0)+1 AS n FROM app_draft_figure_versions "
                    "WHERE figure_id=%s", (int(figure_id),))
        n = int(cur.fetchone()["n"])
        cur.execute("INSERT INTO app_draft_figure_versions "
                    "(figure_id,version_no,prompt,instruction,numerals,png,mime) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id,version_no,created_at",
                    (int(figure_id), n, str(prompt)[:MAX_PROMPT_CHARS], str(instruction)[:1000],
                     "\n".join(numerals or [])[:4000], png, mime))
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
        cur.execute("SELECT figure_id,version_no,instruction,numerals,status,error,created_at "
                    "FROM app_draft_figure_versions WHERE figure_id = ANY(%s) "
                    "ORDER BY figure_id, version_no DESC", ([f["id"] for f in figs],))
        versions = {}
        for r in cur.fetchall():
            versions.setdefault(r["figure_id"], []).append(dict(r))
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


def render_figure(project_id, user_id, *, label, caption, sections=None, instruction="",
                  figure_id=None, base_version=None, disclosure=""):
    """Generate (or re-generate) one figure and store the result as a new version.

    With `figure_id` this is an EDIT: the currently active image is passed back to the model with
    the instruction, so the change applies to that drawing rather than producing a new one.
    """
    sections = sections or {}
    numerals = numerals_for(sections, caption, disclosure)
    previous = None
    if figure_id:
        fig = get_figure(figure_id, user_id)
        if not fig:
            raise FigureError("no such figure")
        label = label or fig["figure_label"]
        caption = caption or fig["caption"]
        _, previous = png_bytes(figure_id, user_id, base_version)
    context = str(sections.get("summary") or disclosure or "")[:1200]
    prompt = build_prompt(label, caption, numerals, instruction, context)
    png = generate_png(prompt, previous_png=previous)
    if not figure_id:
        fig = create_figure(project_id, user_id, label, caption)
        figure_id = fig["id"]
    version = add_version(figure_id, prompt=prompt, instruction=instruction, numerals=numerals,
                          png=png)
    return {"figure_id": figure_id, "label": label, "caption": caption,
            "version_no": version["version_no"], "numerals": numerals}
