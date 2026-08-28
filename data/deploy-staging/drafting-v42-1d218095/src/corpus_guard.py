"""Make the live corpus read only inside an explicitly armed process.

WHY
---
Search time enrichment wrote straight into the retrieval database: `enrich._persist_full_text`
inserts claims, paragraphs and chunks, updates `publications.abstract` and then embeds the new
chunks, which is an insert into a 94 GB HNSW graph while live searches are querying it. That has
blocked live searches behind index maintenance. `ops.py` and `incremental_ingest.py` do the same
thing from the batch side, which is legitimate: the difference is not WHAT is written, it is WHO
is writing.

So the prohibition cannot be a convention. It is a property of the CONNECTION: every connection
opened by an armed process carries a cursor class that refuses any statement which writes a
protected table, before the statement reaches Postgres. Retrieval additionally gets a genuinely
`READ ONLY` transaction, which Postgres enforces itself even if this module were bypassed.

HOW A PROCESS DECIDES
---------------------
    armed      the dormant standalone search worker calls `arm()` at startup. Every db.connect()
               and db.cursor() in that process is guarded from then on. The production web process
               remains unarmed until its route is cut over to that worker.
    unarmed    the offline ingestion CLIs (ingest_bq, ingest_pg, chunker, embed, ops,
               incremental_ingest, the ops/ scripts) never arm, so they write exactly as before.
               `PATENT_CORPUS_INGEST=1` also disarms, for a batch job started from an armed
               parent.

There is one named, auditable escape hatch, `allow_corpus_writes()`, for code that is genuinely
the ingestion path but happens to run inside an armed process. It is a context manager, it is
thread scoped, and grepping for it lists every exception in the tree. Nothing in the search path
uses it.

WHAT A SEARCH DOES INSTEAD
--------------------------
Demand fetched material goes to the scratch store (`sources_docstore`) and the publication is
queued for the next permanent corpus release in `corpus_ingest_queue`. See
docs/corpus_write_policy.md.
"""
from __future__ import annotations

import contextlib
import os
import re
import threading

#  THE LIVE CORPUS. The six named in the rebuild brief, plus the tables that are part of the same
#  retrieval asset and are written by the same enrichment code path. `families` does not exist in
#  the pilot schema (family identity lives on publications.family_id); it is listed because the v3
#  corpus will have it and a table appearing later must not appear unprotected.
PROTECTED_TABLES = frozenset({
    "publications", "chunks", "classifications", "citations", "families", "parties",
    #  Written by enrich._persist_full_text / ops._persist in the same breath as the above.
    "claims", "claim_dependencies", "paragraphs", "figures", "figure_images", "legal_events",
    "field_provenance", "applications", "sources",
    #  Benchmark embedding tables: same index-maintenance hazard, same offline-only rule.
    "bench_emb_1024", "bench_emb_3072", "seedpub",
})

#  Session settings that would let a caller undo the prohibition from inside SQL.
_FORBIDDEN_SET = re.compile(
    r"\bset\s+(?:session\s+|local\s+)?(default_transaction_read_only|session_replication_role|"
    r"role|authorization)\b", re.IGNORECASE)

_IDENT = r'((?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)(?:\s*\.\s*(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*))?)'

