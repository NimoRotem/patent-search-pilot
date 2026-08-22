# V3 integration log

Branch `v3/H-integration`. Workstream H is the only one that merges. Newest entry last.

## Baseline, 2026-08-22 15:10 UTC

Measured before anything landed, on `v3/H-integration` at `517ecdf5`, the point every workstream
except A is based on.

```
~/v3run '~/v3/bin/pt ~/v3/H-integration -q -p no:randomly'
1 failed, 1681 passed, 94 skipped, 9 warnings in 505.89s (0:08:25)
```

Wall clock 14:59:36 to 15:10:26, of which about 2m25s was spent queued on the `pt` flock behind
another workstream's suite. The suite itself is **8m25s**. 119 files in `tests/`.

`-p no:randomly` was passed deliberately so the number is reproducible. A default run shuffles.

### The one pre-existing failure

```
FAILED tests/test_relevance_audit.py::test_cards_carry_server_rendered_content
  assert any(c["images"] for c in top)     # at least some have drawings on disk
```

**Environmental, not a code defect, and it fails identically in every V3 worktree.** The test loads
the gold report `grabo_gripper_novelty` and asserts that some of its top eight cards have drawing
images on disk. The deployed worktree `~/patent-search-pilot` holds 921 files in `data/ops_images`;
every `~/v3/*` worktree holds 23, because that image corpus is not in git and was never copied when
the worktrees were made.

Reproduced on `~/v3/E-shards`, which carries zero code changes:

```
~/v3run '~/v3/bin/pt ~/v3/E-shards tests/test_relevance_audit.py -q -p no:randomly'
1 failed, 15 passed in 4.93s
```

So: if your suite reports exactly this one failure, your branch is green. If it reports two, the
second one is yours. Nobody should spend time on this one, and no merge should be blamed for it.

### Baseline hygiene numbers

* **Em dashes already in the tree: 1,231**, across 181 text files under `src/ ops/ docs/ sql/
  tests/ *.md`. Four are in `docs/` and `sql/`, and `docs/` itself has none. This is pre-existing
  debt and cleaning it would conflict with all seven workstreams, so the guard at a merge is the
  **diff**, not the tree. Written so this file does not itself ship the character it bans:
  `git diff <base>..<branch> -- src ops docs sql tests '*.md' | grep -P '^\+.*\x{2014}'` must
  return nothing.
* **AI attribution: none.** Scanned every commit reachable from `drafting-ready`, both cutover
  branches and all seven workstream branches for `Co-Authored-By`, "Generated with", and any
  mention of an assistant. Clean.
* **Author identity in new commits is clean**, but the base history is not and cannot be fixed:
  of the last 200 commits on the base, 67 are `Nimo@rotem.ai <Nimo@rotem.ai@dianaotech.com>`, 61
  are `NimoRotem <NimoRotem@users.noreply.github.com>`, 50 are the correct
  `Nimo <Nimo@grabo.tech>`, and 22 are three other spellings. That history is deployed and shared,
  so rewriting it is off the table. The rule applies to **new** commits: check yours with
  `git log --format='%an <%ae>' <base>..HEAD` before you say a branch is ready.

## Landed

### 2026-08-22, `b380806e`, the deployed line, once

`origin/Nimo/drafting-ready` merged into `v3/H-integration`. Nine Draft Studio drawing fixes,
`84a637dc` through `bdb148db`, all authored `Nimo <Nimo@grabo.tech>`, no AI attribution, no em
dashes added.

**No conflicts.** The nine commits touch only `src/draft_figures.py`, `src/draft_qa.py`,
`src/draft_studio.py` and their two test files, none of which any V3 workstream owns.

Suite after the merge: `tests/test_draft_figures.py tests/test_draft_studio.py
tests/test_drafting_web.py`, 200 passed in 9.04s.

This was the one deliberate catch-up. Integration does not chase `drafting-ready` after this. It
moves all day, because the live Draft Studio agent keeps committing to it.

### 2026-08-22, `511a2490`, migration 006 restored to its adopted bytes

See `docs/migrations.md`, "The 006 incident". `migrate.py` was refusing every command against the
live database with `ChecksumDrift` on 006, which blocked migration work for all eight workstreams.
006 is back to `a7ad2750`, the new table moved to `sql/015_draft_turn_candidates.sql`, no DDL
changed. `migrate.py status` works again and reports `002` and `015` pending, nothing else.

The same commit rewrites `test_the_repo_migrations_are_discoverable_and_include_figure_images`,
which asserted the literal list 001 through 009 and therefore went red for **every** workstream
adding a migration, for a reason nothing to do with their change. It now asserts the property:
discovery finds exactly the numbered files on disk, in ascending numeric order, with no duplicate
version, and 001 through 009 still lead. Defect injected three ways to prove it still bites:

