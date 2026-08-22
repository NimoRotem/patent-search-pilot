"""A `ParsedDoc` -> the chunk rows that go into `chunks_stage_v3`.

This is `src/chunker.py` applied to a document that is not in the corpus yet. Kind for kind and
clip for clip it produces what the original ingest produced, because a vector is only comparable
to the corpus if the TEXT that was embedded was built the same way: `whole` is title plus abstract
clipped at 2,000 characters, everything else is clipped at 8,000, a dependent claim gets both its
own and its parent-resolved text, an independent claim gets only its own, and a non-English
original abstract is emitted alongside the English one rather than instead of it.

Two deliberate differences from `chunker.py`, both structural rather than cosmetic:

  * **`ref_id` is always NULL.** `chunks_stage_v3` is shared with `ops/desc_backfill.py`, which
    writes `kind='paragraph'` with `ref_id = paragraphs.id`. Keeping every row this pipeline
    writes at `ref_id IS NULL` means the two jobs cannot collide on the table's partial unique
    index `(kind, ref_id) WHERE ref_id IS NOT NULL`, and any reader can tell whose row it is
    without a join.
  * **`coord` always carries `pub`**, the publication number, because a document that is not in
    `publications` has no id a reader could resolve back to a number.
"""
from __future__ import annotations

import hashlib
import json

WHOLE_CHARS = 2000        # chunker.py: title + abstract
MAX_CHARS = 8000          # chunker.py / desc_backfill.py: everything else
MIN_CHARS = 20            # desc_backfill.py: below this a "paragraph" is a numbering artefact

CHUNKER_VERSION = "chunker-2026.08.22"   # the same reading of the same rules as desc_backfill


def clip(s, n=MAX_CHARS):
    s = (s or "").strip()
    return s[:n] if len(s) > n else s


def tok(s):
    return max(1, len(s) // 4)


def item_key(source_key: str, kind: str, slot: str, text: str) -> str:
    """Stable identity for one chunk of work.

    Content is part of the key, so re-running a document with the SAME text is free and re-running
    it with better text is a new item rather than a silent no-op.
    """
    h = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return hashlib.sha256(f"{source_key}\x1e{kind}\x1e{slot}\x1e{h}".encode()).hexdigest()


def chunk_rows(doc, source_key: str) -> list:
    """-> `[{kind, ref_id, coord, lang, text, token_count, item_key}]`, in corpus order."""
    pub = doc.publication_number
    rows = []

    def add(kind, coord, lang, text, slot):
        text = clip(text) if kind != "whole" else text
        if not text or len(text) < (MIN_CHARS if kind == "paragraph" else 1):
            return
        coord = dict(coord or {})
        coord["pub"] = pub
        rows.append({"kind": kind, "ref_id": None, "coord": coord, "lang": lang or "en",
                     "text": text, "token_count": tok(text),
                     "item_key": item_key(source_key, kind, slot, text)})

    whole = clip((doc.title or "") + (". " + doc.abstract if doc.abstract else ""), WHOLE_CHARS)
    if whole:
        add("whole", {}, "en", whole, "whole")

    if doc.abstract:
        add("abstract", {}, "en", doc.abstract, "abstract")
    if doc.abstract_orig:
        add("abstract", {"lang": doc.abstract_orig_lang or doc.lang},
            doc.abstract_orig_lang or doc.lang, doc.abstract_orig,
            f"abstract:{doc.abstract_orig_lang or doc.lang}")

    for c in doc.claims:
        no = c["claim_no"]
        own = clip(c.get("text"))
        if own:
            add("claim_own", {"claim_no": no}, c.get("lang") or doc.lang, own, f"claim_own:{no}")
        res = clip(c.get("resolved_text"))
        if res and res != own:                      # dependent claims only, else a duplicate
            add("claim_resolved", {"claim_no": no}, c.get("lang") or doc.lang, res,
                f"claim_resolved:{no}")

    for p in doc.paragraphs:
        add("paragraph", {"para_no": p.get("para_no"), "heading": p.get("heading"),
                          "page_no": p.get("page_no")},
            p.get("lang") or doc.lang, p.get("text"), f"paragraph:{p.get('para_no')}")

    for f in doc.figures:
        if f.get("caption"):
            add("figure_caption", {"figure_no": f.get("figure_no")}, "en", f["caption"],
                f"figure_caption:{f.get('figure_no')}:{len(rows)}")

    return rows


def coord_json(coord):
    return json.dumps(coord, default=str)
