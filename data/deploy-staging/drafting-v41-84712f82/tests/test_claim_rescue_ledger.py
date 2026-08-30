"""What the rescue goes back for, when a ledger is what decides.

The ledger tracks LIMITATIONS and gives each of them one of three states: `covered` means
`cover_min` documents disclose it, `partial` means evidence exists but fewer than that do, and
`uncovered` means nothing at all. A live run on US 2026/0109053 A1 produced 10 covered, 15 partial
and 2 with nothing — and the rescue searched for the 2. The KPI this ledger reports is the count of
limitations with TWO grounded disclosures, so the 15 are most of the work, and they are where a
patent attorney's references sit: each of those references was filed for one requirement that the
search had touched and not proven.
"""
import limitations as LIM
import claim_rescue as CR


def _lims(n, independent_first=True):
    return [{"id": f"claim 1[{chr(97 + i)}]", "claim_label": "claim 1", "claim_no": 1,
             "index": i, "text": f"a requirement number {i} stated at searchable length",
             "independent": independent_first and i == 0, "depends_on": None,
             "source": "model"} for i in range(n)]


def _ledger(n_covered, n_partial, n_empty, cover_min=2):
    lims = _lims(n_covered + n_partial + n_empty)
    led = LIM.Ledger(lims, cover_min=cover_min)
    i = 0
    for _ in range(n_covered):
        for k in range(cover_min):
            led.add(lims[i]["id"], f"COV-{i}-{k}", "disclosed", "q", "claim 1", "1990-01-01", 0.9)
        i += 1
    for _ in range(n_partial):
        led.add(lims[i]["id"], f"PAR-{i}", "partial", "q", "claim 1", "1990-01-01", 0.5)
        i += 1
    i += n_empty                                    # the rest keep no evidence at all
    return led, lims


def _run(monkeypatch, ledger, lims, max_claims=10):
    """Drive run() far enough to see what it selected, with every search stubbed out."""
    monkeypatch.setattr(CR, "MAX_CLAIMS", max_claims)
    monkeypatch.setattr(CR, "plan", lambda claims, **k: {
        c["label"]: {"queries": ["q"], "hint": "an idea"} for c in claims})
    monkeypatch.setattr(CR, "find_candidates", lambda *a, **k: [])
    import claim_acquire
    for name in ("by_limitation", "by_worldset", "by_concept"):
        monkeypatch.setattr(claim_acquire, name, lambda *a, **k: {"candidates": [], "error": "x"})
    monkeypatch.setattr(claim_acquire, "by_citation", lambda *a, **k: {"candidates": []})
    items = [{"label": l["id"], "claim_no": 1, "text": l["text"],
              "independent": l["independent"]} for l in lims]
    _new, summary = CR.run([], items, ["a feature"], {}, subject=None, mode="novelty",
                           retriever=None, brief="b", title="t", ledger=ledger)
    return summary


def test_thin_limitations_are_searched_for_too(monkeypatch):
    """THE DEFECT. `rows = uncovered(False)` with an `if not rows` fallback to `uncovered(True)`
    is an either/or: with 2 empty limitations it searched for exactly those 2 and never for the 15
    that had one partial each — which are the ones an attorney's reference answers."""
    led, lims = _ledger(n_covered=10, n_partial=15, n_empty=2)
    assert led.summary()["counts"] == {"covered": 10, "partial": 15, "uncovered": 2}
    got = _run(monkeypatch, led, lims)
    assert len(got["orphans"]) == 10, got["orphans"]        # MAX_CLAIMS, not 2


def test_the_empty_ones_come_first(monkeypatch):
    """An independent claim's limitation with nothing against it is the most serious hole a search
    can leave, so the budget must reach it before it reaches a thin one."""
    led, lims = _ledger(n_covered=0, n_partial=6, n_empty=3)
    empty = {l["id"] for l in lims if led.status(l["id"]) == "uncovered"}
    got = _run(monkeypatch, led, lims, max_claims=5)
    assert set(got["orphans"][:3]) == empty, got["orphans"]


def test_a_covered_limitation_is_never_searched_for(monkeypatch):
    led, lims = _ledger(n_covered=4, n_partial=0, n_empty=1)
    got = _run(monkeypatch, led, lims)
    covered = {l["id"] for l in lims if led.status(l["id"]) == "covered"}
    assert not (set(got["orphans"]) & covered), got["orphans"]


def test_nothing_left_means_nothing_run(monkeypatch):
    led, lims = _ledger(n_covered=5, n_partial=0, n_empty=0)
    got = _run(monkeypatch, led, lims)
    assert got["ran"] is False and not got["orphans"]


def test_partial_survives_the_post_reread_filter(monkeypatch):
    """After the cheap re-read the rescue re-decides what still needs searching. Asking
    `orphans()` there counts two PARTIAL matches as answered and drops the limitation, silently
    undoing the selection above — the ledger has to be what is asked."""
    led, lims = _ledger(n_covered=0, n_partial=1, n_empty=0)
    lim_id = lims[0]["id"]
    led.add(lim_id, "SECOND", "partial", "q", "claim 1", "1990-01-01", 0.5)
    assert led.status(lim_id) == "partial"
    #  Two partial matches: `claim_matches` counts both, so orphans(max_matches=1) excludes it.
    charts = [{"pub": p, "method": "llm",
               "claims": [{"item": lim_id, "verdict": "partial", "grounding": "verified",
                           "quote": "q", "location": "claim 1"}]}
              for p in ("PAR-0", "SECOND")]
    still, _ = CR.orphans(charts, [{"label": lim_id, "text": "t", "independent": True}])
    assert not still, "precondition: orphans() considers this answered"

    monkeypatch.setattr(CR, "REREAD_TOP", 0)        # skip the re-read itself
    got = _run(monkeypatch, led, lims)
    assert got["orphans"] == [lim_id]
    #  It reached the search stage rather than returning "no extra search needed".
    assert got.get("candidates") == 0 or "local_candidates" in got, got
