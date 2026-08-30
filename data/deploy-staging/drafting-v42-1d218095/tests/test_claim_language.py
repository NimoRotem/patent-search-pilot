"""Claim dependency is not an English-only fact.

DE 10 2024 133 318 A1, 2026-08-20: eleven of its fourteen claims came back marked INDEPENDENT.
They are not. German drafters put words between the preposition and the noun ("nach dem vorherigen
Anspruch") and the commonest back-references carry no claim number at all. Both matter downstream:
the ledger gives independent-claim limitations first call on the search budget, and the 112(d) rule
walks `depends_on` — with no dependency recorded, `claim_chains` has nothing to walk and
anticipation is withheld across the whole document.
"""
import limitations
import patent_doc


# --------------------------------------------------------------------------- is it dependent


DEPENDENT = [
    "Schlauchheber (10) nach Anspruch 1, wobei der Ventilkörper (64) beaufschlagt ist",
    "Schlauchheber (10) nach dem vorherigen Anspruch, wobei der Grenz-Druckunterschied",
    "Vorrichtung nach einem der vorhergehenden Ansprüche, dadurch gekennzeichnet",
    "Verfahren gemäß Anspruch 3, bei dem die Saugstelle",
    "Vorrichtung nach einem der Ansprüche 1 bis 5",
    "The gripper of claim 1, wherein the seal is annular",
    "A device according to any one of the preceding claims",
    "Dispositif selon la revendication 2, caractérisé en ce que",
    "Dispositif selon la revendication précédente",
]
INDEPENDENT = [
    "Schlauchheber (10), umfassend: einen Hubschlauch (12), welcher einen Schlauchinnenraum "
    "(14) aufweist",
    "Bedienvorrichtung (20) für einen Schlauchheber (10), umfassend einen Hubschlauchanschluss",
    "A vacuum gripper for gripping an object, the vacuum gripper comprising a base element",
    "Verfahren zum Betreiben eines Schlauchhebers, mit den Schritten",
]


def test_a_german_dependent_claim_is_not_called_independent():
    for t in DEPENDENT:
        assert not patent_doc.is_independent(t), t


def test_a_real_independent_claim_still_reads_independent():
    for t in INDEPENDENT:
        assert patent_doc.is_independent(t), t


# --------------------------------------------------------------------------- which claim


def test_the_parent_number_is_read_in_every_language():
    assert limitations.parent_of("Schlauchheber nach Anspruch 1, wobei") == 1
    assert limitations.parent_of("Vorrichtung gemäß Anspruch 7, dadurch") == 7
    assert limitations.parent_of("Vorrichtung nach einem der Ansprüche 3 bis 9") == 3
    assert limitations.parent_of("The gripper of claim 4, wherein") == 4
    assert limitations.parent_of("Dispositif selon la revendication 2") == 2


def test_a_numberless_back_reference_resolves_to_the_claim_before():
    """"nach dem vorherigen Anspruch" is the commonest German form and carries no number, so it
    can only be resolved by knowing which claim is being read."""
    t = "Schlauchheber (10) nach dem vorherigen Anspruch, wobei der Grenz-Druckunterschied"
    assert limitations.parent_of(t, claim_no=3) == 2
    assert limitations.parent_of(t, claim_no=1) is None       # nothing before claim 1
    assert limitations.parent_of(t) is None                   # not knowable without the number
    assert limitations.parent_of("A device according to the preceding claim", claim_no=5) == 4


def test_an_independent_claim_has_no_parent():
    for t in INDEPENDENT:
        assert limitations.parent_of(t, claim_no=1) is None, t


def test_the_chain_is_walkable_for_a_german_claim_set():
    """End to end: the 112(d) rule can only work if the dependency survives the split."""
    claims = [
        {"label": "claim 1", "claim_no": 1, "independent": True,
         "text": "Schlauchheber (10), umfassend einen Hubschlauch (12) mit einem "
                 "Schlauchinnenraum (14), welcher durch Unterdruck verkuerzbar ist"},
        {"label": "claim 2", "claim_no": 2, "independent": False,
         "text": "Schlauchheber (10) nach Anspruch 1, wobei der Ventilkoerper (64) mittels einer "
                 "Federeinrichtung (72) beaufschlagt ist"},
        {"label": "claim 3", "claim_no": 3, "independent": False,
         "text": "Schlauchheber (10) nach dem vorherigen Anspruch, wobei der Grenz-"
                 "Druckunterschied derart gewaehlt ist, dass er ueberschritten wird"},
    ]
    lims = limitations.split_claims(claims, use_llm=False)
    dep = {}
    for l in lims:
        dep.setdefault(l["claim_label"], l.get("depends_on"))
    assert dep["claim 1"] is None
    assert dep["claim 2"] == 1
    assert dep["claim 3"] == 2, dep
    chains = limitations.claim_chains(lims)
    assert chains["claim 3"]["chain"] == ["claim 3", "claim 2", "claim 1"]
    assert chains["claim 3"]["complete"] is True
