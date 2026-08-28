"""SEAM: the 170M-publication global catalog and the external APIs. Workstream E fills it.

WHAT THIS IS FOR. The local corpus is 4,984,254 publications. The measured reach failure it cannot
fix by ranking is the `schmalz` benchmark subject, four of whose ten cited documents are classified
in acoustics, exhaust silencers, vacuum cleaners and power tools, outside the eight indexed CPC
branches: no reordering of documents this corpus does not hold will ever produce them. That is what
the global tier is for, and it is why the architecture runs it IN PARALLEL with local search rather
than as a fallback after it.

CONTRACT.

    backend.search(query, *, subject, mode, limit, qvec) -> [(publication_id, score)] best-first

`publication_id` is the string `"fed:<PUBNUM>"` for a document with no local row (the convention
`federation.py` already uses and `Result.is_external` already tests), or a local bigint when the
global tier resolved the hit onto a publication this corpus holds. The orchestrator registers the
external ids with the retriever's family map before fusion so a global hit dedups against a local
one, and puts the results in under the channel name `"global"`.

UNTIL THEN. `search()` returns [] and `available()` returns False, so the orchestrator runs the
channel, gets nothing and carries on. That is the same fail-soft contract every other channel has:
a channel that returns nothing is not an error.

THE BACKEND OWNS ITS OWN CLOCK. There is no way to interrupt a call that has already started, so
`GLOBAL_TIMEOUT` is a budget the ORCHESTRATOR checks before starting the call, not a deadline it
can impose on one in flight. A backend that talks to an external API must bound its own request
timeouts, or a single hung provider makes every search as slow as that provider.
"""
from __future__ import annotations

import os

#  Seconds. Checked before the global task starts, so a task that queued behind other work past
#  its budget is skipped rather than added to the critical path of a search that has already
#  finished its local tiers. See `retrieval.orchestrator`.
GLOBAL_TIMEOUT = float(os.environ.get("GLOBAL_SEARCH_TIMEOUT", "60"))


class GlobalBackend:
    """Implement this in workstream E and hand it to `register_backend`."""

    name = "global"

    def available(self) -> bool:
        raise NotImplementedError

    def search(self, query, *, subject=None, mode=None, limit=2500, qvec=None) -> list:
        """-> [(publication_id, score)] best-first. See the module docstring for the id shape."""
        raise NotImplementedError

    def family_keys(self, publication_ids) -> dict:
        """publication_id -> a family key for the external ids in the result.

        Without this a global hit cannot be deduped against a local one and the same disclosure is
        shown twice. Return {} if the backend has no family data; the orchestrator then treats each
        external id as its own family, which is the safe direction (nothing is silently merged).
        """
        return {}

    def records(self, publication_ids) -> dict:
        """publication_id -> a display record for the EXTERNAL ids in the result. Optional.

        An external hit has no local row, so nothing downstream can read its title or abstract out
        of `chunks`: `Result.external` is where a renderer and the cross-encoder both look, and an
        id that is missing from it renders as a blank card and reranks on an empty passage. The
        record only has to duck-type `federation.FederatedHit`: `.title` and `.abstract` are what
        `fusion.best_text` reads, and a renderer additionally uses `.pub_number`, `.date`,
        `.assignee` and `.url` when they are present.

        Returning {} is allowed and costs display quality, not correctness.
        """
        return {}


class _NoGlobal(GlobalBackend):
    name = "global:none"

    def available(self):
        return False

    def search(self, query, *, subject=None, mode=None, limit=2500, qvec=None):
        return []


_BACKEND = _NoGlobal()


def register_backend(backend):
    """Called once at startup by workstream E."""
    global _BACKEND
    _BACKEND = backend or _NoGlobal()


def backend():
    return _BACKEND


def available() -> bool:
    try:
        return bool(_BACKEND.available())
    except Exception:
        return False


def search(query, *, subject=None, mode=None, limit=2500, qvec=None):
    """Run the global tier. NEVER raises and NEVER blocks the local answer: a failure here is an
    empty channel, because the local corpus alone is still a usable result and a global outage is
    not a reason to fail a search."""
    if not available():
        return []
    try:
        return list(_BACKEND.search(query, subject=subject, mode=mode, limit=limit, qvec=qvec) or [])
    except Exception:
        return []


def family_keys(publication_ids):
    if not available():
        return {}
    try:
        return dict(_BACKEND.family_keys(list(publication_ids)) or {})
    except Exception:
        return {}


def records(publication_ids):
    """Display records for external ids. Never raises; {} is a valid answer."""
    if not available():
        return {}
    try:
        return dict(_BACKEND.records(list(publication_ids)) or {})
    except Exception:
        return {}
