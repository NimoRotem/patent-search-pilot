"""Reading a draft: sections, claims, the registry, and the normalising that makes them work."""
from __future__ import annotations

import pytest

from fm import claims as claims_mod, ingest, registry as registry_mod, sections

DRAFT = """
TITLE
A gripping apparatus

FIELD OF THE INVENTION

The invention relates to vacuum gripping.

BRIEF DESCRIPTION OF THE DRAWINGS

FIG. 1 is a block diagram of a gripping apparatus according to one embodiment;
FIG. 2 is a perspective view of the apparatus of FIG. 1; and
FIGS. 3A-3C are cross-sectional views of the seal taken along line A-A.

DETAILED DESCRIPTION

Referring to FIG. 1, a gripping apparatus 100 includes a housing 102 carrying a vacuum pump 104
and a controller 106. A pressure sensor 108 reports to the controller 106.

Referring now to FIG. 2, the housing 102 supports first and second arms 112, 114. A drive shaft
116 extends between them. The apparatus also has a lanyard, which is not numbered.

FIGS. 3A-3C show the seal 120 in three states. The seal 120 seats against the rim 122.

What is claimed is:

1. A gripping apparatus comprising: a housing; a vacuum pump carried by the housing; a controller
coupled to the vacuum pump; and a pressure sensor configured to report a pressure to the
controller.

2. The apparatus of claim 1, wherein the housing is aluminium.

3. A method of gripping, comprising: evacuating a chamber; and comparing a pressure to a
threshold.
"""


@pytest.fixture(scope="module")
def parsed():
    return sections.analyse(DRAFT, title="A gripping apparatus")


# ------------------------------------------------------------------------------ figure labels


def test_a_plain_reference():
    assert sections.figure_labels_in("as shown in FIG. 1") == ["FIG. 1"]


def test_a_lettered_reference():
    assert sections.figure_labels_in("see FIG. 2A") == ["FIG. 2A"]


def test_a_lettered_range_expands():
    assert sections.figure_labels_in("FIGS. 3A-3C") == ["FIG. 3A", "FIG. 3B", "FIG. 3C"]


def test_a_numeric_range_expands():
    assert sections.figure_labels_in("FIGS. 4 to 7") == ["FIG. 4", "FIG. 5", "FIG. 6", "FIG. 7"]


def test_a_list_does_not_expand():
    assert sections.figure_labels_in("FIGS. 1 and 5") == ["FIG. 1", "FIG. 5"]


def test_figures_sort_by_number_then_letter():
    labels = ["FIG. 10", "FIG. 2A", "FIG. 2", "FIG. 1"]
    assert sorted(labels, key=sections.figure_sort_key) == \
        ["FIG. 1", "FIG. 2", "FIG. 2A", "FIG. 10"]


# ---------------------------------------------------------------------------------- sections


def test_the_headings_are_found(parsed):
    assert parsed.brief.strip()
    assert parsed.detailed.strip()
    assert parsed.claims_text.strip()


def test_a_heading_word_in_a_sentence_is_not_a_heading():
    """Matching "summary" mid-sentence silently truncates the description."""
    body = DRAFT.replace("The invention relates to vacuum gripping.",
                         "As stated in the summary above, the invention relates to gripping.")
    spans = sections.split_sections(body)
    assert "summary" not in spans


def test_the_brief_description_is_parsed(parsed):
    labels = [item.label for item in parsed.brief_items]
    assert labels == ["FIG. 1", "FIG. 2", "FIG. 3A", "FIG. 3B", "FIG. 3C"]


def test_the_brief_description_says_what_each_view_is(parsed):
    kinds = {item.label: item.kind_hint for item in parsed.brief_items}
    assert kinds["FIG. 1"] == "block_diagram"
    assert kinds["FIG. 2"] == "perspective"
    assert kinds["FIG. 3A"] == "cross_section"


# ------------------------------------------------------------------------------------ claims