#  Every statement form that can change a table, with the table it changes. `FOR UPDATE` is
#  excluded from the UPDATE pattern by the negative lookbehind: it is a row lock in a SELECT, and
#  the writes it precedes are caught on their own.
_WRITE_PATTERNS = [
    ("insert",   re.compile(r"\binsert\s+into\s+" + _IDENT, re.IGNORECASE)),
    ("update",   re.compile(r"(?<!for )(?<!for no key )\bupdate\s+(?:only\s+)?" + _IDENT, re.IGNORECASE)),
    ("delete",   re.compile(r"\bdelete\s+from\s+(?:only\s+)?" + _IDENT, re.IGNORECASE)),
    ("copy_to",  re.compile(r"\bcopy\s+" + _IDENT + r"\s*(?:\([^)]*\))?\s*from\b", re.IGNORECASE)),
    ("merge",    re.compile(r"\bmerge\s+into\s+" + _IDENT, re.IGNORECASE)),
    ("truncate", re.compile(r"\btruncate\s+(?:table\s+)?(?:only\s+)?" + _IDENT, re.IGNORECASE)),
    ("alter",    re.compile(r"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?" + _IDENT, re.IGNORECASE)),
    ("drop",     re.compile(r"\bdrop\s+table\s+(?:if\s+exists\s+)?" + _IDENT, re.IGNORECASE)),
    ("index",    re.compile(r"\bcreate\s+(?:unique\s+)?index\s+(?:concurrently\s+)?"
                            r"(?:if\s+not\s+exists\s+)?(?:\S+\s+)?on\s+" + _IDENT, re.IGNORECASE)),
    ("reindex",  re.compile(r"\breindex\s+(?:table|index)\s+(?:concurrently\s+)?" + _IDENT, re.IGNORECASE)),
    ("cluster",  re.compile(r"\bcluster\s+(?:verbose\s+)?" + _IDENT, re.IGNORECASE)),
    #  VACUUM / ANALYZE take option words before the table: VACUUM (FULL) ANALYZE publications.
    ("vacuum",   re.compile(r"\b(?:vacuum|analyze)\b(?:\s*\([^)]*\))?"
                            r"(?:\s+(?:full|freeze|verbose|analyze|skip_locked))*\s+"
                            + _IDENT, re.IGNORECASE)),
    ("lock",     re.compile(r"\block\s+(?:table\s+)?(?:only\s+)?" + _IDENT, re.IGNORECASE)),
    ("grant",    re.compile(r"\b(?:grant|revoke)\b.*?\bon\s+(?:table\s+)?" + _IDENT, re.IGNORECASE | re.DOTALL)),
    ("create",   re.compile(r"\bcreate\s+(?:(?:temporary|temp|unlogged)\s+)?table\s+"
                            r"(?:if\s+not\s+exists\s+)?" + _IDENT, re.IGNORECASE)),
    ("comment",  re.compile(r"\bcomment\s+on\s+(?:table|view|materialized\s+view|index)\s+"
                            + _IDENT, re.IGNORECASE)),
    ("trigger",  re.compile(r"\bcreate\s+(?:or\s+replace\s+)?trigger\s+\S+.*?\bon\s+"
                            + _IDENT, re.IGNORECASE | re.DOTALL)),
]

_MULTI_TARGET_PATTERNS = (
    re.compile(r"\btruncate\s+(?:table\s+)?(?:only\s+)?(?P<targets>.*?)"
               r"(?=\s+(?:restart|continue)\s+identity\b|\s+(?:cascade|restrict)\b|;|$)",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"\bdrop\s+table\s+(?:if\s+exists\s+)?(?P<targets>.*?)"
               r"(?=\s+(?:cascade|restrict)\b|;|$)", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(?:grant|revoke)\b.*?\bon\s+(?:table\s+)?(?P<targets>.*?)"
               r"(?=\s+(?:to|from)\b)", re.IGNORECASE | re.DOTALL),
)
_TARGET_AT_START = re.compile(r"^\s*(?:only\s+)?" + _IDENT, re.IGNORECASE)

#  A statement that writes SOMETHING but whose target we could not read is a defect in this
#  matcher, not a permission. Fail closed on the verb.
_ANY_WRITE_VERB = re.compile(
    r"\b(insert|delete|merge|truncate|reindex|cluster|vacuum)\b|"
    r"(?<!\bexplain )\banalyze\s+[\w\"]|"
    r"(?<!for )(?<!for no key )\bupdate\b|"
    r"\balter\s+table\b|\bdrop\s+(?:table|view|materialized\s+view|index|trigger|rule|policy|"
    r"function|procedure)\b|\bcreate\s+(?:(?:or\s+replace|temporary|temp|unlogged|unique)\s+)*"
    r"(?:table|index|trigger|rule|policy|function|procedure)\b|\bcopy\b|"
    r"\b(?:grant|revoke)\b|\bcomment\s+on\b|\brefresh\s+materialized\s+view\b", re.IGNORECASE)

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


class CorpusWriteBlocked(RuntimeError):
    """A search path tried to write the live corpus."""


# ---------------------------------------------------------------------------------------------
# process / thread state
# ---------------------------------------------------------------------------------------------
_armed = False
_local = threading.local()


def armed() -> bool:
    """True when this process guards its connections."""
    if os.environ.get("PATENT_CORPUS_INGEST", "") in ("1", "true", "yes"):
        return False
    return _armed


def arm(reason=""):
    """Guard every connection this process opens from now on. Idempotent."""
    global _armed
    if not _armed:
        _armed = True
        print(f"[corpus_guard] armed{': ' + reason if reason else ''} - the live corpus is read "
              f"only in this process ({len(PROTECTED_TABLES)} tables)", flush=True)
    return _armed


def disarm():
    """Only for tests and for a batch process that deliberately took the ingestion role."""
    global _armed
    _armed = False


@contextlib.contextmanager
def allow_corpus_writes(reason):
    """THE ONE ESCAPE HATCH, thread scoped and named. Ingestion only.

    `reason` is required and is printed, so an exception that appears in a log can be traced to
    the code that took it. Nothing in the search path may call this; grep for it to enumerate
    every exception in the tree.
    """
    if not reason:
        raise ValueError("allow_corpus_writes needs a reason")
    prev = getattr(_local, "allowed", None)
    _local.allowed = reason
    try:
        yield
    finally:
        _local.allowed = prev


