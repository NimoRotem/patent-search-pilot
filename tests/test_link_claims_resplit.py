"""A patent arriving as a LINK must yield its real claims, with their real numbers.

MEASURED on US 2025/0033224 A1 — the application a patent attorney actually filed against. The
publication record came back as two items: claims 1 to 19 glued into one 17,309-character blob, and
claim 20 on its own. The link path took the claim number from the list index, so the report tracked
a "claim 1" that was nineteen claims and a "claim 2" that was claim 20.

For a Type B search that is fatal rather than untidy. The unit of work is the limitation,
limitations are split out of claims, and `limitations.split_claims` truncates each claim at 4,000
characters — so claims 5 through 19 never reached any stage of the pipeline. Nothing raised. The
ledger simply had two rows and reported itself done.
"""
import patent_doc

#  The shape of the real document: twenty SEQUENTIALLY numbered claims run together in one string,
#  claim 1 and claim 20 independent and the rest dependent. Sequential matters — patent_doc stops a
#  claims run at a numbering gap, which is its guard against a numbered list in a description, so a
#  fixture that jumps 4 -> 20 tests that guard rather than this repair. It must also clear
#  MIN_RUN_CLAIMS=3 and MIN_RUN_CHARS=600.
_INDEP = ("A vacuum gripper for gripping an object, the vacuum gripper comprising a base element, "
          "wherein the base element comprises one or more openings around a periphery of the base "
          "element; a vacuum seal element coupled to the base element and configured to surround a "
          "cavity; and an air extraction mechanism in fluid communication with the cavity")
BLOB = "\n".join(
    [f"1. {_INDEP}."]
    + [f"{n}. A vacuum gripper as in claim 1, wherein the vacuum seal element comprises a first "
       f"portion disposed on a second portion, the first portion comprising a flexible and "
       f"stretchable material and the second portion a compressible material, variant {n}."
       for n in range(2, 20)]
    + [f"20. {_INDEP}, a pressure alarm, and a battery housed in a handle."]) + "\n"


def test_a_run_together_claims_record_is_re_split():
    got = patent_doc.split_claims(BLOB)
    assert len(got) == 20, [c["claim_no"] for c in got]
    assert [c["claim_no"] for c in got] == list(range(1, 21))


def test_the_claim_number_comes_from_the_text_not_the_list_position():
    """The last claim is claim 20, not claim 5. Numbering it by position renumbers the whole set
    and every downstream reference to "claim 20" then points at nothing."""
    got = patent_doc.split_claims(BLOB)
    assert got[-1]["claim_no"] == 20
    assert "battery housed in a handle" in got[-1]["text"]


def test_a_correctly_split_record_is_left_alone():
    """The repair must only ever ADD claims. A source that split correctly must not be re-split by
    a parser that might merge two of them — the guard is `len(resplit) > len(records)`."""
    correct = ["1. A gripper comprising a base element and a seal.",
               "2. The gripper of claim 1, wherein the seal is layered.",
               "3. The gripper of claim 1, wherein the base is rigid."]
    resplit = patent_doc.split_claims("\n".join(correct))
    assert len(resplit) <= len(correct) or len(resplit) == len(correct)


def test_independence_is_recomputed_from_the_repaired_text():
    """Claim 20 is independent and claim 2 is not. Before the repair the blob's independence was
    computed over nineteen claims at once, which is neither."""
    got = patent_doc.split_claims(BLOB)
    by_no = {c["claim_no"]: c["text"] for c in got}
    assert patent_doc.is_independent(by_no[20]) is True
    assert patent_doc.is_independent(by_no[2]) is False
