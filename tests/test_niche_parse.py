from __future__ import annotations

import json

from corpus.niche.chunks import build_chunks, write_chunks_jsonl, write_chunks_parquet
from corpus.niche.parse import merge_parsed, parse_source

XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<patent-document lang="de">
  <bibliographic-data>
    <publication-reference><document-id><country>DE</country><doc-number>102000001</doc-number><kind>A1</kind><date>20200102</date></document-id></publication-reference>
    <invention-title lang="de">Vakuumgreifer</invention-title>
    <abstract lang="de"><p>Ein Greifer fuer Werkstuecke.</p></abstract>
    <classification-cpc><text>B25J 15/0616</text></classification-cpc>
    <classification-ipc><text>B25J 15/06</text></classification-ipc>
    <references-cited><citation><patcit><document-id><country>EP</country><doc-number>1234567</doc-number><kind>A1</kind></document-id></patcit></citation></references-cited>
  </bibliographic-data>
  <claims lang="de">
    <claim num="1"><claim-text>1. Ein Greifer mit einer Saugglocke.</claim-text></claim>
    <claim num="2"><claim-text>2. Greifer nach Anspruch 1, mit einem Ventil.</claim-text></claim>
    <claim num="3"><claim-text>3. Greifer nach einem der vorhergehenden Ansprueche.</claim-text></claim>
  </claims>
  <description lang="de">
    <heading>Technisches Gebiet</heading>
    <p id="p0001" page="3">Der Greifer hebt eine Platte.</p>
    <heading>Ausfuehrung</heading>
    <p id="p0002" page="4">Eine Pumpe erzeugt Unterdruck.</p>
  </description>
  <figures><figure><figref>Fig. 1</figref><p>Greifer in Seitenansicht.</p></figure></figures>
