"""Building the release's Tantivy index, and measuring what it costs.

WHY THE BUILDER OWNS THIS. A release is not complete when the vectors are loaded: the lexical
channel is half of retrieval, and a shard whose Tantivy index was built from a different set of
chunks than its vectors is a shard that answers two different questions depending on the channel.
So the lexical index is built in the same pass, from the same rows, and its checksum goes in the
same manifest. Workstream C owns the QUERY side (`LexicalBackend` in docs/lexical_interface.md);
this module owns only the artifact and the analyzer configuration recorded beside it.

CJK IS THE REASON THE ANALYZER IS CONFIGURED RATHER THAN DEFAULT. 39.9% of the corpus is CJK and
`to_tsvector('english')` has no segmenter for it, so that share is not degraded, it is dead. A
default Tantivy analyzer is no better: its simple tokenizer splits on non-alphanumeric boundaries,
and a run of Chinese characters has none, so a whole sentence becomes one term that no query will
ever match. Two fields solve it without paying ngram costs on the whole corpus:

    text      simple tokenizer + lowercase        every chunk
    text_cjk  character bigrams + lowercase       only chunks that contain CJK characters

Bigrams are the standard cheap CJK segmentation and they are exact-recall for two-character terms,
which is most technical vocabulary. Restricting the field to chunks that actually contain CJK
keeps the ngram explosion off the 60.1% of the corpus that does not need it.

The analyzer configuration is written into the manifest, so a shard verifies that it is querying
an index built the way it thinks it was.
"""
from __future__ import annotations

import os
import re
import sys

#  The venv on the patents VM is production's venv, shared by every workstream, so a package is
#  not installed into it on this branch's account. `vendor/` is a worktree-local install that only
#  this module reaches for. Workstream C adds tantivy to requirements.txt when the query side
#  lands; until then this keeps the builder able to produce a real index rather than a placeholder.
_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.append(_VENDOR)

try:
    import tantivy                                            # noqa: F401
    TANTIVY_ERROR = ""
except Exception as exc:                                      # pragma: no cover - import guard
    tantivy = None
    TANTIVY_ERROR = str(exc)

#  Hiragana, katakana, CJK extension A, CJK unified ideographs, Hangul syllables.
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")

ANALYZER_VERSION = "corpus-lexical/1"
CJK_MIN_GRAM = 2
CJK_MAX_GRAM = 2


def available():
    return tantivy is not None


def has_cjk(text):
    return bool(CJK_RE.search(text or ""))


def analyzer_config(store_text=False):
    """What went into the index, recorded in the manifest so a shard can check it."""
    return {"engine": "tantivy",
            "engine_version": getattr(tantivy, "__version__", "") if tantivy else "",
            "analyzer_version": ANALYZER_VERSION,
            "fields": {
                "text": {"tokenizer": "simple", "filters": ["lowercase"], "stored": bool(store_text)},
                "text_cjk": {"tokenizer": f"ngram({CJK_MIN_GRAM},{CJK_MAX_GRAM})",
                             "filters": ["lowercase"], "stored": False,
                             "populated_when": "chunk text contains CJK characters"},
                "kind": {"tokenizer": "raw", "stored": True},
                "family_key": {"tokenizer": "raw", "stored": True},
                "lang": {"tokenizer": "raw", "stored": True},
                "chunk_id": {"type": "i64", "stored": True, "fast": True},
                "publication_id": {"type": "i64", "stored": True, "fast": True},
            }}


def _schema(store_text):
    sb = tantivy.SchemaBuilder()
    #  Signed 64 bit, not unsigned: tantivy-py infers I64 from a Python int and a u64
    #  column then panics inside the writer thread on the first commit, which surfaces as an
    #  opaque "An error occurred in a thread" with no field name in it.
    sb.add_integer_field("chunk_id", stored=True, fast=True)
    sb.add_integer_field("publication_id", stored=True, fast=True)
    sb.add_text_field("family_key", stored=True, tokenizer_name="raw")
    sb.add_text_field("kind", stored=True, tokenizer_name="raw")
    sb.add_text_field("lang", stored=True, tokenizer_name="raw")
    sb.add_text_field("text", stored=bool(store_text), tokenizer_name="corpus_text")
    sb.add_text_field("text_cjk", stored=False, tokenizer_name="corpus_cjk")
    return sb.build()


def _register(index):
    text = (tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
            .filter(tantivy.Filter.lowercase()).build())
    cjk = (tantivy.TextAnalyzerBuilder(
        tantivy.Tokenizer.ngram(CJK_MIN_GRAM, CJK_MAX_GRAM, False))
        .filter(tantivy.Filter.lowercase()).build())
    index.register_tokenizer("corpus_text", text)
    index.register_tokenizer("corpus_cjk", cjk)


def open_index(path, store_text=False):
    if not available():
        raise RuntimeError(f"tantivy is not importable: {TANTIVY_ERROR}")
    index = tantivy.Index(_schema(store_text), path=str(path))
    _register(index)
    return index


def dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def build(path, docs, *, store_text=False, heap_mb=256, threads=1, commit_every=200_000):
    """Build a Tantivy index at `path` from an iterable of chunk dicts.

    -> {"docs", "cjk_docs", "bytes", "bytes_per_doc", "path", "analyzer"}.

    `store_text=False` by default: the snippet a `LexicalHit` carries can be fetched from the
    release database by `chunk_id`, which is one primary-key lookup, and storing the text in
    Tantivy as well duplicates a measured 902 bytes per chunk for nothing.
    """
    os.makedirs(path, exist_ok=True)
    index = open_index(path, store_text=store_text)
    writer = index.writer(heap_size=int(heap_mb) * 1024 * 1024, num_threads=int(threads))
    n = cjk_n = 0
    for d in docs:
        text = d.get("text") or ""
        fields = {"chunk_id": int(d["chunk_id"]),
                  "publication_id": int(d.get("publication_id") or 0),
                  "family_key": str(d.get("family_key") or ""),
                  "kind": str(d.get("kind") or ""),
                  "lang": str(d.get("lang") or ""),
                  "text": text}
        if has_cjk(text):
            fields["text_cjk"] = text
            cjk_n += 1
        writer.add_document(tantivy.Document(**fields))
        n += 1
        if commit_every and n % commit_every == 0:
            writer.commit()
    writer.commit()
    writer.wait_merging_threads()
    index.reload()
    size = dir_bytes(path)
    return {"docs": n, "cjk_docs": cjk_n, "bytes": size,
            "bytes_per_doc": round(size / n, 1) if n else 0.0,
            "path": str(path), "analyzer": analyzer_config(store_text)}


def count(path, store_text=False):
    """Documents actually in the index on disk. Used to verify a restored snapshot."""
    index = open_index(path, store_text=store_text)
    index.reload()
    return index.searcher().num_docs


def search(path, query_text, *, limit=10, fields=("text", "text_cjk"), store_text=False):
    """A smoke-test search. The real query path is workstream C's `LexicalBackend`."""
    index = open_index(path, store_text=store_text)
    index.reload()
    searcher = index.searcher()
    q = index.parse_query(query_text, list(fields))
    out = []
    for score, addr in searcher.search(q, limit).hits:
        doc = searcher.doc(addr)
        out.append({"score": float(score),
                    "chunk_id": (doc.get_first("chunk_id") or 0),
                    "publication_id": (doc.get_first("publication_id") or 0),
                    "kind": doc.get_first("kind") or ""})
    return out
