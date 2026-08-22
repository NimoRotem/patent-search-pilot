"""Continuous full-text acquisition for the niche corpus.

WHY THIS EXISTS
---------------
Text starvation in this system is circular, and it has been measured. Enrichment fetched text only
for the references a run had already chosen to read, and that choice was a screen score computed
from the text that had not been fetched. The attorney's single most comprehensive match was
screened 10/100 on a title alone, at retrieval rank 1,817. Separately, 14,379,018 description
paragraphs exist in the corpus and only 1,689,243 are chunked. Text we do not hold is art we
cannot find.

So acquisition has to stop being a side effect of reading. This package is a continuously running
fetcher driven by a NICHE MANIFEST (workstream B's), not by any run's read set.

WHERE THE OUTPUT GOES, AND NOWHERE ELSE
---------------------------------------
    GCS raw/{publication}/{provider}.{ext}.gz     exactly what the provider returned
    GCS parsed/{publication}/{provider}.json      the normalised record
    sources_docstore                              merge-never-overwrite, keyed by publication
    corpus_ingest_queue                           the request to join the next permanent release

Never `publications`, `chunks`, `claims`, `paragraphs`, `classifications`, `citations` or any
other live retrieval table. An insert into `chunks` is an insert into a 94 GB HNSW graph while
production is querying it, and that has already blocked live searches. The worker calls
`corpus_guard.arm()` at startup, so the prohibition is a property of its connections rather than
a convention: see `worker.run()`, and
`tests/test_fulltext_acquire.py::test_worker_run_arms_the_corpus_guard`.

THE MODULES
-----------
    manifest    the seam onto workstream B's niche manifest, plus a provisional reader
    tasks       the work pool: dedup by primary key, lease with expiry, reaper
    ledger      progress and cost: one event row per provider attempt, plus the hard budget
    providers   the cascade, cheapest and most complete first
    blobstore   GCS raw/ and parsed/
    worker      the loop that ties them together
"""
