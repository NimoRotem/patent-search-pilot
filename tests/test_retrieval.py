"""Retrieval fusion tests — LOCK IN the M3 weighted-RRF + dense-floor fix as a regression.
Pure logic (no DB/embed): synthetic per-channel rankings fed straight into Retriever.rrf()."""
from retrieval import Retriever, CHANNEL_WEIGHTS, RRF_K, DENSE_FLOOR


def _pos(fused, pid):
    for i, (p, _s, _pr) in enumerate(fused):
        if p == pid:
            return i + 1
    return None


def _channels():
    # G = strong #1 dense hit, absent from the broad/noisy channels.
    # N = mediocre doc appearing in bm25 rank ~31 and cpc rank ~41 (two weak channels).
    # M = dense rank 5 + citation rank 3 + crosslingual rank 2 (multi-strong-channel agreement).
    dense = [("G", 0.9)] + [(f"d{i}", 0.5) for i in range(1, 60)]
    dense[4] = ("M", 0.6)
    bm25 = [(f"b{i}", 1) for i in range(30)] + [("N", 1)] + [(f"b{i}", 1) for i in range(30, 100)]
    cpc = [(f"c{i}", 1) for i in range(40)] + [("N", 1)] + [(f"c{i}", 1) for i in range(40, 100)]
    citation = [("z1", 3), ("z2", 3), ("M", 3)] + [(f"e{i}", 1) for i in range(50)]
    crossl = [("y1", 1), ("M", 1)] + [(f"f{i}", 1) for i in range(50)]
    return {"dense": dense, "bm25": bm25, "cpc": cpc, "citation": citation, "crosslingual": crossl}


def test_weighted_rrf_protects_strong_dense_hit():
    """THE regression: a strong #1 dense hit must not be demoted below the head by a mediocre doc
    that merely appears in two broad/noisy channels (bm25+cpc)."""
    fused = Retriever.rrf(_channels(), weighted=True)
    g, n = _pos(fused, "G"), _pos(fused, "N")
    assert g is not None and n is not None
    assert g < n, f"strong dense hit G(#{g}) must rank above noise N(#{n})"
    assert g <= 3, f"strong dense hit should be near the head, got #{g}"
    assert n > 50, f"noise doc in two weak channels should sink, got #{n}"


def test_unweighted_rrf_would_demote_the_dense_hit():
    """Documents WHY the fix matters: with plain (unweighted) RRF the noise out-ranks the dense hit."""
    fused = Retriever.rrf(_channels(), weighted=False)
    assert _pos(fused, "N") < _pos(fused, "G"), "unweighted RRF should (wrongly) rank noise above dense"


def test_multichannel_agreement_beats_pure_dense():
    """The agent's lift: a doc found by dense+citation+crosslingual ranks above a pure-dense hit."""
    fused = Retriever.rrf(_channels(), weighted=True)
    assert _pos(fused, "M") <= _pos(fused, "G"), "multi-strong-channel agreement should rank >= top dense"


def test_dense_floor_guarantees_top_hits_survive():
    """A top-DENSE_FLOOR dense hit gets a floor so weak channels can't push it out of the head."""
    # dense hit at rank ~DENSE_FLOOR-1, present in NO other channel
    dense = [(f"x{i}", 0.9 - i * 0.001) for i in range(DENSE_FLOOR)]
    target = dense[DENSE_FLOOR - 2][0]
    noise = {"bm25": [(f"n{i}", 1) for i in range(200)]}
    fused = Retriever.rrf({"dense": dense, **noise}, weighted=True)
    floor = CHANNEL_WEIGHTS["dense"] / (RRF_K + DENSE_FLOOR)
    score = dict((p, s) for p, s, _ in fused)[target]
    assert score >= floor - 1e-9, "top dense hit should get at least the dense floor"


def test_channel_weights_are_dense_dominant():
    assert CHANNEL_WEIGHTS["dense"] == max(CHANNEL_WEIGHTS.values())
    assert CHANNEL_WEIGHTS["dense"] > CHANNEL_WEIGHTS["bm25"] > 0
    assert CHANNEL_WEIGHTS["dense"] > CHANNEL_WEIGHTS["cpc"] > 0
    assert CHANNEL_WEIGHTS["cpc"] < 0.3, "CPC is a broad prior, must be a low weight"


def test_family_dedup_keeps_best_per_family():
    """dedup_family keeps the first (highest-fused) publication per family, drops the rest."""
    R = Retriever.__new__(Retriever)          # no DB connection needed for this unit
    R._fam = {1: "famA", 2: "famA", 3: "famB"}
    ranked = [(1, 0.9, {}), (2, 0.5, {}), (3, 0.4, {})]
    out = R.dedup_family(ranked)
    fams = [fk for fk, _pid, _s, _pr in out]
    assert fams == ["famA", "famB"], "one row per family, best-first"
    assert out[0][1] == 1, "kept the higher-scored member of famA"


def test_fork_gets_own_connection_without_reloading_family_map(monkeypatch):
    """Concurrent agent workers share only immutable map data, never a psycopg connection."""
    import retrieval

    made = []

    class FakeConnection:
        def __init__(self):
            self.autocommit = False
            self.statements = []
            self.closed = False

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            self.statements.append(sql)

        def fetchall(self):
            return []

        def close(self):
            self.closed = True

    def connect():
        conn = FakeConnection()
        made.append(conn)
        return conn

    monkeypatch.setattr(retrieval.db, "connect", connect)
    parent = Retriever.__new__(Retriever)
    parent._fam = {1: "family-one"}
    child = parent.fork()

    assert len(made) == 1
    assert child.conn is made[0]
    assert child._fam is parent._fam
    assert not any("SELECT id" in sql for sql in child.conn.statements)
    child.close()
    assert child.conn.closed is True