| Injection | Result |
|---|---|
| add `sql/016_zz_probe.sql` | passes, so the tripwire is gone |
| rename `009_durable_runs.sql` to `019_` | fails at the adopted-prefix assertion |
| add `sql/9_dup.sql` beside `009_` | fails on the duplicate numeric alias |

## Open, and what each is waiting on

| Branch | Ahead of base | Ready? |
|---|---|---|
| `v3/A-durable-worker` | 3 commits, plus uncommitted `sql/013_run_side_effects.sql`, `src/runartifact.py` | not yet, and see the two blockers below |
| `v3/B-corpus-manifest` | 0 commits | not started committing |
| `v3/C-fulltext-acquisition` | 0 commits, uncommitted `sql/012_fulltext_acquisition.sql`, `src/acquire/` | **number collision, see below** |
| `v3/D-embed-pipeline` | 0 commits, uncommitted `src/gcs_lite.py`, `src/parsed_norm.py`, `src/stage_chunks.py` | not started committing |
| `v3/E-shard-infra` | 0 commits, uncommitted `ops/shards/` | not started committing |
| `v3/F-corpus-release` | 0 commits, uncommitted `src/corpus/`, `vendor/` | not started committing |
| `v3/G-retrieval-wiring` | 0 commits, uncommitted work in `src/retrieval/` | not started committing |

### `Nimo/v3-run-cutover` is superseded. Do not merge it.

`origin/Nimo/v3-worker-cutover` already contains all three of run-cutover's commits, replayed onto
a newer `drafting-ready` merge. Proven by patch id, not by reading subjects:

```
git cherry origin/Nimo/v3-worker-cutover origin/Nimo/v3-run-cutover
- e1d71685   - d47c114c   - 9099dc37        (all three: already upstream)
```

`e1d71685`/`b3358a89`, `d47c114c`/`6f8cbcc0` and `9099dc37`/`20beac36` are pairwise identical
patch ids. So durable execution lands as **one** merge of `v3/A-durable-worker`, which descends
from `v3-worker-cutover`. Merging `v3-run-cutover` as well would add nothing and would drag a
staler `drafting-ready` base back in. It is not in the plan.

One caveat that matters when reading either branch: `v3-run-cutover` carries the **old** 006, the
one whose checksum the live ledger recorded. That is why the ledger and that branch agree while
everything newer did not.

### Blocker: `sql/012` is claimed twice

Workstream C has an uncommitted `sql/012_fulltext_acquisition.sql`. **012 is already taken** by
`sql/012_run_admission.sql`, which is committed and pushed on `origin/Nimo/v3-worker-cutover`.
`discover()` treats two files at the same version as a hard error, so the second one to land breaks
migrations for everyone. C renumbers to **014**, which costs one `git mv` of a file that is not yet
committed. The full assignment table is in `docs/migrations.md`.

### Blocker: `v3/A-durable-worker` is red before it is merged

A's branch adds `sql/012_run_admission.sql` without touching `tests/test_migrate.py`, so
`test_the_repo_migrations_are_discoverable_and_include_figure_images` fails on A's branch today.
`511a2490` on this branch fixes the test, so the failure disappears the moment A rebases on, or is
merged into, integration. Nothing for A to do beyond knowing why it is red.

A's three commits are also authored `NimoRotem <NimoRotem@users.noreply.github.com>` rather than
`Nimo <Nimo@grabo.tech>`. They arrived that way on `origin/Nimo/v3-worker-cutover` before A started,
so this is inherited, not A's doing, and rewriting a pushed branch under a running workstream is
worse than the defect. New commits must use the right identity: check with
`git config user.name` and `git config user.email` in your worktree before you commit.

## Standing notes

* **`patent-results` is being restarted about once an hour by somebody**, cleanly, via supervisor:
  09:33, 10:23, 11:35, 12:56, 14:19 and 14:59 on 2026-08-22, every one a deliberate
  `stopped (exit status 0)` and respawn, not a crash. Consistent with the live Draft Studio agent
  deploying its `drafting-ready` commits. Flagged, not acted on: it is not a V3 workstream's doing
  and it is not H's to stop.
* **Test runs leave untracked junk in `data/`**: `data/families/`, `data/manifests/`,
  `data/mongo_cache/`, `data/ops_images/`, `data/reports/*.trace.jsonl` and an export under
  `data/reports/exports/`. None of these are in `.gitignore`. Never `git add -A` in a worktree
  where the suite has run.
* **Migration application against the live database has not happened and is not scheduled.** Today's
  answer is no, while `patents-desc-backfill` runs and schema integration moves.
