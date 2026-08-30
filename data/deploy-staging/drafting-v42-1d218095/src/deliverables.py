"""The client-facing layer on top of a search: letterhead, framing, and a shareable link.

A prior-art search produces evidence. What a firm actually sends out is a DOCUMENT — on its own
letterhead, addressed to a named client under a matter reference, opening with why the search was
run and what it found, and only then showing the references. Everything above the reference list
was missing here: the export went straight from a generic cover page into the retrieval output,
so it read as a machine dump rather than work product, and there was no way to put it in front of
somebody who does not have a login.

This module holds that layer:

  * **letterhead**       — firm name, address, responsible attorney, contact email, and an
    uploaded logo, stored per user so it is typed once and reused;
  * **matter**           — client name, client reference number, matter title, and the subject
    application number and date;
  * **narrative**        — purpose of the search, key findings, and analysis, each editable and
    each draftable by the model FROM THE REPORT so the author starts from something rather than
    a blank box;
  * **sharing**          — a revocable capability token that opens a read-only view of the
    report for somebody with no account.

Two rules the rest of the app depends on:

  1. **The narrative never becomes a legal conclusion.** ``drafting`` already refuses phrases like
     "is patentable" or "does not infringe" in a generated application; the same guard runs over
     anything the model drafts here, because this text sits directly above a reference list and is
     the most likely place for an unsupported opinion to appear.
  2. **A share token grants exactly one report, read-only.** It carries no account, cannot reach
     any other slug, and is revocable; the token is stored hashed so a database copy does not hand
     over live links.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading

import db
import drafting

MAX_FIELD_CHARS = 400
MAX_NARRATIVE_CHARS = 20000
MAX_LOGO_BYTES = 512 * 1024
LOGO_MIMES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
SHARE_TOKEN_BYTES = 32

# Short identity/matter fields, in the order the letterhead renders them.
LETTERHEAD_FIELDS = ("firm_name", "firm_address", "firm_attorney", "attorney_email", "firm_detail")
MATTER_FIELDS = ("client_name", "client_reference_number", "matter_title",
                 "subject_patent_number", "subject_patent_date")
NARRATIVE_FIELDS = ("purpose", "key_findings", "analysis")
ALL_FIELDS = LETTERHEAD_FIELDS + MATTER_FIELDS + NARRATIVE_FIELDS

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS app_report_docs (
         id bigserial PRIMARY KEY,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         slug text NOT NULL,
         firm_name text NOT NULL DEFAULT '', firm_address text NOT NULL DEFAULT '',
         firm_attorney text NOT NULL DEFAULT '', attorney_email text NOT NULL DEFAULT '',
         firm_detail text NOT NULL DEFAULT '',
         client_name text NOT NULL DEFAULT '', client_reference_number text NOT NULL DEFAULT '',
         matter_title text NOT NULL DEFAULT '',
         subject_patent_number text NOT NULL DEFAULT '',
         subject_patent_date text NOT NULL DEFAULT '',
         purpose text NOT NULL DEFAULT '', key_findings text NOT NULL DEFAULT '',
         analysis text NOT NULL DEFAULT '',
         logo_mime text, logo_bytes bytea,
         share_token_hash text UNIQUE, share_created_at timestamptz,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (user_id, slug))""",
    "CREATE INDEX IF NOT EXISTS app_report_docs_slug_idx ON app_report_docs (slug)",
    # The letterhead is a property of the FIRM, not of one search: typing it again for every
    # report is the reason such fields end up blank. Defaults live on the user and are copied
    # into a new report document.
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS firm_name text NOT NULL DEFAULT ''",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS firm_address text NOT NULL DEFAULT ''",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS firm_attorney text NOT NULL DEFAULT ''",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS attorney_email text NOT NULL DEFAULT ''",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS firm_detail text NOT NULL DEFAULT ''",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS logo_mime text",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS logo_bytes bytea",
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
# field hygiene
# ---------------------------------------------------------------------------
def _short(value) -> str:
    """A single-line identity field: control characters stripped, length capped."""
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))
    return re.sub(r"[ \t]+", " ", s).strip()[:MAX_FIELD_CHARS]