</patent-document>
"""


def test_parser_outputs_canonical_patent_shape():
    parsed = parse_source(
        XML,
        media_type="application/xml",
        publication_number="DE102000001A1",
        provider="marec",
    )

    assert parsed["publication_number"] == "DE102000001A1"
    assert parsed["title"] == "Vakuumgreifer"
    assert parsed["abstract"] == "Ein Greifer fuer Werkstuecke."
    assert parsed["language"] == "de"
    assert parsed["cpc"] == ["B25J15/0616"]
    assert parsed["ipc"] == ["B25J15/06"]
    assert parsed["citations"] == ["EP1234567A1"]
    assert parsed["source"]["provider"] == "marec"


def test_claim_dependency_parsing_never_invents_ambiguous_ancestry():
    parsed = parse_source(XML, "application/xml", "DE102000001A1", "marec")
    claims = parsed["claims"]

    assert claims[0]["number"] == 1
    assert claims[0]["independent"] is True
    assert claims[0]["depends_on"] == []
    assert claims[0]["chain_complete"] is True
    assert claims[1]["depends_on"] == [1]
    assert claims[1]["independent"] is False
    assert "Saugglocke" in claims[1]["resolved_text"]
    assert claims[2]["depends_on"] == []
    assert claims[2]["chain_complete"] is False


def test_description_paragraph_boundaries_and_locations_are_preserved():
    parsed = parse_source(XML, "application/xml", "DE102000001A1", "marec")

    assert parsed["description_paragraphs"] == [
        {
            "id": "p0001",
            "text": "Der Greifer hebt eine Platte.",
            "section": "Technisches Gebiet",
            "page": 3,
            "language": "de",
            "source_location": "paragraph:p0001;page:3",
        },
        {
            "id": "p0002",
            "text": "Eine Pumpe erzeugt Unterdruck.",
            "section": "Ausfuehrung",
            "page": 4,
            "language": "de",
            "source_location": "paragraph:p0002;page:4",
        },
    ]


def test_normalized_chunks_are_embedding_ready_and_stable(tmp_path):
    parsed = parse_source(XML, "application/xml", "DE102000001A1", "marec")

    chunks = build_chunks(parsed)
    path = write_chunks_jsonl(chunks, tmp_path / "chunks.jsonl")
    written = [json.loads(line) for line in path.read_text().splitlines()]

    assert {row["chunk_kind"] for row in written} == {
        "abstract", "claim_own", "claim_resolved", "description", "figure_caption"
    }
    assert all(row["publication_id"] == "DE102000001A1" for row in written)
    assert all(len(row["content_hash"]) == 64 for row in written)
    assert all(row["chunk_id"] for row in written)
    assert sum(row["chunk_kind"] == "description" for row in written) == 2
    assert not any(
        row["chunk_kind"] == "claim_resolved" and row["claim_number"] == 3
        for row in written
    )


def test_empty_parquet_batch_retains_embedding_schema(tmp_path):
    import pyarrow.parquet as pq

    path = write_chunks_parquet([], tmp_path / "chunks.parquet")

    assert pq.read_schema(path).names == [
        "chunk_id",
        "publication_id",
        "family_id",
        "chunk_kind",
        "claim_number",
        "language",
        "text",
        "source_location",
        "content_hash",
    ]


def test_html_parser_preserves_separate_description_paragraphs():
    html = b"""
    <html lang="en"><head>
      <meta name="DC.title" content="Vacuum lifting tool">
    </head><body>
      <section itemprop="abstract"><p>A compact lifter.</p></section>
      <section itemprop="claims">
        <div class="claim"><div class="claim-text">1. A vacuum lifting tool.</div></div>
        <div class="claim"><div class="claim-text">2. The tool of claim 1 with a valve.</div></div>
      </section>
      <section itemprop="description">
        <heading>Background</heading><p id="p-1">First paragraph.</p><p id="p-2">Second paragraph.</p>
      </section>
    </body></html>
    """

    parsed = parse_source(html, "text/html", "US1234567A1", "google_patents")

    assert [p["text"] for p in parsed["description_paragraphs"]] == [
        "First paragraph.", "Second paragraph."
    ]
    assert parsed["claims"][1]["depends_on"] == [1]
    assert parsed["claims"][0]["independent"] is True
    assert parsed["claims"][0]["dependent"] is False
    assert parsed["claims"][1]["dependent"] is True


def test_html_parser_groups_nested_claim_text_continuations():
    html = b"""
    <html lang="en"><body>
      <section itemprop="claims">
        <div class="claim-text"><b>1</b>. A vacuum tool comprising
          <div class="claim-text">a suction cup,</div>
          <div class="claim-text">a valve, and</div>
          <div class="claim-text">a vacuum generator.</div>
        </div>
        <div class="claim-text"><b>2</b>. The tool of claim 1, wherein the valve is pneumatic.</div>
      </section>
      <section itemprop="description"><p>A complete description.</p></section>
    </body></html>
    """

    parsed = parse_source(html, "text/html", "US1234567A1", "google_patents")

    assert len(parsed["claims"]) == 2
    assert parsed["claims"][0]["number"] == 1
    assert "suction cup" in parsed["claims"][0]["text"]
    assert "vacuum generator" in parsed["claims"][0]["text"]
    assert parsed["claims"][1]["number"] == 2
    assert parsed["claims"][1]["depends_on"] == [1]


def test_html_parser_preserves_google_description_line_boundaries():
    html = b"""
    <html lang="en"><body>
      <section itemprop="claims"><div class="claim-text"><b>1</b>. A vacuum tool.</div></section>
      <section itemprop="description">
        <heading>Technical field</heading>
        <li><para-num num="[0001]"></para-num>
          <div id="p-0002" num="0001" class="description-line">First paragraph.</div>
        </li>
        <li><para-num num="[0002]"></para-num>
          <div id="p-0003" num="0002" class="description-line">Second paragraph.</div>
        </li>
      </section>
    </body></html>
    """

    parsed = parse_source(html, "text/html", "US1234567A1", "google_patents")

    assert parsed["description_paragraphs"] == [
        {
            "id": "p-0002",
            "text": "First paragraph.",
            "section": "Technical field",
            "page": None,
            "language": "en",
            "source_location": "paragraph:p-0002",
        },
        {
            "id": "p-0003",
            "text": "Second paragraph.",
            "section": "Technical field",
            "page": None,
            "language": "en",
            "source_location": "paragraph:p-0003",
        },
    ]


def test_html_parser_limits_citations_to_citation_tables():
    html = b"""
    <html lang="en"><body>
      <span itemprop="publicationNumber">US9999999A1</span>
      <section itemprop="claims"><div class="claim-text"><b>1</b>. A vacuum tool.</div></section>
      <section itemprop="description"><p>Description.</p></section>
      <table itemprop="patentCitations"><tr><td itemprop="publicationNumber">EP1234567A1</td></tr></table>
      <table itemprop="citedBy"><tr><td itemprop="publicationNumber">DE7654321B1</td></tr></table>
    </body></html>
    """

    parsed = parse_source(html, "text/html", "US1234567A1", "google_patents")

    assert parsed["citations"] == ["EP1234567A1", "DE7654321B1"]


def test_parser_merge_deduplicates_structured_figure_captions():
    first = parse_source(XML, "application/xml", "DE102000001A1", "marec")
    first["figure_captions"] = [{"number": "1", "text": "Side view"}]
    second = dict(first)
    second["source"] = {"provider": "epo"}

    merged = merge_parsed(first, second)

    assert merged["figure_captions"] == first["figure_captions"]
    assert [source["provider"] for source in merged["source_history"]] == [
        "marec",
        "epo",
    ]


def test_parser_merge_prefers_more_text_over_more_tiny_fragments():
    first = parse_source(XML, "application/xml", "DE102000001A1", "marec")
    second = dict(first)
    second["description_paragraphs"] = [
        {"id": f"tiny-{index}", "text": "x"} for index in range(10)
    ]

    merged = merge_parsed(first, second)

    assert merged["description_paragraphs"] == first["description_paragraphs"]
