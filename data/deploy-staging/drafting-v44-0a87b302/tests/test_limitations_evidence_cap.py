"""The stored ledger must not silently drop the evidence an argument is built from.

MEASURED on adhoc-a2fec8ee8ba2, a real subject with 20 claims and 68 limitations: the chart held
10,880 verified limitation cells and the report stored 544 — five per cent — with all 68 of 68
limitations sitting at exactly the old cap of 8. Every limitation at exactly the cap is the tell
for a truncation rather than a finding.

It is not a cosmetic loss. The cut is by verdict strength then model confidence, so a `partial`
from the reference a patent attorney would actually cite loses its place to eight confident
`disclosed` rows from documents nobody will file.
"""
import limitations as LIM


def _ledger(n_evidence):
    lims = [{"id": "claim 1[a]", "claim_label": "claim 1", "claim_no": 1, "index": 0,
             "text": "a requirement stated at searchable length", "independent": True,
             "depends_on": None, "source": "model"}]
    led = LIM.Ledger(lims)
    for i in range(n_evidence):
        led.add("claim 1[a]", f"US-{1000000 + i}-A", "partial", "q", "claim 1",
                "1990-01-01", 0.9 - i * 0.001)
    return led


def test_the_true_count_is_recorded_even_when_the_list_is_cut(monkeypatch):
    monkeypatch.setattr(LIM, "EVIDENCE_KEEP", 5)
    d = _ledger(60).to_dict()
    row = d["limitations"][0]
    assert row["n_evidence"] == 60, row["n_evidence"]
    assert len(row["evidence"]) == 5


def test_nothing_is_cut_when_it_fits(monkeypatch):
    monkeypatch.setattr(LIM, "EVIDENCE_KEEP", 40)
    d = _ledger(12).to_dict()
    row = d["limitations"][0]
    assert row["n_evidence"] == 12 and len(row["evidence"]) == 12


def test_the_default_keeps_more_than_the_old_eight():
    """8 is roughly the number of documents a model is most confident about, not the number an
    argument needs."""
    assert LIM.EVIDENCE_KEEP >= 40