def _long(value) -> str:
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))
    return s.strip()[:MAX_NARRATIVE_CHARS]


def clean_fields(payload) -> dict:
    """Accept only the known fields, cleaned. Anything else in the payload is ignored."""
    out = {}
    payload = payload or {}
    for key in LETTERHEAD_FIELDS + MATTER_FIELDS:
        if key in payload:
            out[key] = _short(payload[key])
    for key in NARRATIVE_FIELDS:
        if key in payload:
            out[key] = _long(payload[key])
    return out


def _row(row):
    if row is None:
        return None
    d = dict(row)
    d.pop("logo_bytes", None)                 # never travel with the record; served on its own route
    d.pop("share_token_hash", None)           # a hash is not a link and must not look like one
    d["has_logo"] = bool(d.pop("_has_logo", False))
    return d


# ---------------------------------------------------------------------------
# read / write
# ---------------------------------------------------------------------------
def get(user_id, slug):
    """The report document for this user + search, or None."""
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT *, (logo_bytes IS NOT NULL) AS _has_logo, "
                    "(share_token_hash IS NOT NULL) AS is_shared "
                    "FROM app_report_docs WHERE user_id=%s AND slug=%s",
                    (int(user_id), str(slug)))
        return _row(cur.fetchone())


def get_or_create(user_id, slug):
    """Existing document, or a new one pre-filled from the user's saved letterhead."""
    existing = get(user_id, slug)
    if existing:
        return existing
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT firm_name,firm_address,firm_attorney,attorney_email,firm_detail,"
                    "logo_mime,logo_bytes FROM app_users WHERE id=%s", (int(user_id),))
        u = cur.fetchone() or {}
        cur.execute(
            """INSERT INTO app_report_docs
                 (user_id,slug,firm_name,firm_address,firm_attorney,attorney_email,firm_detail,
                  logo_mime,logo_bytes)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (user_id,slug) DO UPDATE SET updated_at=now()
               RETURNING *, (logo_bytes IS NOT NULL) AS _has_logo,
                         (share_token_hash IS NOT NULL) AS is_shared""",
            (int(user_id), str(slug), u.get("firm_name", ""), u.get("firm_address", ""),
             u.get("firm_attorney", ""), u.get("attorney_email", ""), u.get("firm_detail", ""),
             u.get("logo_mime"), u.get("logo_bytes")))
        return _row(cur.fetchone())


def save(user_id, slug, payload, *, remember_letterhead: bool = True):
    """Persist edits. Letterhead fields are also stored on the user as the default for next time."""
    fields = clean_fields(payload)
    get_or_create(user_id, slug)
    if not fields:
        return get(user_id, slug)
    ensure_schema()
    sets = ", ".join(f"{k}=%s" for k in fields)
    with db.cursor() as cur:
        cur.execute(f"UPDATE app_report_docs SET {sets}, updated_at=now() "
                    "WHERE user_id=%s AND slug=%s "
                    "RETURNING *, (logo_bytes IS NOT NULL) AS _has_logo, "
                    "(share_token_hash IS NOT NULL) AS is_shared",
                    (*fields.values(), int(user_id), str(slug)))
        out = _row(cur.fetchone())
        lh = {k: v for k, v in fields.items() if k in LETTERHEAD_FIELDS}
        if remember_letterhead and lh:
            cur.execute("UPDATE app_users SET " + ", ".join(f"{k}=%s" for k in lh) +
                        ", updated_at=now() WHERE id=%s", (*lh.values(), int(user_id)))
    return out


