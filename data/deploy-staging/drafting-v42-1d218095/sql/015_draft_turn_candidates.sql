-- Draft Studio blocked-candidate carry-over (src/draft_studio.py).
--
-- A candidate that cleared structural parsing but exhausted an automatic repair cycle is not a
-- published version. Keep it separately so a leased retry continues from that work and its exact
-- review instead of rebuilding the last published version and repeating the same failed attempt.
--
-- Why this is 015 and not part of 006. The DDL below was appended to sql/006_draft_agent.sql in
-- commit e4199f5b, on the deployed Nimo/drafting-ready line, hours after 006 had been adopted into
-- the live ledger. Editing an applied migration in place is exactly what the checksum guard exists
-- to catch, and it did: migrate.py refused every command with ChecksumDrift on 006, which blocked
-- migration work for all eight V3 workstreams, not only Draft Studio. 006 has been restored to the
-- bytes the ledger recorded and the new table moved here, where it can be applied or adopted on
-- its own. The table already exists on the live database, created out of band, so 015 probes
-- present and CREATE TABLE IF NOT EXISTS makes applying it a no-op either way.
CREATE TABLE IF NOT EXISTS app_draft_turn_candidates (
  turn_id bigint PRIMARY KEY REFERENCES app_draft_turns(id) ON DELETE CASCADE,
  snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  qa_report jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
