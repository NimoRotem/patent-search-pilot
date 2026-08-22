"""The fetch worker: lease a partition of the pool, run the cascade, store, mark, repeat.

DURABILITY. Nothing lives in a process global. The pool is a table, the lease is a column with an
expiry, and progress is a state transition committed before the next publication starts. Kill this
worker at any point and the rows it held come back to the pool when `tasks.reap()` next runs; the
text it had already stored is already in `sources_docstore`, which merges rather than overwrites,
so the repeat costs nothing but the fetch it did not finish.

SAFETY. `corpus_guard.arm()` is called before anything else. From that point every connection this
process opens refuses a statement that writes `publications`, `chunks`, `claims`, `paragraphs`,
`classifications`, `citations` or any of the rest, in this process, before Postgres is asked. The
worker cannot write a live retrieval table even by accident, and
`tests/test_fulltext_acquire.py::test_worker_run_arms_the_corpus_guard` fails if the arming is
removed.

A HANGING PROVIDER MUST NOT STALL THE PARTITION. Each rung runs inside its own `Gate`: bounded
concurrency, a minimum interval, a wall-clock timeout and a breaker. A timeout costs one
publication one rung's timeout, then the cascade falls through; eight consecutive failures open
the breaker and the rung is skipped entirely for five minutes. There is also a per-publication
deadline, so a publication cannot absorb the sum of every rung's timeout.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import traceback

import httpx

from . import blobstore, ledger, providers, tasks

BATCH = int(os.environ.get("FULLTEXT_BATCH", "24"))
IN_FLIGHT = int(os.environ.get("FULLTEXT_IN_FLIGHT", "8"))
IDLE_SLEEP = float(os.environ.get("FULLTEXT_IDLE_SLEEP", "30"))
PUBLICATION_DEADLINE = float(os.environ.get("FULLTEXT_PUBLICATION_DEADLINE", "240"))
HEARTBEAT_SECONDS = float(os.environ.get(
    "FULLTEXT_HEARTBEAT_SECONDS", str(max(15.0, tasks.LEASE_SECONDS / 4.0))))
#  BEHIND search-time demand, which uses the corpus_ingest_queue default of 100. A publication a
#  live search asked for is more urgent to the next release than one a bulk niche sweep asked for,
#  and `pending_ingest(limit=N)` returns the top N by priority: at 80 this fetcher put tens of
#  thousands of rows in front of every search-time request and pushed them out of the window.
INGEST_PRIORITY = int(os.environ.get("FULLTEXT_INGEST_PRIORITY", "150"))


def log(msg: str) -> None:
    print(f"[fulltext {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class Worker:
    def __init__(self, shard: int = 0, of: int = 1, cascade=None, *, dry_run: bool = False,
                 partitions=None, manifests=None):
        self.shard, self.of = int(shard), int(of)
        self.partitions = (list(partitions) if partitions is not None
                           else tasks.partitions_for(self.shard, self.of))
        self.id = tasks.worker_id()
        self.cascade = cascade if cascade is not None else providers.build()
        self.dry_run = dry_run
        self.manifests = list(manifests) if manifests else None
        self.stopping = asyncio.Event()
        self.held = set()
        self.fetched = 0
        self.missed = 0
        self.started = time.time()

    # -- the cascade ---------------------------------------------------------------------------
    async def _try(self, prov, pub: str, task: dict, client, events: list):
        """One rung, for one publication. Never raises: a rung that fails is a rung that misses."""
        ok, why = prov.available()
        base = {"worker": self.id, "partition_id": task["partition_id"],
                "publication_number": pub, "provider": prov.name}
        if not ok:
            events.append(dict(base, outcome="skipped", detail=why[:200]))
            return None
        if not prov.covers(pub):
            return None
        if prov.gate.open():
            events.append(dict(base, outcome="breaker", detail=prov.gate.reason))
            return None

        reserved = 0.0
        if prov.budget_key and prov.credits:
            cap = providers.DEFAULT_CAPS.get(prov.budget_key, 0.0)
            got = await asyncio.to_thread(ledger.reserve, prov.budget_key, prov.credits, cap)
            if not got["granted"]:
                events.append(dict(base, outcome="budget",
                                   detail=f"cap {got['cap']:.0f} spent {got['spent']:.0f}"))
                return None
            reserved = prov.credits

        t0 = time.monotonic()
        try:
            res = await prov.gate.run(lambda: prov.fetch(pub, client))
        except asyncio.TimeoutError:
            prov.gate.note_fail("timeout")
            if reserved:
                await asyncio.to_thread(ledger.refund, prov.budget_key, reserved)
            events.append(dict(base, outcome="timeout",
                               latency_ms=int((time.monotonic() - t0) * 1000),
                               detail=f"exceeded {prov.gate.timeout:.0f}s"))
            return None
        except Exception as exc:
            prov.gate.note_fail(type(exc).__name__)
            if reserved:
                await asyncio.to_thread(ledger.refund, prov.budget_key, reserved)
            events.append(dict(base, outcome="error",
                               latency_ms=int((time.monotonic() - t0) * 1000),
                               detail=f"{type(exc).__name__}: {str(exc)[:160]}"))
            return None

        ms = int((time.monotonic() - t0) * 1000)
        prov.gate.note_ok()
        if res is None or not res.complete():
            #  A rung that answered with a stub still spent its credit. Keep the charge; the
            #  refund is only for a call that did not happen. The INCOMPLETE result is returned
            #  rather than swallowed, because `reached` on it is what stops the rungs below
            #  buying the same nothing: see cascade_for.
            events.append(dict(base, outcome="miss", latency_ms=ms,
                               credits=(res.credits if res else 0.0),
                               usd=(res.usd if res else 0.0),
                               claims_chars=len(res.claims) if res else 0,
                               desc_chars=len(res.description) if res else 0,
                               detail=("upstream reached, no full text there" if res
                                       else "no answer")))
            return res
        events.append(dict(base, outcome="hit", latency_ms=ms, credits=res.credits, usd=res.usd,
                           claims_chars=len(res.claims), desc_chars=len(res.description),
                           provider=res.provider, detail=""))
        return res

    async def cascade_for(self, pub: str, task: dict, client, events: list):
        """Down the rungs until one answers in full.

        A rung that REACHED its upstream and found no full text there settles the question for
        every rung reading the same upstream. `serp_self`, `scrapingbee` and `serpapi` all read
        the Google Patents index: when the free one fetches the page and the page has no claims
        and no description section, the two paid ones fetch the identical page and produce the
        identical nothing. Before this existed, 1,018 old FR / SE / GB / NL / AT documents cost
        15,480 ScrapingBee credits and the entire $4.58 SerpApi budget doing exactly that.
        """
        deadline = time.monotonic() + PUBLICATION_DEADLINE
        settled: set = set()
        for prov in self.cascade:
            if self.stopping.is_set() or time.monotonic() > deadline:
                return None
            if prov.upstream and prov.upstream in settled:
                events.append({"worker": self.id, "partition_id": task["partition_id"],
                               "publication_number": pub, "provider": prov.name,
                               "outcome": "settled",
                               "detail": f"{prov.upstream} was reached already and holds no "
                                         f"full text for this publication"})
                continue
            res = await self._try(prov, pub, task, client, events)
            if res is not None and res.complete():
                return res
            if res is not None and res.reached and prov.upstream:
                settled.add(prov.upstream)
        return None

    # -- storage -------------------------------------------------------------------------------
    async def store(self, pub: str, res, client, events: list, task: dict) -> dict:
        """GCS raw + GCS parsed + sources_docstore + corpus_ingest_queue. Nothing else, ever."""
        uris = {"raw_uri": "", "parsed_uri": ""}
        rec = res.record()
        if self.dry_run:
            return uris

        if blobstore.enabled():
            try:
                if res.raw:
                    uris["raw_uri"] = await blobstore.put_raw(
                        client, pub, res.provider.replace(":", "_"), res.raw,
                        ext=res.raw_ext or "bin")
                uris["parsed_uri"] = await blobstore.put_parsed(
                    client, pub, res.provider.replace(":", "_"),
                    dict(rec, publication_number=pub, fetched_at=time.time()))
            except Exception as exc:
                #  Do not lose text we have already paid for because a bucket is unhappy. The
                #  empty raw_uri on the task row is what a later re-upload pass looks for.
                events.append({"worker": self.id, "partition_id": task["partition_id"],
                               "publication_number": pub, "provider": "gcs", "outcome": "error",
                               "detail": f"{type(exc).__name__}: {str(exc)[:160]}"})

        from sources import docstore
        await asyncio.to_thread(docstore._put_sync, pub, rec)

        import runstore
        try:
            await asyncio.to_thread(
                lambda: runstore.queue_for_ingest(
                    pub,
                    reason=f"niche acquisition ({task.get('manifest') or 'manifest'})",
                    source=res.provider, scratch_ref=f"sources_docstore:{pub}",
                    payload={"claims_chars": len(res.claims), "desc_chars": len(res.description),
                             "raw_uri": uris["raw_uri"], "parsed_uri": uris["parsed_uri"]},
                    priority=INGEST_PRIORITY))
        except Exception as exc:
            events.append({"worker": self.id, "partition_id": task["partition_id"],
                           "publication_number": pub, "provider": "ingest_queue",
                           "outcome": "error",
                           "detail": f"{type(exc).__name__}: {str(exc)[:160]}"})
        return uris

    # -- one publication -----------------------------------------------------------------------
    async def handle(self, task: dict, client, events: list) -> None:
        pub = task["publication_number"]
        try:
            res = await self.cascade_for(pub, task, client, events)
        except Exception:
            traceback.print_exc()
            res = None
        if res is None:
            ok = await asyncio.to_thread(
                tasks.complete, pub, self.id, state="missing",
                error="no provider in the cascade returned complete text")
            #  Out of `held` the moment the row is no longer leased, whatever the outcome. A
            #  finished row left in `held` is renewed by the next heartbeat, comes back absent
            #  (renew only matches state='leased') and is reported as a LOST LEASE. That reads
            #  as a stalled worker to an operator and it is nothing of the kind.
            self.held.discard(pub)
            if ok:
                self.missed += 1
            return
        uris = await self.store(pub, res, client, events, task)
        ok = await asyncio.to_thread(
            tasks.complete, pub, self.id, state="done", provider=res.provider,
            claims_chars=len(res.claims), desc_chars=len(res.description),
            raw_uri=uris["raw_uri"], parsed_uri=uris["parsed_uri"])
        self.held.discard(pub)
        if ok:
            self.fetched += 1
        else:
            #  The lease went while we were fetching. The text is stored (the docstore merges, so
            #  that is never a loss); the row belongs to whoever holds it now.
            log(f"lease lost for {pub} mid-fetch; text stored, row left to its new owner")

    # -- the loop ------------------------------------------------------------------------------
    async def _heartbeat(self):
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=HEARTBEAT_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            if not self.held:
                continue
            try:
                still = set(await asyncio.to_thread(tasks.renew, list(self.held), self.id))
            except Exception:
                traceback.print_exc()
                continue
            lost = self.held - still
            if lost:
                log(f"lost the lease on {len(lost)} publication(s): {sorted(lost)[:5]}")
                self.held = still

    async def run(self, max_publications: int = 0, max_batches: int = 0) -> dict:
        tasks.require_schema()
        log(f"worker {self.id} shard {self.shard}/{self.of} partitions={self.partitions} "
            f"cascade={[p.name for p in self.cascade]} "
            f"bucket={blobstore.bucket_name() or '(none)'}")
        for p in self.cascade:
            #  `why` is the adapter's static explanation of what it WOULD need; it is only
            #  interesting when the rung is actually off. Printing it beside "available" reads
            #  like a contradiction and is how an operator stops trusting the startup banner.
            ok, why = p.available()
            log(f"  rung {p.name}: " + ("available" if ok else f"INERT ({why})"))

        hb = asyncio.create_task(self._heartbeat())
        limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
        batches = 0
        try:
            async with httpx.AsyncClient(limits=limits, timeout=90.0,
                                         follow_redirects=True) as client:
                while not self.stopping.is_set():
                    try:
                        reaped = await asyncio.to_thread(tasks.reap)
                    except Exception:
                        traceback.print_exc()
                        reaped = {}
                    if reaped.get("requeued") or reaped.get("failed"):
                        log(f"reaper: {reaped}")

                    want = BATCH
                    if max_publications:
                        want = min(want, max_publications - self.fetched - self.missed)
                        if want <= 0:
                            break
                    batch = await asyncio.to_thread(
                        tasks.lease, self.partitions, self.id, want, None, self.manifests)
                    if not batch:
                        if max_batches or max_publications:
                            break
                        await self._sleep(IDLE_SLEEP)
                        continue
                    self.held = {t["publication_number"] for t in batch}
                    events: list = []
                    sem = asyncio.Semaphore(IN_FLIGHT)

                    async def one(t):
                        async with sem:
                            if self.stopping.is_set():
                                return
                            await self.handle(t, client, events)

                    await asyncio.gather(*[one(t) for t in batch])
                    if events:
                        try:
                            await asyncio.to_thread(ledger.record_many, events)
                        except Exception:
                            traceback.print_exc()
                    self.held = set()
                    batches += 1
                    rate = (self.fetched + self.missed) / max(1e-6, time.time() - self.started)
                    log(f"batch {batches}: fetched={self.fetched} missed={self.missed} "
                        f"({rate * 3600:.0f}/h)")
                    if max_batches and batches >= max_batches:
                        break
        finally:
            self.stopping.set()
            hb.cancel()
            if self.held:
                try:
                    await asyncio.to_thread(tasks.release, list(self.held), self.id)
                except Exception:
                    traceback.print_exc()
        return {"worker": self.id, "fetched": self.fetched, "missed": self.missed,
                "batches": batches, "seconds": round(time.time() - self.started, 1)}

    async def _sleep(self, seconds: float):
        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


def run(shard: int = 0, of: int = 1, *, max_publications: int = 0, max_batches: int = 0,
        dry_run: bool = False) -> dict:
    """Arm the corpus guard, then run. Arming FIRST is the point: it is a property of every
    connection this process opens from here on, not a rule somebody remembered."""
    import corpus_guard
    corpus_guard.arm("full-text acquisition worker: writes only sources_docstore, "
                     "corpus_ingest_queue and GCS")

    w = Worker(shard, of, dry_run=dry_run)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_):
        log("shutdown signal: finishing the batch in flight, then releasing the leases")
        loop.call_soon_threadsafe(w.stopping.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except ValueError:
            pass
    try:
        return loop.run_until_complete(
            w.run(max_publications=max_publications, max_batches=max_batches))
    finally:
        loop.close()


if __name__ == "__main__":
    shard = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    of = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    print(json.dumps(run(shard, of), indent=2))
