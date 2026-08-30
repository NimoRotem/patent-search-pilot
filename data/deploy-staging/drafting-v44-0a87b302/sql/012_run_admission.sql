-- Admission: the difference between a row that is WAITING and a row that is RUNNABLE.
--
-- The producer records every accepted search as a queued run, including the ones the caps turned
-- away, because that is the user-facing behaviour that already existed: a gate-full or cap-full
-- search queues and reports running, it is not refused. But `claim` took any queued row, so a
-- worker would have executed rows that were never admitted and spent real money on them.
--
-- 010 and 011 are reserved for the corpus and eval workstreams, so this is 012.
--
-- THE ROWS ARE THE DAILY LEDGER. `charged_day` is the UTC day a run spent from its lane's budget;
-- NULL means it has never been charged. That makes "charge exactly once" a property of the row
-- rather than an invariant somebody has to remember, and a UTC rollover is a date changing rather
-- than a counter that has to be reset.

-- Added NULLABLE and WITHOUT a default on purpose. NULL is the one-time sentinel meaning "this
-- row predates admission", which is the only safe way to identify them: `false` cannot be used,
-- because after this migration `false` is also the legitimate state of a genuinely refused row.
-- Re-running the migration would then admit every waiting row, which is a cap bypass triggered by
-- an ordinary redeploy.
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS admitted    boolean;
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS admitted_at timestamptz;
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS charged_day date;

-- One-time transition. On a re-run there are no NULLs left, so this matches nothing.
UPDATE search_runs
   SET admitted = true,
       admitted_at = COALESCE(admitted_at, enqueued_at),
       charged_day = COALESCE(charged_day, (enqueued_at AT TIME ZONE 'UTC')::date)
 WHERE admitted IS NULL;

-- Only now does false become the default and the column become total.
ALTER TABLE search_runs ALTER COLUMN admitted SET DEFAULT false;
ALTER TABLE search_runs ALTER COLUMN admitted SET NOT NULL;

--  The claim scan reads only admitted queued rows, so the flag has to be in the index or every
--  claim degrades into a scan of everything anyone ever queued.
CREATE INDEX IF NOT EXISTS ix_search_runs_runnable
    ON search_runs (priority, enqueued_at) WHERE status = 'queued' AND admitted;

--  Waiting-for-admission, scanned by the admission sweep in enqueued order.
CREATE INDEX IF NOT EXISTS ix_search_runs_waiting
    ON search_runs (lane, priority, enqueued_at) WHERE status = 'queued' AND NOT admitted;

--  The daily budget query: how many runs in this lane have charged today.
CREATE INDEX IF NOT EXISTS ix_search_runs_charged_day
    ON search_runs (lane, charged_day) WHERE charged_day IS NOT NULL;