def writes_allowed() -> bool:
    return not armed() or bool(getattr(_local, "allowed", None))


# ---------------------------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------------------------
def _bare(ident: str) -> str:
    """publications / public.chunks / "Chunks"  ->  the table name, lowercased, unquoted."""
    last = ident.split(".")[-1].strip()
    return last.strip('"').lower()


def _sql_code(sql: str) -> str:
    """Return SQL code with comments and literal values replaced by spaces.

    A regex cannot strip comments safely: ``'-- data'; UPDATE publications`` contains a comment
    token inside a string, and treating it as syntax hides the real UPDATE that follows. This
    small lexer preserves code and quoted identifiers while masking single-quoted, dollar-quoted,
    line-comment and nested block-comment regions. Newlines are preserved for readable errors.
    """
    chars = list(sql)
    n = len(chars)

    def blank(start, end):
        for pos in range(start, min(end, n)):
            if chars[pos] not in "\r\n":
                chars[pos] = " "

    i = 0
    while i < n:
        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            end = n if end < 0 else end
            blank(i, end)
            i = end
            continue
        if sql.startswith("/*", i):
            depth, end = 1, i + 2
            while end < n and depth:
                if sql.startswith("/*", end):
                    depth += 1
                    end += 2
                elif sql.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            blank(i, end)
            i = end
            continue
        if sql[i] == "'":
            end = i + 1
            while end < n:
                if sql[end] == "'":
                    if end + 1 < n and sql[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                if sql[end] == "\\" and end + 1 < n:
                    end += 2
                else:
                    end += 1
            blank(i, end)
            i = end
            continue
        if sql[i] == '"':
            # Quoted identifier, not a value. Preserve it, but skip comment detection inside it.
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if sql[i] == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                tag = match.group(0)
                close = sql.find(tag, match.end())
                end = n if close < 0 else close + len(tag)
                blank(i, end)
                i = end
                continue
        i += 1
    return "".join(chars)


def check(sql) -> None:
    """Raise CorpusWriteBlocked if `sql` would write a protected table. Cheap: two regex passes
    over a string, only when the process is armed."""
    if writes_allowed():
        return
    text = sql if isinstance(sql, str) else (sql.decode("utf-8", "replace")
                                             if isinstance(sql, (bytes, bytearray))
                                             else str(sql))
    stripped = _sql_code(text)
    if _FORBIDDEN_SET.search(stripped):
        raise CorpusWriteBlocked(
            "a search path may not change the transaction/role settings that enforce the corpus "
            "write prohibition: " + " ".join(stripped.split())[:200])
    if not _ANY_WRITE_VERB.search(stripped):
        return                                     # a read. The overwhelmingly common case.
    targets = []
    for _kind, pat in _WRITE_PATTERNS:
        for m in pat.finditer(stripped):
            targets.append(_bare(m.group(1)))
    for pat in _MULTI_TARGET_PATTERNS:
        for match in pat.finditer(stripped):
            for item in match.group("targets").split(","):
                target = _TARGET_AT_START.match(item)
                if target:
                    targets.append(_bare(target.group(1)))
    hit = [t for t in targets if t in PROTECTED_TABLES]
    if hit:
        raise CorpusWriteBlocked(
            f"write to the live corpus table(s) {', '.join(sorted(set(hit)))} from a search "
            f"process is prohibited. Demand fetched material belongs in the scratch store and in "
            f"corpus_ingest_queue (docs/corpus_write_policy.md). Statement: "
            + " ".join(stripped.split())[:200])
    if not targets:
        #  A write verb we could not attribute to a table. Fail closed: an unparsed write is
        #  exactly the case a permissive default would let through.
        raise CorpusWriteBlocked(
            "a write statement whose target table could not be identified was refused by the "
            "corpus guard (fail closed). Statement: " + " ".join(stripped.split())[:200])


# ---------------------------------------------------------------------------------------------
# the guarded cursor. Installed as the connection's cursor_factory, so EVERY cursor derived from
# a guarded connection is guarded, including the implicit one behind Connection.execute().
# ---------------------------------------------------------------------------------------------
def guarded_cursor_class():
    import psycopg

    class GuardedCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            check(query)
            return super().execute(query, params, **kw)

        def executemany(self, query, params_seq, **kw):
            check(query)
            return super().executemany(query, params_seq, **kw)

        def stream(self, query, params=None, **kw):
            check(query)
            return super().stream(query, params, **kw)

        def copy(self, statement, params=None, **kw):
            check(statement)
            return super().copy(statement, params, **kw)

    return GuardedCursor


_CURSOR_CLASS = None


def cursor_class():
    global _CURSOR_CLASS
    if _CURSOR_CLASS is None:
        _CURSOR_CLASS = guarded_cursor_class()
    return _CURSOR_CLASS