def test_claims_are_numbered_and_split(parsed):
    assert [c.number for c in parsed.claims] == [1, 2, 3]


def test_dependency_is_read(parsed):
    by_number = {c.number: c for c in parsed.claims}
    assert by_number[1].independent
    assert not by_number[2].independent
    assert by_number[2].depends_on == 1
    assert by_number[3].independent


def test_claim_elements_are_split(parsed):
    claim = next(c for c in parsed.claims if c.number == 1)
    elements = claims_mod.split_elements(claim)
    terms = {e.term for e in elements}
    assert "housing" in terms
    assert "vacuum pump" in terms
    assert len(elements) >= 4


# ---------------------------------------------------------------------------------- registry


@pytest.fixture(scope="module")
def registry(parsed):
    return registry_mod.build(parsed, use_model=False)


def test_numerals_are_found_with_their_terms(registry):
    terms = {e.numeral: e.term for e in registry.entries}
    assert terms["102"] == "housing"
    assert terms["104"] == "vacuum pump"
    assert terms["108"] == "pressure sensor"


def test_a_run_of_numerals_shares_the_phrase(registry):
    """"first and second arms 112, 114" gives 114 the term as well as 112."""
    terms = {e.numeral: e.term for e in registry.entries}
    assert "112" in terms and "114" in terms
    assert "arm" in terms["114"]


def test_numerals_are_tied_to_the_figures_that_discuss_them(registry):
    by_numeral = registry.by_numeral()
    assert "FIG. 1" in by_numeral["104"].figures
    assert "FIG. 2" in by_numeral["116"].figures
    assert "FIG. 3A" in by_numeral["120"].figures


def test_a_claim_number_is_not_a_reference_character(registry):
    assert "1" not in registry.by_numeral()
    assert "2" not in registry.by_numeral()


def test_a_measurement_is_not_a_reference_character():
    body = DRAFT.replace("a housing 102", "a housing 102 that is 250 mm wide and 40 mm deep")
    parsed = sections.analyse(body)
    found = registry_mod.build(parsed, use_model=False).by_numeral()
    assert "250" not in found
    assert "40" not in found


def test_a_figure_number_is_not_a_reference_character(registry):
    assert "3" not in registry.by_numeral()


# ---------------------------------------------------------------------------------- conflicts


def test_one_numeral_two_parts_is_an_error():
    body = DRAFT + "\n\nThe apparatus further includes a gearbox 102 driven by the motor.\n"
    parsed = sections.analyse(body)
    found = registry_mod.build(parsed, use_model=False)
    codes = {c.code for c in found.conflicts}
    assert "numeral_two_terms" in codes
    hit = next(c for c in found.conflicts if c.code == "numeral_two_terms")
    assert hit.severity == "error"
    assert hit.cite == "37 CFR 1.84(p)(5)"


def test_one_part_two_numerals_is_reported():
    body = DRAFT + "\n\nIn another embodiment the housing 202 is moulded.\n"
    parsed = sections.analyse(body)
    found = registry_mod.build(parsed, use_model=False)
    assert "term_two_numerals" in {c.code for c in found.conflicts}


def test_a_numeral_tied_to_no_figure_is_reported():
    body = DRAFT.replace("Referring to FIG. 1, a gripping", "A gripping")
    body = body.replace("Referring now to FIG. 2, the", "The")
    parsed = sections.analyse(body)
    found = registry_mod.build(parsed, use_model=False)
    assert "numeral_no_figure" in {c.code for c in found.conflicts}


def test_a_promised_figure_nobody_discusses_is_reported():
    body = DRAFT.replace("FIGS. 3A-3C show the seal 120 in three states. "
                         "The seal 120 seats against the rim 122.",
                         "The seal seats against a rim.")
    parsed = sections.analyse(body)
    found = registry_mod.build(parsed, use_model=False)
    assert "figure_never_discussed" in {c.code for c in found.conflicts}


def test_a_clean_draft_raises_no_error_conflicts(registry):
    assert not [c for c in registry.conflicts if c.severity == "error"]


