"""Immutable releases, atomic activation, and the rollback, against a real Postgres.

WHERE THESE RUN. Not on the live corpus and not on the builder database either: a throwaway
database is created on the `relbuild` cluster named by CORPUS_RELEASE_DSN, sql/010 is applied to
it, and it is dropped at the end. So these exercise the real triggers, the real partitioning and
the real pgvector, and a test can neither read nor damage a release anybody is serving.

THE THING BEING PROVED. Activation is the only moment this system changes what it searches. One
transaction across every shard key, so a query cannot fan out across two generations; refused when
the new release is measurably worse than the one it replaces; and reversible, in both directions.
"""
import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from corpus import manifest as manifest_mod, release_store, stats as stats_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#  The module-scoped fixture below is built BEFORE conftest's function-scoped autouse fixtures,
#  so `config` (which loads .env) may not have been imported yet and CORPUS_RELEASE_DSN would not
#  be in the environment. Load it here rather than depending on import order.
from dotenv import load_dotenv                                          # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))


def _admin_dsn():
    try:
        return release_store.dsn()
    except release_store.ReleaseError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def scratch_dsn():
    """A throwaway database on the release cluster, with sql/010 applied.

    Applying the migration here is itself worth something: sql/010 has NOT been applied to the
    live database (two files claim 010 until workstream M renumbers the niche pipeline to 018),
    so this is the only place it is executed at all.
    """
    admin = _admin_dsn()
    name = f"reltest_{uuid.uuid4().hex[:12]}"
    try:
        adm = psycopg.connect(admin, autocommit=True)
    except Exception as exc:                                    # noqa: BLE001
        pytest.skip(f"release cluster unreachable: {str(exc)[:120]}")
    with adm.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    adm.close()

    dsn = " ".join(p for p in admin.split() if not p.startswith("dbname=")) + f" dbname={name}"
    conn = psycopg.connect(dsn, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(open(os.path.join(ROOT, "sql", "010_corpus_release.sql"),
                         encoding="utf-8").read())
    conn.close()
    try:
        yield dsn
    finally:
        adm = psycopg.connect(admin, autocommit=True)
        with adm.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname=%s AND pid <> pg_backend_pid()", (name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        adm.close()


@pytest.fixture(scope="module")
def target_dsn(scratch_dsn):
    """A SECOND throwaway database: the shard a snapshot is restored onto."""
    admin = _admin_dsn()
    name = f"reltgt_{uuid.uuid4().hex[:12]}"
    adm = psycopg.connect(admin, autocommit=True)
    with adm.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    adm.close()
    dsn = " ".join(p for p in admin.split() if not p.startswith("dbname=")) + f" dbname={name}"
    c = psycopg.connect(dsn, autocommit=True)
    with c.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(open(os.path.join(ROOT, "sql", "010_corpus_release.sql"),
                         encoding="utf-8").read())
    c.close()
    try:
        yield dsn
    finally:
        adm = psycopg.connect(admin, autocommit=True)
        with adm.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname=%s AND pid <> pg_backend_pid()", (name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        adm.close()


@pytest.fixture()
def conn(scratch_dsn):
    c = psycopg.connect(scratch_dsn, row_factory=dict_row, autocommit=False)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


VEC = "[" + ",".join(["0.01"] * 768) + "]"


def _seed(conn, shard_key, *, version, chunks=3, families=("F1",), note=""):
    """Build a minimal release by hand: rows, partition, index, manifest, seal."""
    rel = release_store.create(conn, shard_key=shard_key, version=version, kind="domain",
                               note=note)
    rid = rel["release_id"]
    release_store.ensure_partition(conn, rid)
    with conn.cursor() as cur:
        n = 0
        for fam in families:
            for i in range(chunks):
                n += 1
                cur.execute("""INSERT INTO chunks_release (release_id, chunk_id, family_key,
                                   publication_id, publication_number, home_domain, kind, lang,
                                   text, token_count, embedding, embed_model, embed_dim, source)
                               VALUES (%s,%s,%s,%s,%s,'B65G','claim_own','en',%s,10,%s,
                                       'test-model',768,'corpus')""",
                            (rid, n, fam, 1000 + n, f"US-{n}-A", f"a claim about grippers {n}",
                             VEC))
    release_store.add_members(conn, rid, [
        {"family_key": f, "home_domain": "B65G", "secondary_domains": ["B25J"],
         "n_publications": chunks, "n_chunks": chunks, "source": "corpus"} for f in families])
    release_store.set_shard_rows(conn, rid, [
        {"domain": "B65G", "n_families": len(families), "n_publications": chunks * len(families),
         "n_chunks": chunks * len(families), "index_bytes": 0, "disk_bytes": 0}])
    idx = release_store.build_indexes(conn, rid, m=16, ef_construction=64)
    st = stats_mod.compute(conn, rid, lexical_docs=chunks * len(families))
    man = manifest_mod.build(release_id=rid, shard_key=shard_key, version=version, kind="domain",
                             domains=["B65G"], counts=release_store.observed_counts(conn, rid),
                             built_from={"chunks_max_id": 1}, index_params={"dense": idx},
                             artifacts=[], stats=st, timings={}, root=ROOT, note=note)
    release_store.seal(conn, rid, man)
    conn.commit()
    return rid


# ------------------------------------------------------------------ immutability
def test_a_sealed_release_cannot_be_edited(conn):
    rid = _seed(conn, "t_imm", version=1)
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("UPDATE corpus_release SET note='edited' WHERE release_id=%s", (rid,))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM corpus_release WHERE release_id=%s", (rid,))
    conn.rollback()
    #  state is the one column that may move, because activation has to be able to set it.
    with conn.cursor() as cur:
        cur.execute("UPDATE corpus_release SET state='verified' WHERE release_id=%s", (rid,))
    conn.commit()
    assert release_store.get(conn, rid)["state"] == "verified"


def test_the_chunk_payload_of_a_sealed_release_cannot_be_rewritten(conn):
    """A past search's corpus must stay what it was. Retiring a release is DROP PARTITION, a DDL
    decision somebody takes on purpose, not an UPDATE nobody notices."""
    rid = _seed(conn, "t_payload", version=1)
    for sql in ("UPDATE chunks_release SET text='rewritten' WHERE release_id=%s",
                "DELETE FROM chunks_release WHERE release_id=%s",
                "UPDATE corpus_release_member SET n_chunks=0 WHERE release_id=%s",
                "DELETE FROM corpus_release_shard WHERE release_id=%s"):
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute(sql, (rid,))
        conn.rollback()


def test_an_unsealed_release_cannot_be_activated(conn):
    rel = release_store.create(conn, shard_key="t_unsealed", version=1)
    conn.commit()
    with pytest.raises(release_store.NotSealed):
        release_store.activate(conn, rel["release_id"], actor="test")
    conn.rollback()


# ------------------------------------------------------------------ the switch
def test_activation_is_one_transaction_across_every_shard_key(conn, scratch_dsn):
    """The property the fan-out depends on: a search asks the hot shard and two domain shards, and
    there is no instant at which two generations are both visible."""
    a = _seed(conn, "t_atomic_a", version=1)
    b = _seed(conn, "t_atomic_b", version=1)

    other = psycopg.connect(scratch_dsn, row_factory=dict_row, autocommit=True)
    try:
        assert release_store.active_set(other) == {}
        release_store.activate_many(conn, [a, b], actor="test", reason="both", commit=False)
        #  Mid-switch, and the concurrent reader sees NEITHER half.
        assert release_store.active_set(other) == {}
        conn.commit()
        seen = release_store.active_set(other)
        assert set(seen) == {"t_atomic_a", "t_atomic_b"}
        assert seen["t_atomic_a"]["generation"] == seen["t_atomic_b"]["generation"], \
            "one activation is one generation, or a query can straddle two"
    finally:
        other.close()


def test_activation_refuses_an_autocommit_connection(conn, scratch_dsn):
    """Autocommit would make a multi-shard activation several transactions, which is exactly the
    window this design exists to remove."""
    auto = psycopg.connect(scratch_dsn, row_factory=dict_row, autocommit=True)
    try:
        with pytest.raises(release_store.ReleaseError):
            release_store.activate_many(auto, ["anything"], actor="test")
    finally:
        auto.close()


def test_rollback_returns_the_shard_to_the_release_it_was_serving(conn):
    v1 = _seed(conn, "t_roll", version=1, families=("F1", "F2"))
    release_store.activate(conn, v1, actor="test", reason="first")
    v2 = _seed(conn, "t_roll", version=2, families=("F1", "F2", "F3"))
    release_store.activate(conn, v2, actor="test", reason="second")

    now = release_store.active(conn, "t_roll")
    assert now["release_id"] == v2 and now["previous_release_id"] == v1

    r = release_store.rollback(conn, "t_roll", actor="test", reason="v2 was bad")
    assert r["release_id"] == v1
    assert r["rolled_back_from"] == v2
    back = release_store.active(conn, "t_roll")
    assert back["release_id"] == v1
    assert back["generation"] > now["generation"], "a rollback is a new generation"
    assert release_store.get(conn, v1)["state"] == "active"
    assert release_store.get(conn, v2)["state"] == "superseded"


def test_a_rollback_is_itself_reversible(conn):
    """The pair is SWAPPED, not popped, so an operator who rolls back the wrong shard undoes it
    with the same call rather than needing a build."""
    v1 = _seed(conn, "t_roll2", version=1, families=("F1", "F2"))
    release_store.activate(conn, v1, actor="test")
    v2 = _seed(conn, "t_roll2", version=2, families=("F1", "F2", "F3"))
    release_store.activate(conn, v2, actor="test")

    assert release_store.rollback(conn, "t_roll2", actor="test")["release_id"] == v1
    assert release_store.rollback(conn, "t_roll2", actor="test")["release_id"] == v2
    assert release_store.active(conn, "t_roll2")["release_id"] == v2


def test_rollback_is_one_transaction_too(conn, scratch_dsn):
    v1 = _seed(conn, "t_rollatomic", version=1, families=("F1", "F2"))
    release_store.activate(conn, v1, actor="test")
    v2 = _seed(conn, "t_rollatomic", version=2, families=("F1", "F2", "F3"))
    release_store.activate(conn, v2, actor="test")

    other = psycopg.connect(scratch_dsn, row_factory=dict_row, autocommit=True)
    try:
        release_store.rollback(conn, "t_rollatomic", actor="test", commit=False)
        assert release_store.active(other, "t_rollatomic")["release_id"] == v2
        conn.commit()
        assert release_store.active(other, "t_rollatomic")["release_id"] == v1
    finally:
        other.close()


def test_rollback_refuses_when_there_is_nothing_to_roll_back_to(conn):
    v1 = _seed(conn, "t_first", version=1)
    release_store.activate(conn, v1, actor="test")
    with pytest.raises(release_store.ReleaseError):
        release_store.rollback(conn, "t_first", actor="test")
    conn.rollback()
    with pytest.raises(release_store.ReleaseError):
        release_store.rollback(conn, "t_never_activated", actor="test")
    conn.rollback()


def test_reactivating_the_current_release_is_a_no_op_and_does_not_erase_the_rollback_target(conn):
    """Otherwise a repeated activate would set previous_release_id to itself and the shard would
    lose the release it could roll back to."""
    v1 = _seed(conn, "t_noop", version=1, families=("F1", "F2"))
    release_store.activate(conn, v1, actor="test")
    v2 = _seed(conn, "t_noop", version=2, families=("F1", "F2", "F3"))
    release_store.activate(conn, v2, actor="test")
    r = release_store.activate(conn, v2, actor="test")
    assert r["activated"][0]["unchanged"] is True
    assert release_store.active(conn, "t_noop")["previous_release_id"] == v1
    assert release_store.rollback(conn, "t_noop", actor="test")["release_id"] == v1


# ------------------------------------------------------------------ the completeness gate
def test_a_release_worse_than_its_predecessor_is_refused_before_it_serves(conn):
    good = _seed(conn, "t_gate", version=1, families=("F1", "F2", "F3"))
    release_store.activate(conn, good, actor="test")
    worse = _seed(conn, "t_gate", version=2, families=("F1",))
    with pytest.raises(release_store.StatsRegression) as exc:
        release_store.activate(conn, worse, actor="test")
    conn.rollback()
    assert exc.value.regressions
    assert any("families" in r["metric"] for r in exc.value.regressions)
    assert release_store.active(conn, "t_gate")["release_id"] == good, \
        "a refused activation must not have moved the switch"


def test_the_gate_can_be_overridden_and_the_override_is_recorded(conn):
    good = _seed(conn, "t_force", version=1, families=("F1", "F2", "F3"))
    release_store.activate(conn, good, actor="test")
    worse = _seed(conn, "t_force", version=2, families=("F1",))
    release_store.activate(conn, worse, actor="test", reason="deliberate shrink", force=True)
    row = release_store.active(conn, "t_force")
    assert row["release_id"] == worse
    assert "[forced]" in row["reason"], "an unrecorded override is one nobody can audit"


def test_the_comparison_ignores_a_metric_with_no_declared_direction():
    """Guessing that up is good for `bytes` would block an activation that made the index
    smaller."""
    prev = {"counts": {"families": 10}, "bytes": {"total": 1000}}
    new = {"counts": {"families": 10}, "bytes": {"total": 10}}
    assert stats_mod.compare(prev, new)["regressions"] == []


def test_the_comparison_names_the_metric_and_both_values(conn):
    prev = {"counts": {"families": 100, "publications": 100, "chunks": 100}}
    new = {"counts": {"families": 50, "publications": 100, "chunks": 100}}
    regs = stats_mod.compare(prev, new)["regressions"]
    assert [r["metric"] for r in regs] == ["counts.families"]
    assert regs[0]["previous"] == 100 and regs[0]["new"] == 50
    assert "families" in regs[0]["detail"]


# ------------------------------------------------------------------ what a shard checks
def test_a_shard_can_prove_it_is_serving_the_release_it_thinks_it_is(conn):
    rid = _seed(conn, "t_verify", version=1, families=("F1", "F2"))
    v = release_store.verify_serving(conn, rid)
    assert v.ok, v.failures


def test_verification_catches_a_content_hash_that_does_not_recompute(conn):
    rid = _seed(conn, "t_tamper", version=1)
    #  Not an UPDATE: the trigger refuses one. The check is on the manifest a shard was handed.
    rel = release_store.get(conn, rid)
    m = dict(rel["manifest"])
    m["counts"] = {**m["counts"], "chunks": m["counts"]["chunks"] + 1}
    assert not manifest_mod.verify(m).ok


def test_the_manifest_records_what_the_database_holds_not_what_was_selected(conn):
    """The defect that made every release ever built fail its own verification.

    The builder recorded `counts.publications` as the number of publications SELECTED, while
    `observed_counts` counts the ones that actually contributed a chunk. A publication whose every
    chunk lacked an embedding is selected and contributes none, so hot_v1 recorded 4,136 and held
    3,909 and `verify hot_v1` exited 1.
    """
    rid = _seed(conn, "t_counts", version=1, families=("F1", "F2"))
    observed = release_store.observed_counts(conn, rid)
    assert release_store.get(conn, rid)["manifest"]["counts"]["publications"] \
        == observed["publications"]
    assert release_store.verify_serving(conn, rid).ok

    #  The old shape: a selection count larger than what was loaded.
    stale = dict(release_store.get(conn, rid)["manifest"])
    stale["counts"] = {**stale["counts"], "publications": observed["publications"] + 227}
    v = manifest_mod.verify(stale, observed_counts=observed)
    assert not v.ok
    assert any(c["check"] == "count:publications" for c in v.failures)


def test_a_demand_publication_is_counted_as_a_publication(conn):
    """Demand chunks have no row in `publications` and carry publication_id = 0, so counting
    distinct ids collapses every demand publication in a release into one."""
    rel = release_store.create(conn, shard_key="t_demand", version=1, kind="hot")
    rid = rel["release_id"]
    release_store.ensure_partition(conn, rid)
    with conn.cursor() as cur:
        for i, pn in enumerate(("US-D1-A", "US-D2-A", "US-D3-A"), start=1):
            cur.execute("""INSERT INTO chunks_release (release_id, chunk_id, family_key,
                               publication_id, publication_number, home_domain, kind, lang, text,
                               token_count, embedding, embed_model, embed_dim, source)
                           VALUES (%s,%s,%s,0,%s,'unclassified','abstract','en',%s,5,%s,
                                   'test-model',768,'demand')""",
                        (rid, i, f"demand:{pn}", pn, f"an abstract {i}", VEC))
    conn.commit()
    assert release_store.observed_counts(conn, rid)["publications"] == 3


def test_a_passing_check_carries_no_failure_text(conn):
    """"ok: true, detail: observed 768 != manifest 768" is a contradiction an operator has to stop
    and parse in the middle of a cutover."""
    rid = _seed(conn, "t_detail", version=1)
    v = release_store.verify_serving(conn, rid)
    assert v.ok
    assert all(c["detail"] == "" for c in v.checks if c["ok"])


def test_verifying_a_release_that_does_not_exist_fails_rather_than_raising(conn):
    v = release_store.verify_serving(conn, "no_such_release_v9")
    assert not v.ok


# ------------------------------------------------------------------ the domain lookup
def test_the_domain_to_release_lookup_prefers_the_active_release(conn):
    v1 = _seed(conn, "t_lookup", version=1, families=("F1", "F2"))
    release_store.activate(conn, v1, actor="test")
    v2 = _seed(conn, "t_lookup", version=2, families=("F1", "F2", "F3"))
    rows = release_store.releases_for_domain(conn, "B65G")
    ids = [r["release_id"] for r in rows]
    assert v1 in ids and v2 in ids
    by_id = {r["release_id"]: r for r in rows}
    assert by_id[v1]["is_active"] and not by_id[v2]["is_active"]
    #  Active first: `route()` must not be handed a superseded release ahead of the live one.
    assert ids.index(v1) < ids.index(v2)
    assert all(r["is_active"] for r in rows[:sum(1 for r in rows if r["is_active"])])


# ------------------------------------------------------------------ the snapshot round trip
def test_a_restored_shard_holds_the_whole_release_and_can_verify_itself(conn, target_dsn, tmp_path):
    """The defect this was written for: `write_snapshot` dumped the chunk partition and nothing
    else, so a restored shard held every chunk, reported ZERO families, could not answer
    `releases_for_domain` (the lookup shard_router.route() output needs) and failed its own
    verification on count:families. Measured on hot_v3: 5,887 chunks restored, 0 of 200 families.
    """
    from corpus import builder

    rid = _seed(conn, "t_snap", version=1, families=("F1", "F2", "F3"), chunks=4)
    snap = builder.write_snapshot(conn, rid, str(tmp_path))
    names = {a["name"] for a in snap["artifacts"]}
    assert {"chunks.dump", "members.copy.gz", "domains.copy.gz"} <= names

    #  Re-seal the manifest with the artifacts, the way build_release does, so the restored copy
    #  checks the files it was actually shipped.
    rel = release_store.get(conn, rid)
    man = manifest_mod.build(release_id=rid, shard_key="t_snap", version=1, kind="domain",
                             domains=["B65G"], counts=release_store.observed_counts(conn, rid),
                             built_from={"chunks_max_id": 1},
                             index_params={"dense": release_store.observed_index_params(conn, rid)},
                             artifacts=snap["artifacts"], stats=rel["stats"], timings={},
                             root=ROOT, note="")
    manifest_mod.write(man, os.path.join(snap["dir"], "manifest.json"))

    target = psycopg.connect(target_dsn, row_factory=dict_row, autocommit=False)
    try:
        r = builder.restore_snapshot(target, rid, str(tmp_path))
        assert r["rows"]["members"] == 3
        assert r["rows"]["domains"] == 1
        assert release_store.observed_counts(target, rid) == \
            release_store.observed_counts(conn, rid)
        assert [x["release_id"] for x in release_store.releases_for_domain(target, "B65G")] == [rid]
        v = release_store.verify_serving(target, rid, artifact_root=snap["dir"])
        assert v.ok, v.failures

        #  Repeatable. pg_restore --data-only appends, so a second run over a populated partition
        #  is a primary key violation or, worse, a doubled release.
        builder.restore_snapshot(target, rid, str(tmp_path))
        assert release_store.observed_counts(target, rid)["chunks"] == 12
    finally:
        target.close()


def test_a_tampered_snapshot_is_refused_before_it_is_restored(conn, target_dsn, tmp_path):
    from corpus import builder

    rid = _seed(conn, "t_tamper_snap", version=1, families=("F1",))
    snap = builder.write_snapshot(conn, rid, str(tmp_path))
    man = manifest_mod.build(release_id=rid, shard_key="t_tamper_snap", version=1, kind="domain",
                             domains=["B65G"], counts=release_store.observed_counts(conn, rid),
                             built_from={}, index_params={}, artifacts=snap["artifacts"],
                             stats={}, timings={}, root=ROOT, note="")
    manifest_mod.write(man, os.path.join(snap["dir"], "manifest.json"))
    with open(os.path.join(snap["dir"], "members.copy.gz"), "ab") as fh:
        fh.write(b"\x00")

    target = psycopg.connect(target_dsn, row_factory=dict_row, autocommit=False)
    try:
        with pytest.raises(builder.BuildError) as exc:
            builder.restore_snapshot(target, rid, str(tmp_path))
        assert "does not verify" in str(exc.value)
    finally:
        target.close()
