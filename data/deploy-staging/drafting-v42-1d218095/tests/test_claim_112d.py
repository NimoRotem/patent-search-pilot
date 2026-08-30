"""A dependent claim carries its parent's limitations. 35 U.S.C. 112(d).

Reported by counsel on 2026-08-20 after reading two real reports: the ledger marked the independent
claim PARTIAL while stamping ten dependent claims ANTICIPATED in one report and four in the other.
That is not unlikely, it is impossible — nothing anticipates claim 3 unless it also anticipates
claim 1 — and it came from computing anticipation over the limitations a claim ADDS.

These drive the real Ledger over real-shaped rows. Reverting `claim_detail` to the old
own-limitations-only query fails every one of them.
"""
import limitations


def _lims(spec):
    """spec: {claim_no: (independent, depends_on, n_limitations)} -> ledger limitations."""
    out = []
    for no, (indep, dep, n) in sorted(spec.items()):
        for j in range(n):
            out.append({"id": "claim %d[%s]" % (no, chr(97 + j)), "claim_label": "claim %d" % no,
                        "claim_no": no, "index": j, "text": "requirement %d.%d" % (no, j),
                        "independent": indep, "depends_on": dep, "source": "model"})
    return out


def _fill(led, pub, lim_ids, verdict="disclosed"):
    for lid in lim_ids:
        led.add(lid, pub, verdict, quote="a passage", location="para 1", bar="discloses")


def test_a_dependent_claim_is_not_anticipated_while_its_parent_is_not():
    """THE REPORTED BUG, in its exact shape: claim 1 partial, one document covering all of what
    claim 3 adds. The old code called that anticipation of claim 3."""
    led = limitations.Ledger(_lims({1: (True, None, 3), 3: (False, 1, 1)}), cover_min=1)
    #  D discloses everything claim 3 ADDS, and only one of claim 1's three requirements.
    _fill(led, "D", ["claim 3[a]", "claim 1[a]"])
    d = led.claim_detail("claim 3")
    assert d["anticipated_by"] == [], "claim 3 anticipated while claim 1 is not"
    assert d["adds_disclosed_by"] == ["D"], "the real finding was thrown away instead of relabelled"
    assert d["status"] == "partial"
    assert led.summary()["anticipated"] == []


def test_a_dependent_claim_IS_anticipated_when_one_document_meets_the_whole_chain():
    """The fix must not simply suppress anticipation — a real §102 kill still has to land."""
    led = limitations.Ledger(_lims({1: (True, None, 2), 3: (False, 1, 1)}), cover_min=1)
    _fill(led, "D", ["claim 1[a]", "claim 1[b]", "claim 3[a]"])
    assert led.claim_detail("claim 3")["anticipated_by"] == ["D"]
    assert led.claim_detail("claim 1")["anticipated_by"] == ["D"]
    assert led.summary()["anticipated"] == ["claim 1", "claim 3"]


def test_the_whole_ancestry_is_walked_not_just_one_hop():
    """claim 14 < claim 5 < claim 3 < claim 1 is a real chain out of adhoc-44d103bc4e6e."""
    led = limitations.Ledger(
        _lims({1: (True, None, 1), 3: (False, 1, 1), 5: (False, 3, 1), 14: (False, 5, 1)}),
        cover_min=1)
    #  Everything except claim 1's requirement: one hop of checking would pass this.
    _fill(led, "D", ["claim 3[a]", "claim 5[a]", "claim 14[a]"])
    assert led.chains["claim 14"]["chain"] == ["claim 14", "claim 5", "claim 3", "claim 1"]
    assert led.claim_detail("claim 14")["anticipated_by"] == []
    _fill(led, "D", ["claim 1[a]"])
    assert led.claim_detail("claim 14")["anticipated_by"] == ["D"]


def test_two_documents_between_them_are_not_an_anticipation():
    """§102 needs ONE document. This is the rule the ledger already had; it must survive."""
    led = limitations.Ledger(_lims({1: (True, None, 2), 2: (False, 1, 1)}), cover_min=1)
    _fill(led, "A", ["claim 1[a]", "claim 2[a]"])
    _fill(led, "B", ["claim 1[b]"])
    assert led.claim_detail("claim 2")["anticipated_by"] == []
    assert led.claim_detail("claim 2")["status"] in ("partial", "covered")


def test_a_teaches_cell_never_anticipates():
    led = limitations.Ledger(_lims({1: (True, None, 1), 2: (False, 1, 1)}), cover_min=1)
    led.add("claim 1[a]", "D", "partial", quote="", location="", bar="teaches")
    led.add("claim 2[a]", "D", "disclosed", quote="q", location="p", bar="discloses")
    assert led.claim_detail("claim 2")["anticipated_by"] == []


def test_a_parent_the_ledger_does_not_hold_withholds_anticipation():
    """A claim that names a parent we cannot see is a claim whose requirements we cannot check.
    The honest answer is no anticipation plus a flag, not a guess in either direction."""
    led = limitations.Ledger(_lims({7: (False, 6, 1)}), cover_min=1)      # claim 6 is absent
    _fill(led, "D", ["claim 7[a]"])
    d = led.claim_detail("claim 7")
    assert d["chain_complete"] is False
    assert d["anticipated_by"] == []
    assert d["adds_disclosed_by"] == ["D"]


def test_a_dependency_loop_does_not_hang():
    lims = _lims({4: (False, 4, 1)})                      # claim 4 of claim 4
    led = limitations.Ledger(lims, cover_min=1)
    _fill(led, "D", ["claim 4[a]"])
    assert led.claim_detail("claim 4")["chain"] == ["claim 4"]


def test_a_dependent_claim_is_not_covered_while_its_parent_is_not():
    """Same rule, weaker verdict: claim 19 of adhoc-0a80ecb18aa6 read `covered` under a `partial`
    claim 1."""
    led = limitations.Ledger(_lims({1: (True, None, 2), 19: (False, 1, 1)}), cover_min=1)
    _fill(led, "A", ["claim 19[a]", "claim 1[a]"])
    assert led.claim_detail("claim 19")["status"] == "partial"


def test_the_summary_carries_the_chain_for_the_page():
    led = limitations.Ledger(_lims({1: (True, None, 1), 2: (False, 1, 1)}), cover_min=1)
    m = led.summary()["claims"]["claim 2"]
    assert m["chain"] == ["claim 2", "claim 1"]
    assert m["depends_on_label"] == "claim 1"
    assert m["chain_complete"] is True


def test_an_old_stored_report_is_re_read_under_the_rule():
    """`from_stored` is what stops 660 reports on disk publishing a withdrawn §102 assertion."""
    led = limitations.Ledger(_lims({1: (True, None, 2), 3: (False, 1, 1)}), cover_min=1)
    _fill(led, "D", ["claim 3[a]", "claim 1[a]"])
    stored = led.to_dict()
    #  Forge the pre-fix summary the way every report on disk carries it.
    stored["summary"]["claims"]["claim 3"] = {"status": "anticipated", "anticipated_by": ["D"]}
    again = limitations.Ledger.from_stored(stored)
    assert again.claim_detail("claim 3")["anticipated_by"] == []
    assert again.claim_detail("claim 3")["adds_disclosed_by"] == ["D"]