def set_logo(user_id, slug, data: bytes, mime: str, *, remember: bool = True):
    """Store an uploaded logo. Rejects anything that is not a small, real image."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime not in LOGO_MIMES:
        raise ValueError("the logo must be a PNG, JPEG, GIF or WebP image")
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError(f"the logo must be under {MAX_LOGO_BYTES // 1024} KB")
    if not _sniff_image(data, mime):
        raise ValueError("that file is not a valid image")
    get_or_create(user_id, slug)
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("UPDATE app_report_docs SET logo_mime=%s, logo_bytes=%s, updated_at=now() "
                    "WHERE user_id=%s AND slug=%s", (mime, data, int(user_id), str(slug)))
        if remember:
            cur.execute("UPDATE app_users SET logo_mime=%s, logo_bytes=%s WHERE id=%s",
                        (mime, data, int(user_id)))
    return True


def _sniff_image(data: bytes, mime: str) -> bool:
    """Magic bytes must agree with the declared type — a file that merely claims to be a PNG is
    refused rather than stored and later served back with an image content type."""
    head = data[:12]
    if mime == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
    if mime == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def clear_logo(user_id, slug):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("UPDATE app_report_docs SET logo_mime=NULL, logo_bytes=NULL, updated_at=now() "
                    "WHERE user_id=%s AND slug=%s", (int(user_id), str(slug)))


def logo(user_id, slug):
    """(mime, bytes) or (None, None)."""
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT logo_mime, logo_bytes FROM app_report_docs "
                    "WHERE user_id=%s AND slug=%s", (int(user_id), str(slug)))
        r = cur.fetchone()
    if not r or not r.get("logo_bytes"):
        return None, None
    return r["logo_mime"], bytes(r["logo_bytes"])


# ---------------------------------------------------------------------------
# sharing: one report, read-only, revocable
# ---------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(("patents-share:" + str(token)).encode()).hexdigest()


def create_share(user_id, slug) -> str:
    """Mint a fresh share token and return it ONCE. Any previous link stops working."""
    get_or_create(user_id, slug)
    token = secrets.token_urlsafe(SHARE_TOKEN_BYTES)
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("UPDATE app_report_docs SET share_token_hash=%s, share_created_at=now(), "
                    "updated_at=now() WHERE user_id=%s AND slug=%s",
                    (_hash_token(token), int(user_id), str(slug)))
    return token


def revoke_share(user_id, slug):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("UPDATE app_report_docs SET share_token_hash=NULL, share_created_at=NULL, "
                    "updated_at=now() WHERE user_id=%s AND slug=%s", (int(user_id), str(slug)))


def by_share_token(token):
    """Resolve a share token to its ONE report document, or None.

    The token is a capability for a single slug. It is looked up by hash, so the stored value is
    not itself a working link, and it carries no user session — the holder can read that report
    and nothing else.
    """
    if not token or len(str(token)) > 200:
        return None
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT *, (logo_bytes IS NOT NULL) AS _has_logo, true AS is_shared "
                    "FROM app_report_docs WHERE share_token_hash=%s", (_hash_token(token),))
        return _row(cur.fetchone())


def logo_by_share_token(token):
    if not token:
        return None, None
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT logo_mime, logo_bytes FROM app_report_docs WHERE share_token_hash=%s",
                    (_hash_token(token),))
        r = cur.fetchone()
    if not r or not r.get("logo_bytes"):
        return None, None
    return r["logo_mime"], bytes(r["logo_bytes"])


# ---------------------------------------------------------------------------
# model-drafted narrative, grounded in the report and stripped of conclusions
# ---------------------------------------------------------------------------
_PURPOSE_SYS = (
    "You are a patent professional writing the PURPOSE section that opens a prior-art search "
    "report for a client. In 60-110 words state what was searched for and why: the subject "
    "technology, the question the search was run to answer, and the scope actually covered. "
    "Describe the search, not its outcome. State no opinion on patentability, novelty, "
    "obviousness, validity or infringement. "
    'Return ONLY JSON: {"text":"<the purpose>"}'
)

_FINDINGS_SYS = (
    "You are a patent professional writing the KEY FINDINGS of a prior-art search report. "
    "You are given the search query and the references that were found, with what each one "
    "discloses. In 120-220 words summarise what the art shows: which features of the invention "
    "are disclosed in the references and by which of them, which features were not found in any "
    "reference, and where the closest art sits. Refer to references by publication number. "
    "Report only what the supplied material supports. State no opinion on patentability, "
    "novelty, obviousness, validity or infringement, and do not recommend a course of action. "
    'Return ONLY JSON: {"text":"<the key findings>"}'
)

_ANALYSIS_SYS = (
    "You are a patent professional writing the ANALYSIS section of a prior-art search report. "
    "For each of the closest references given, in one short paragraph each, set out what it "
    "discloses that is relevant and what it does not disclose, referring to it by publication "
    "number. Ground every statement in the supplied text. State no opinion on patentability, "
    "novelty, obviousness, validity or infringement. 200-400 words total. "
    'Return ONLY JSON: {"text":"<the analysis>"}'
)

_SYS_FOR = {"purpose": _PURPOSE_SYS, "key_findings": _FINDINGS_SYS, "analysis": _ANALYSIS_SYS}


def _material(view, report, kind):
    """The report facts a draft is written from — the same ones the page shows."""
    cards = (view or {}).get("cards") or []
    bits = ["SEARCH QUERY: " + str((report or {}).get("query") or "")[:4000],
            "SEARCH MODE: " + str((report or {}).get("mode") or "novelty")]
    elements = (view or {}).get("elements") or []
    if elements:
        bits.append("TECHNICAL ELEMENTS OF THE INVENTION:\n- " +
                    "\n- ".join(str(e.get("text") or e)[:300] for e in elements[:20]))
    if kind == "purpose":
        return "\n\n".join(bits)
    take = 8 if kind == "analysis" else 15
    for c in cards[:take]:
        line = [f"{c.get('pub')} — {str(c.get('title') or '')[:200]}"]
        if c.get("basis"):
            line.append(f"  basis: {c['basis']}")
        reads = c.get("reads_on") or []
        if reads:
            line.append("  reads on: " + "; ".join(str(r)[:120] for r in reads[:6]))
        why = c.get("why") or c.get("rationale") or ""
        if why:
            line.append("  " + str(why)[:600])
        bits.append("\n".join(line))
    uncovered = (view or {}).get("uncovered_elements") or []
    if uncovered:
        bits.append("ELEMENTS NO REFERENCE DISCLOSED:\n- " +
                    "\n- ".join(str(u)[:200] for u in uncovered[:15]))
    return "\n\n".join(bits)


def suggest(kind, view, report):
    """Draft one narrative section from the report. Returns "" when unavailable.

    The output passes through the SAME legal-conclusion guard the generated application uses.
    This text sits immediately above a list of prior art, which makes it the likeliest place in
    the product for an unsupported opinion to appear, and a sentence like "the invention is
    patentable over the cited art" in a document on a firm's letterhead is not a wording problem.
    """
    if kind not in _SYS_FOR:
        raise ValueError(f"unknown section {kind!r}")
    import llm
    material = _material(view, report, kind)
    if not material.strip():
        return ""
    d = llm.chat_json(_SYS_FOR[kind], material, max_tokens=1600) or {}
    text = _long(d.get("text"))
    if not text:
        return ""
    return strip_legal_conclusions(text)


def legal_conclusions(text):
    """Sentences that state a legal conclusion, using ``drafting``'s own pattern set.

    Deliberately reuses `drafting._LEGAL_CONCLUSION_PATTERNS` rather than keeping a second list:
    two copies of a rule like this drift, and the drafted application and the client-facing report
    must refuse exactly the same sentences.
    """
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        for pat in drafting._LEGAL_CONCLUSION_PATTERNS:
            if pat.search(sentence):
                out.append(sentence)
                break
    return out


def strip_legal_conclusions(text):
    """Drop offending sentences rather than the whole draft — the rest is usually sound.

    ``drafting`` REJECTS a generated application outright for the same offence, which is right
    there: the author asked for a document and must not silently receive a censored one. Here the
    author is going to read and edit this text before it goes anywhere, so removing the one bad
    sentence and keeping the useful draft is more helpful than returning nothing.
    """
    offending = set(legal_conclusions(text))
    if not offending:
        return text
    keep = [s for s in re.split(r"(?<=[.!?])\s+", text or "") if s not in offending]
    return " ".join(keep).strip()


def is_configured(doc) -> bool:
    """True when the document carries enough to be worth printing as a letterhead."""
    if not doc:
        return False
    return any(str(doc.get(k) or "").strip()
               for k in LETTERHEAD_FIELDS + MATTER_FIELDS + NARRATIVE_FIELDS)
