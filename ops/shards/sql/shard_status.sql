-- The shard's own readiness ledger. Lives in the SHARD database, not in the corpus database:
-- the process that loads a shard commits the corpus rows and the readiness flip in one
-- transaction, and a file on disk cannot be in that transaction.
--
-- WHY IT EXISTS. `available()` has to be able to say False while an index is mid build. An empty
-- result set and a genuine miss are indistinguishable to fusion, and a miss is scored as a recall
-- failure, so a shard that is up but not loaded must be reported as NOT serving rather than as
-- serving nothing.
--
-- WHO WRITES IT. Workstream F, at the end of a load, in the load's own transaction:
--     UPDATE shard_status SET state='ready', generation=..., n_chunks=..., updated_at=now();
-- `ops/shards/shardctl.sh ready <shard>` does the same thing by hand, for an operator.

CREATE TABLE IF NOT EXISTS shard_status (
    shard       text PRIMARY KEY,
    state       text NOT NULL DEFAULT 'building',   -- building | ready | draining
    generation  text NOT NULL DEFAULT '',           -- the corpus release this shard holds
    n_chunks    bigint NOT NULL DEFAULT 0,
    note        text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