def test_the_next_free_numeral_avoids_what_is_used(registry):
    value = registry_mod.next_free_numeral(registry.entries)
    assert str(value) not in registry.by_numeral()
    assert value > 122


# --------------------------------------------------------------------------- claims to parts


def test_claim_elements_are_matched_to_numerals(parsed, registry):
    matched = claims_mod.match_to_registry(
        claims_mod.analyse(parsed.claims, use_model=False), registry)
    claim = next(c for c in matched if c.number == 1)
    found = {e.numeral for e in claim.elements if e.numeral}
    assert {"102", "104", "106", "108"} <= found


def test_an_unmatched_element_is_left_empty_rather_than_guessed(registry):
    parsed = sections.analyse(DRAFT.replace("a pressure sensor configured to report a pressure",
                                            "a flux capacitor configured to report a pressure"))
    matched = claims_mod.match_to_registry(
        claims_mod.analyse(parsed.claims, use_model=False), registry)
    claim = next(c for c in matched if c.number == 1)
    flux = [e for e in claim.elements if "flux" in e.term]
    assert flux and not flux[0].numeral


# --------------------------------------------------------------------------------- normalising


def test_a_numeral_split_by_a_space_is_rejoined():
    """A text layer that writes "1 02" hides the housing from the registry entirely."""
    assert "102" in ingest.normalise_text("the housing 1 02 carries")


def test_a_word_hyphenated_across_a_line_is_rejoined():
    assert "housing" in ingest.normalise_text("the hous-\ning 102")


def test_a_space_before_punctuation_is_removed():
    assert ingest.normalise_text("the housing 102 , which") == "the housing 102, which"


def test_no_em_dash_survives():
    assert "—" not in ingest.normalise_text("the housing—102—carries")


# -------------------------------------------------------------------------- patent numbers


@pytest.mark.parametrize("raw,expected", [
    ("US10123456B2", "US10123456B2"),
    ("US 10,123,456 B2", "US10123456B2"),
    ("10,123,456", "US10123456B2"),
    ("EP1234567A1", "EP1234567A1"),
    ("US 2021/0123456 A1", "US20210123456A1"),
    ("https://patents.google.com/patent/US9878876B2/en", "US9878876B2"),
])
def test_patent_numbers_are_normalised(raw, expected):
    assert ingest.normalise_patent_number(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "the housing 102 carries a pump", "a much longer sentence than a number could be",
])
def test_prose_is_not_mistaken_for_a_patent_number(raw):
    assert ingest.normalise_patent_number(raw) is None


def test_a_short_draft_is_refused_rather_than_run():
    with pytest.raises(ingest.IngestError):
        ingest.ingest(text="a housing 102")


# ------------------------------------------------------------------------- html extraction


def test_an_inline_figref_stays_in_its_sentence():
    """Google Patents wraps every FIG reference in an inline element.

    An extractor that takes only a block's direct text turns "FIG. 1 is a block diagram" into
    "is a block diagram", the brief description parses to nothing, and the figure set is invented
    rather than read. This is the regression test for that.
    """
    html = ('<html><body><section itemprop="description"><div class="description">'
            '<heading>BRIEF DESCRIPTION OF THE DRAWINGS</heading>'
            '<div class="description-paragraph"><figref idref="p-0001">FIG. 1</figref> '
            'is a block diagram of a system; and</div>'
            '<div class="description-paragraph"><figref>FIG. 2</figref> '
            'is a perspective view thereof.</div></div></section>'
            '<section itemprop="claims"><div class="claim"><div class="claim-text">'
            '1. A system comprising:<div class="claim-text">a housing; and</div>'
            '<div class="claim-text">a pump.</div></div></div></section></body></html>')
    _title, text = ingest.parse_google_patents(html)
    assert "FIG. 1 is a block diagram" in text
    assert "FIG. 2 is a perspective view" in text
    assert "1. A system comprising:" in text
    assert "a pump." in text
