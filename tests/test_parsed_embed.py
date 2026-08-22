"""Tests for the parse-and-embed pipeline, one per failure that has already cost this project.

The claim tests are written against the measured defect, not against the fix: US 2025/0033224 A1
came back as TWO items, claims 1 to 19 glued into a 17,309-character blob and claim 20 alone, and
twenty claims were tracked as two while a 4,000-character clip dropped claims 5 to 19. Three of
the tests below defect-inject the guard, so they prove it is load-bearing rather than present.

Nothing here calls a paid API: `conftest.py` already replaces the embedding provider, and the
tests that need one replace `embed_common.embed_texts` explicitly. The database tests write only
to tables this workstream owns, under a publication id and a shard number no worker uses, and
delete their rows through an index rather than scanning a 32 GB staging table.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "ops"))

import parsed_norm                                                   # noqa: E402
import stage_chunks                                                  # noqa: E402
import batch_embed                                                   # noqa: E402
import embed_common                                                  # noqa: E402

TEST_PID = -999999999          # no surrogate will ever reach this; deletes go through ix_stage_v3_pub
TEST_SHARD = 777               # no deployed worker drains this shard


# --------------------------------------------------------------------------- fixtures in prose

def _claim_bodies(n=20):
    out = ["A vacuum gripping device comprising a housing, a suction cup carried by the housing, "
           "a manually operable pump in fluid communication with the suction cup, and an indicator "
           "responsive to the pressure within the suction cup."]
    for i in range(2, n + 1):
        out.append(f"A vacuum gripping device according to claim {i - 1}, wherein the sealing lip "
                   f"further comprises a resilient flange arranged to conform to an uneven "
                   f"substrate surface and to maintain the reduced pressure for at least "
                   f"{i * 10} seconds.")
    return out


def glued_payload(declared=20):
    """The measured shape: two items, nineteen claims in the first."""
    bodies = _claim_bodies(20)
    glued = "\n".join(f"{i}. {b}" for i, b in enumerate(bodies[:19], start=1))
    payload = {"publication_number": "US-2025033224-A1",
               "title": "Vacuum gripping device",
               "abstract": "A vacuum gripping device with a manually operable pump.",
               "claims": [glued, f"20. {bodies[19]}"],
               "description": "TECHNICAL FIELD\n" + ("The invention relates to vacuum "
                                                      "handling equipment. " * 40)}
    if declared:
        payload["n_claims"] = declared
    return payload


def unrecoverably_glued_payload():
    """A source that hands several items, one of which is several claims, and whose first claim
    carries no number so neither splitter can anchor the run. Positional numbering therefore wins
    and the glue survives into the chosen reading. This is what the guard exists for."""
    bodies = _claim_bodies(5)
    item1 = (bodies[0] + " 2. " + bodies[1] + " 3. " + bodies[2] + " 4. " + bodies[3])
    return {"publication_number": "US-9999999-B2", "title": "Vacuum lifter",
            "abstract": "A vacuum lifter.", "claims": [item1, "20. " + bodies[4]]}


# --------------------------------------------------------------------------- the claim assertion

def test_the_glued_twenty_claim_record_is_recovered_as_twenty_not_two():
    """The whole reason this module exists. Two items in, twenty numbered claims out."""
    doc = parsed_norm.normalise(glued_payload())
    assert len(doc.claims) == 20
    assert [c["claim_no"] for c in doc.claims] == list(range(1, 21))
    assert any("re-split" in n for n in doc.notes)


def test_the_recovered_claims_are_not_a_single_blob():
    """Twenty rows is not enough: claim 1 must be claim 1, not claims 1 to 19."""
    doc = parsed_norm.normalise(glued_payload())
    assert len(doc.claims[0]["text"]) < 400
    assert "2." not in doc.claims[0]["text"][:400] or "claim 2" not in doc.claims[0]["text"]


def test_a_declared_count_that_disagrees_with_the_parse_is_rejected():
    """The source says twenty and two survive splitting: that document does not get staged."""
    payload = {"publication_number": "US-1-A1", "n_claims": 20,
               "claims": ["A device comprising a housing and a suction cup, wherein the cup seals.",
                          "The device of claim 1, wherein the housing is moulded from polymer."]}
    with pytest.raises(parsed_norm.ParseRejected) as e:
        parsed_norm.normalise(payload)
    assert e.value.code == "CLAIM_COUNT_MISMATCH"
    assert e.value.detail["declared"] == 20
    assert 20 not in e.value.detail["readings"].values(), "no reading produced twenty"


def test_removing_the_declared_count_check_lets_the_wrong_count_through(monkeypatch):
    """DEFECT INJECTION. With the independent count ignored, the same document parses cleanly at
    two claims and nothing raises, which is exactly how twenty claims were tracked as two."""
    monkeypatch.setattr(parsed_norm, "declared_claim_count", lambda payload, blob=None: None)
    payload = {"publication_number": "US-1-A1", "n_claims": 20,
               "claims": ["A device comprising a housing and a suction cup, wherein the cup seals.",
                          "The device of claim 1, wherein the housing is moulded from polymer."]}
    doc = parsed_norm.normalise(payload)
    assert len(doc.claims) == 2, "the guard is the only thing that catches this"


def test_a_source_that_glues_claims_into_an_item_is_rejected():
    with pytest.raises(parsed_norm.ParseRejected) as e:
        parsed_norm.normalise(unrecoverably_glued_payload())
    assert e.value.code == "CLAIMS_GLUED"
    assert e.value.detail["embedded"][0] == 2


def test_removing_the_glue_guard_lets_the_glued_record_through(monkeypatch):
    """DEFECT INJECTION. Raise the run length out of reach and the four-claims-in-one item is
    staged as one claim, silently."""
    monkeypatch.setattr(parsed_norm, "GLUE_RUN_MIN", 99)
    doc = parsed_norm.normalise(unrecoverably_glued_payload())
    assert len(doc.claims) == 2, "without the guard the glued item is accepted whole"


def test_a_gap_in_the_claim_numbering_is_rejected():
    """A dropped claim is invisible rather than wrong, which is why nothing noticed the first
    time."""
    text = ("1. A device comprising a housing and a suction cup arranged to seal against a wall.\n"
            "2. The device of claim 1, wherein the cup is elastomeric and resists abrasion.\n"
            "4. The device of claim 2, wherein the pump is a bellows pump of at least 50 cc.\n"
            "5. The device of claim 4, wherein the indicator is a coloured band on a plunger.\n")
    with pytest.raises(parsed_norm.ParseRejected) as e:
        parsed_norm.normalise({"publication_number": "US-2-A1", "claims": text})
    assert e.value.code == "CLAIM_NUMBERING"


def test_a_claim_exactly_on_a_clip_boundary_is_rejected():
    """4,000 characters is the limitation splitter's window, and a claim that is exactly that long
    was clipped by something, not written that way."""
    body = ("A device comprising " + ("a further element, " * 400))[:4000]
    assert not body[-1].isspace()                # the splitter strips, so this must survive whole
    with pytest.raises(parsed_norm.ParseRejected) as e:
        parsed_norm.normalise({"publication_number": "US-3-A1", "claims": [f"1. {body}"]})
    assert e.value.code == "CLAIM_TRUNCATED"
    assert e.value.detail["chars"] == 4000


def test_a_document_that_declares_claims_and_carries_none_is_rejected():
    with pytest.raises(parsed_norm.ParseRejected) as e:
        parsed_norm.normalise({"publication_number": "US-4-A1", "n_claims": 8, "claims": []})
    assert e.value.code == "EMPTY_CLAIMS"


def test_a_document_with_no_text_at_all_is_rejected():
    with pytest.raises(parsed_norm.ParseRejected) as e:
        parsed_norm.normalise({"publication_number": "US-5-A1"})
    assert e.value.code == "NO_TEXT"


def test_a_record_with_no_publication_number_is_rejected():
    with pytest.raises(parsed_norm.ParseRejected) as e:
        parsed_norm.normalise({"abstract": "An abstract."})
    assert e.value.code == "NO_PUBLICATION"


# ------------------------------------------------- the count nobody was reading: "Claims (15)"

def gp_translated_payload(n=15):
    """The Google Patents shape for a machine-translated DE record. MEASURED on
    DE102017113305A1: a "Claims (15)" header, a "Translated from German" line, then fifteen
    claims separated by NEWLINES and carrying no numbers at all. Both numeric splitters return
    one claim of 3,487 characters."""
    bodies = [f"Vacuum lifter ({i}) having a vacuum device which generates a vacuum in a base "
              f"body and a suction plate arranged on a lower side of the base body, "
              f"characterized in that the sealing means encloses a suction space number {i}."
              for i in range(1, n + 1)]
    return {"publication_number": "DE102017113305A1", "title": "Vacuum lifter and suction plate",
            "claims": "Claims (%d)\nTranslated from German\n" % n + "\n".join(bodies),
            "abstract": "A vacuum lifter with a suction plate."}


def test_the_header_count_recovers_claims_neither_splitter_can_find():
    """Fifteen claims with no numbers is the same silent loss as the glued blob in different
    clothes: one chunk of 3,487 characters where fifteen claims should be."""
    import patent_doc
    payload = gp_translated_payload(15)
    assert len(patent_doc.split_claims(payload["claims"])) == 1, "the premise of the test"
    doc = parsed_norm.normalise(payload)
    assert len(doc.claims) == 15
    assert [c["claim_no"] for c in doc.claims] == list(range(1, 16))


def test_the_declared_count_is_read_from_the_claims_header():
    assert parsed_norm.declared_claim_count({}, "Claims (15)\nTranslated from German\nA thing.") == 15
    assert parsed_norm.declared_claim_count({}, "Ansprüche (7)\nEin Ding.") == 7


def test_the_source_header_never_becomes_a_claim():
    doc = parsed_norm.normalise(gp_translated_payload(15))
    assert not any("Translated from" in c["text"] for c in doc.claims)
    assert not any(c["text"].startswith("Claims (") for c in doc.claims)


def test_a_header_count_that_no_reading_matches_is_a_mismatch():
    """Nine declared, sixteen lines of 1970s OCR: this is the 88 of 460 in `sources_docstore`
    that no splitter can read, and it must be recorded rather than staged."""
    payload = {"publication_number": "DE1781406B2",
               "claims": "Claims (2)\n" + "\n".join(f"line {i} of scanned nonsense ι 2 kubaren"
                                                    for i in range(9))}
    with pytest.raises(parsed_norm.ParseRejected) as e:
        parsed_norm.normalise(payload)
    assert e.value.code == "CLAIM_COUNT_MISMATCH"
    assert e.value.detail["declared"] == 2


def test_unreadable_claims_do_not_take_the_abstract_and_description_down_with_them():
    """`allow_claim_defect` is what the worker passes. It changes WHICH material is discarded, not
    whether a wrong claim can be staged: the claim list is empty either way."""
    payload = {"publication_number": "DE1781406B2", "abstract": "A suction head for glass.",
               "description": "TECHNICAL FIELD\n" + ("The invention relates to suction. " * 50),
               "claims": "Claims (2)\n" + "\n".join(f"line {i} of scanned nonsense"
                                                    for i in range(9))}
    doc = parsed_norm.normalise(payload, allow_claim_defect=True)
    assert doc.claims == []
    assert doc.claim_defect["code"] == "CLAIM_COUNT_MISMATCH"
    assert doc.abstract and doc.paragraphs
    rows = stage_chunks.chunk_rows(doc, "test:key")
    assert not [r for r in rows if r["kind"].startswith("claim")]
    assert [r for r in rows if r["kind"] == "paragraph"]


def test_a_record_whose_only_text_was_unreadable_claims_is_still_rejected():
    payload = {"publication_number": "DE1781406B2",
               "claims": "Claims (2)\n" + "\n".join(f"line {i} nonsense" for i in range(9))}
    with pytest.raises(parsed_norm.ParseRejected) as e:
        parsed_norm.normalise(payload, allow_claim_defect=True)
    assert e.value.code == "CLAIM_COUNT_MISMATCH"


# --------------------------------------------------------------------------- shapes

def test_claims_arrive_in_four_shapes_and_flatten_the_same_way():
    bodies = _claim_bodies(3)
    numbered = [f"{i}. {b}" for i, b in enumerate(bodies, start=1)]
    shapes = [numbered,
              "\n".join(numbered),
              [{"text": t} for t in numbered],
              {"text": "\n".join(numbered)}]
    for shape in shapes:
        doc = parsed_norm.normalise({"publication_number": "US-6-A1", "claims": shape})
        assert [c["claim_no"] for c in doc.claims] == [1, 2, 3], shape


def test_the_declared_count_is_read_from_the_front_page_when_there_is_no_field():
    assert parsed_norm.declared_claim_count({"cover": "20 Claims, 36 Drawing Sheets"}) == 20
    assert parsed_norm.declared_claim_count({"meta": {"n_claims": 7}}) == 7
    assert parsed_norm.declared_claim_count({"n_claims": "12"}) == 12
    assert parsed_norm.declared_claim_count({}) is None


def test_a_description_with_no_line_structure_is_still_searchable():
    doc = parsed_norm.normalise({"publication_number": "US-7-A1",
                                 "description": "One long sentence about vacuum handling. " * 60})
    assert doc.paragraphs and doc.paragraphs[0]["text"]


# --------------------------------------------------------------------------- chunk boundaries

def _doc():
    return parsed_norm.normalise(glued_payload())


def test_chunk_kinds_are_exactly_the_kinds_the_live_schema_holds():
    """`sql/001_schema.sql` line 123. A seventh kind in staging is a kind nothing downstream knows
    how to weight."""
    rows = stage_chunks.chunk_rows(_doc(), "test:key")
    assert set(r["kind"] for r in rows) <= {"whole", "abstract", "claim_own", "claim_resolved",
                                            "paragraph", "figure_caption"}


def test_the_whole_chunk_is_title_plus_abstract_clipped_at_two_thousand():
    doc = _doc()
    doc.title = "T" * 3000
    rows = stage_chunks.chunk_rows(doc, "test:key")
    whole = [r for r in rows if r["kind"] == "whole"][0]
    assert len(whole["text"]) == stage_chunks.WHOLE_CHARS == 2000


def test_every_other_chunk_is_clipped_at_eight_thousand():
    doc = _doc()
    doc.abstract = "A" * 20000
    rows = stage_chunks.chunk_rows(doc, "test:key")
    for r in rows:
        assert len(r["text"]) <= (2000 if r["kind"] == "whole" else stage_chunks.MAX_CHARS)


def test_a_dependent_claim_emits_both_readings_and_an_independent_one_does_not():
    rows = stage_chunks.chunk_rows(_doc(), "test:key")
    own = {r["coord"]["claim_no"] for r in rows if r["kind"] == "claim_own"}
    res = {r["coord"]["claim_no"] for r in rows if r["kind"] == "claim_resolved"}
    assert own == set(range(1, 21))
    assert 1 not in res, "claim 1 is independent, so resolved == own and the row is a duplicate"
    assert res, "dependent claims must carry their parent's limitations"


def test_a_non_english_original_abstract_is_emitted_beside_the_english_one():
    doc = parsed_norm.normalise({"publication_number": "DE-1-A1", "abstract": "A gripper.",
                                 "abstract_orig": {"text": "Ein Greifer mit Saugnapf.",
                                                   "lang": "de"}})
    rows = stage_chunks.chunk_rows(doc, "test:key")
    abstracts = [r for r in rows if r["kind"] == "abstract"]
    assert len(abstracts) == 2
    assert {r["lang"] for r in abstracts} == {"en", "de"}


def test_a_paragraph_shorter_than_the_floor_is_not_a_chunk():
    doc = parsed_norm.normalise({"publication_number": "US-8-A1", "abstract": "x" * 50})
    doc.paragraphs = [{"para_no": "p0001", "heading": None, "text": "[0001]"}]
    rows = stage_chunks.chunk_rows(doc, "test:key")
    assert not [r for r in rows if r["kind"] == "paragraph"]


def test_every_staged_row_has_a_null_ref_id_and_carries_its_publication_number():
    """The structural half of the split from the description backfill: that job writes
    `ref_id = paragraphs.id`, this one never writes a ref_id at all, so the stage table's partial
    unique index on (kind, ref_id) cannot be contended and no reader can confuse the two."""
    rows = stage_chunks.chunk_rows(_doc(), "test:key")
    assert rows
    for r in rows:
        assert r["ref_id"] is None
        assert r["coord"]["pub"] == "US-2025033224-A1"


def test_the_item_key_is_stable_for_the_same_text_and_moves_with_it():
    a = stage_chunks.item_key("src", "abstract", "abstract", "hello")
    b = stage_chunks.item_key("src", "abstract", "abstract", "hello")
    c = stage_chunks.item_key("src", "abstract", "abstract", "hello there")
    assert a == b and a != c


def test_rerunning_the_same_document_produces_the_same_item_keys():
    one = stage_chunks.chunk_rows(_doc(), "test:key")
    two = stage_chunks.chunk_rows(_doc(), "test:key")
    assert [r["item_key"] for r in one] == [r["item_key"] for r in two]


# --------------------------------------------------------------------------- Gemini Batch

class FakeGcs:
    def __init__(self, files=None):
        self.written = {}
        self.files = files or {}

    def write_bytes(self, bucket, name, data, content_type=None):
        self.written[f"gs://{bucket}/{name}"] = data
        return {"name": name}

    def list_objects(self, bucket, prefix, start_after=None, limit=None):
        for name in sorted(self.files):
            if name.startswith(prefix):
                yield {"name": name}

    def read_jsonl(self, bucket, name):
        return iter(self.files[name])


class FakeRest:
    def __init__(self, job=None, state="JOB_STATE_SUCCEEDED", output_dir="gs://b/out/x/"):
        self.posted = []
        self.job = job or {"name": "projects/p/locations/l/batchPredictionJobs/1",
                           "state": "JOB_STATE_PENDING"}
        self.state, self.output_dir = state, output_dir

    def post(self, url, body):
        self.posted.append((url, body))
        return self.job

    def get(self, url):
        return {"state": self.state, "outputInfo": {"gcsOutputDirectory": self.output_dir},
                "error": {"message": ""}}


def test_a_batch_request_line_is_the_shape_vertex_actually_accepts():
    """Measured 2026-08-22: an `{"instances": [...]}` line is accepted at submission and then
    fails every row with "no such field: 'instances'", producing a SUCCEEDED job that embedded
    nothing. The dimension goes inside the request, not in modelParameters."""
    e = batch_embed.VertexBatchEmbedder(dim=768, task_type="RETRIEVAL_DOCUMENT")
    line = e.request_line("hello")
    assert list(line) == ["request"]
    assert line["request"]["content"]["parts"][0]["text"] == "hello"
    assert line["request"]["outputDimensionality"] == 768
    assert line["request"]["taskType"] == "RETRIEVAL_DOCUMENT"
    assert "instances" not in json.dumps(line)


def test_submit_writes_one_json_line_per_item_and_names_the_job():
    gcs, rest = FakeGcs(), FakeRest()
    e = batch_embed.VertexBatchEmbedder(bucket="b", prefix="p", rest=rest, gcs=gcs)
    job = e.submit([{"item_key": "a", "text": "one"}, {"item_key": "b", "text": "two"}], tag="t")
    assert job["n_items"] == 2
    body = gcs.written["gs://b/p/t/in.jsonl"].decode()
    assert len(body.splitlines()) == 2
    assert rest.posted[0][1]["model"].endswith("gemini-embedding-001")


def test_submit_refuses_an_empty_batch():
    e = batch_embed.VertexBatchEmbedder(rest=FakeRest(), gcs=FakeGcs())
    with pytest.raises(ValueError):
        e.submit([], tag="t")


def test_poll_separates_terminal_from_running_and_good_from_bad():
    for state, terminal, ok in (("JOB_STATE_PENDING", False, False),
                                ("JOB_STATE_RUNNING", False, False),
                                ("JOB_STATE_SUCCEEDED", True, True),
                                ("JOB_STATE_PARTIALLY_SUCCEEDED", True, True),
                                ("JOB_STATE_FAILED", True, False)):
        e = batch_embed.VertexBatchEmbedder(rest=FakeRest(state=state), gcs=FakeGcs())
        st = e.poll("projects/p/locations/l/batchPredictionJobs/1")
        assert (st["terminal"], st["ok"]) == (terminal, ok), state


def _out_line(text, values=None, status=""):
    return {"request": {"content": {"parts": [{"text": text}]}, "outputDimensionality": 4},
            "status": status, "response": ({"embedding": {"values": values}} if values else {})}


def test_a_partially_failed_batch_returns_only_the_items_that_failed():
    """A failed LINE in a SUCCEEDED job is the partial failure that matters: rerunning the whole
    job pays for the 4,998 rows that worked."""
    files = {"out/x/predictions.jsonl": [
        _out_line("one", [0.1, 0.2, 0.3, 0.4]),
        _out_line("two", None, status='{"code":3,"message":"input too long"}')]}
    e = batch_embed.VertexBatchEmbedder(dim=4, bucket="b", rest=FakeRest(), gcs=FakeGcs(files))
    vectors, errors = e.collect("gs://b/out/x/", [{"item_key": "a", "text": "one"},
                                                  {"item_key": "b", "text": "two"},
                                                  {"item_key": "c", "text": "three"}])
    assert list(vectors) == ["a"]
    assert set(errors) == {"b", "c"}
    assert "missing from batch output" in errors["c"]


def test_a_vector_of_the_wrong_dimension_is_an_error_not_a_row():
    files = {"out/x/predictions.jsonl": [_out_line("one", [0.1, 0.2])]}
    e = batch_embed.VertexBatchEmbedder(dim=4, bucket="b", rest=FakeRest(), gcs=FakeGcs(files))
    vectors, errors = e.collect("gs://b/out/x/", [{"item_key": "a", "text": "one"}])
    assert not vectors and set(errors) == {"a"}


def test_two_items_with_identical_text_both_get_a_vector():
    """The output echoes the request and carries no id of ours, so correlation is by text. Two
    identical texts are interchangeable because the embedding is a function of the text."""
    files = {"out/x/predictions.jsonl": [_out_line("same", [1, 2, 3, 4]),
                                         _out_line("same", [1, 2, 3, 4])]}
    e = batch_embed.VertexBatchEmbedder(dim=4, bucket="b", rest=FakeRest(), gcs=FakeGcs(files))
    vectors, errors = e.collect("gs://b/out/x/", [{"item_key": "a", "text": "same"},
                                                  {"item_key": "b", "text": "same"}])
    assert set(vectors) == {"a", "b"} and not errors


# --------------------------------------------------------------------------- shared machinery

def test_the_retry_policy_and_lease_key_survived_being_factored_out():
    """`ops/desc_backfill.py` is running right now against these. If the shared module changed
    their behaviour, that job's next 429 is a crash."""
    import desc_backfill as bf
    assert bf.retryable is embed_common.retryable
    assert bf.lease_key is embed_common.lease_key
    assert bf.lease_key("core", 0) == embed_common.lease_key("core", 0)
    assert bf._vec([1.5, 2.0]) == "[1.500000,2.000000]"
    assert bf.retryable(Exception("429")) and not bf.retryable(Exception("400 INVALID_ARGUMENT"))


def test_the_two_workers_cannot_take_each_others_lease():
    """Same pass name, same shard, different namespace: the description backfill and this
    pipeline must be unable to block each other by coincidence."""
    import desc_backfill as bf
    import parsed_embed as pe
    assert pe.LEASE_NAMESPACE != bf.LEASE_NAMESPACE


def test_the_two_workers_tag_their_rows_differently():
    import desc_backfill as bf
    import parsed_embed as pe
    assert pe.CORPUS_RELEASE != bf.CORPUS_RELEASE


def test_shard_ownership_is_stable_across_processes():
    import parsed_embed as pe
    assert pe.shard_of("gcs:b/parsed/US-1-A1/doc.json", 4) == \
        pe.shard_of("gcs:b/parsed/US-1-A1/doc.json", 4)
    assert 0 <= pe.shard_of("x", 8) < 8


def test_the_cost_estimate_is_linear_and_uses_the_list_price():
    import parsed_embed as pe
    a = pe.estimate_cost(1_000_000, 1000, price_per_mtok=0.15, chars_per_token=4)
    b = pe.estimate_cost(2_000_000, 1000, price_per_mtok=0.15, chars_per_token=4)
    assert a["tokens"] == 250_000_000
    assert round(b["usd"] / a["usd"], 6) == 2.0


def test_gcs_uri_parsing():
    import gcs_lite
    assert gcs_lite.parse_uri("gs://b/x/y.json") == ("b", "x/y.json")
    with pytest.raises(ValueError):
        gcs_lite.parse_uri("/b/x")


def test_a_gcs_source_with_no_bucket_yields_nothing_instead_of_raising():
    """Workstream C's output does not exist until C runs. A pipeline that crashes because its
    upstream has not started is a pipeline nobody can leave running."""
    import parsed_sources

    class Boom:
        def list_objects(self, *a, **k):
            raise RuntimeError("404 bucket does not exist")

    docs, wm, err = parsed_sources.GcsParsedSource(bucket="nope", gcs=Boom()).fetch("", limit=5)
    assert docs == [] and "404" in err


# --------------------------------------------------------------------------- database

def _conn():
    import parsed_embed as pe
    try:
        conn = pe._connect()
    except Exception as exc:                                         # noqa: BLE001
        pytest.skip(f"corpus DB unreachable: {exc}")
    pe.ensure_schema(conn)
    return conn


def _cleanup(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chunks_stage_v3 WHERE publication_id = %s", (TEST_PID,))
        cur.execute("DELETE FROM parsed_embed_item WHERE shard = %s", (TEST_SHARD,))
        cur.execute("DELETE FROM parsed_embed_batch WHERE shard = %s", (TEST_SHARD,))
        cur.execute("DELETE FROM parsed_doc_ledger WHERE source_key LIKE 'test:parsed:%'")
        cur.execute("DELETE FROM parsed_stage_pub WHERE publication_number LIKE 'TEST-PARSED-%'")
    conn.commit()


@pytest.fixture()
def db():
    conn = _conn()
    _cleanup(conn)
    try:
        yield conn
    finally:
        _cleanup(conn)
        conn.close()


def test_a_second_worker_is_refused_the_lease(db):
    import parsed_embed as pe
    other = pe._connect()
    try:
        assert embed_common.take_lease(db, pe.LEASE_NAMESPACE, "test_pass_unused", 96) is True
        assert embed_common.take_lease(other, pe.LEASE_NAMESPACE, "test_pass_unused", 96) is False
    finally:
        other.close()


def test_the_lease_is_released_when_the_worker_dies(db):
    import parsed_embed as pe
    a = pe._connect()
    assert embed_common.take_lease(a, pe.LEASE_NAMESPACE, "test_pass_unused3", 95) is True
    a.close()
    b = pe._connect()
    try:
        assert embed_common.take_lease(b, pe.LEASE_NAMESPACE, "test_pass_unused3", 95) is True
    finally:
        b.close()


def test_the_description_backfills_lease_does_not_block_this_one(db):
    """Same pass, same shard, different namespace: both must be grantable at the same time."""
    import desc_backfill as bf
    import parsed_embed as pe
    a, b = pe._connect(), pe._connect()
    try:
        assert embed_common.take_lease(a, bf.LEASE_NAMESPACE, "test_pass_unused4", 94) is True
        assert embed_common.take_lease(b, pe.LEASE_NAMESPACE, "test_pass_unused4", 94) is True
    finally:
        a.close()
        b.close()


def test_a_publication_with_paragraph_rows_keeps_its_description_with_the_desc_backfill(db):
    """THE DISJOINTNESS CHECK, against the live corpus. `ops/desc_backfill.py` reads
    `FROM paragraphs`; anything with a row there is its work and none of ours."""
    import parsed_embed as pe
    with db.cursor() as cur:
        cur.execute("SELECT publication_id FROM paragraphs ORDER BY id LIMIT 1")
        row = cur.fetchone()
    if not row:
        pytest.skip("no paragraphs in this database")
    owned = pe.publications_with_paragraphs(db, [row["publication_id"], TEST_PID])
    assert row["publication_id"] in owned
    assert TEST_PID not in owned, "a surrogate id must never look like the backfill's population"


def test_a_surrogate_publication_id_is_negative_and_stable(db):
    import parsed_embed as pe
    a = pe.surrogate_publication_ids(db, ["TEST-PARSED-1", "TEST-PARSED-2"])
    db.commit()
    b = pe.surrogate_publication_ids(db, ["TEST-PARSED-1"])
    db.commit()
    assert all(v < 0 for v in a.values())
    assert a["TEST-PARSED-1"] == b["TEST-PARSED-1"]


def _queue(conn, texts):
    import parsed_embed as pe
    rows = [{"item_key": stage_chunks.item_key("test:parsed:k", "abstract", f"s{i}", t),
             "kind": "abstract", "coord": {"pub": "TEST-PARSED-1", "slot": i}, "lang": "en",
             "text": t} for i, t in enumerate(texts)]
    n = pe.enqueue(conn, rows, "test:parsed:k", TEST_PID, TEST_SHARD)
    conn.commit()
    return rows, n


def test_enqueue_is_idempotent_on_the_item_key(db):
    texts = ["one two three four five", "six seven eight nine ten"]
    _, first = _queue(db, texts)
    _, second = _queue(db, texts)
    assert first == 2 and second == 0, "a re-listed document must cost nothing"


def test_a_drained_item_is_staged_and_removed_in_one_transaction(db, monkeypatch):
    """Exactly once. A vector in the stage table and a row still in the queue would be paid for
    twice; a deleted row with no vector would be lost."""
    import parsed_embed as pe
    monkeypatch.setattr(pe, "CORPUS_RELEASE", "test-parsed-suite")
    monkeypatch.setattr(embed_common, "embed_texts",
                        lambda texts, *a, **k: [[0.01 * (i + 1)] * pe.EMBED_DIM
                                                for i in range(len(texts))])
    _queue(db, ["alpha beta gamma delta", "epsilon zeta eta theta"])
    done, calls = pe.drain_sync(db, TEST_SHARD, limit=10)
    assert done == 2 and calls == 1
    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM chunks_stage_v3 WHERE publication_id = %s", (TEST_PID,))
        assert cur.fetchone()["c"] == 2
        cur.execute("SELECT count(*) c FROM parsed_embed_item WHERE shard = %s", (TEST_SHARD,))
        assert cur.fetchone()["c"] == 0
    # a restart re-embeds nothing, because there is nothing left queued
    assert pe.drain_sync(db, TEST_SHARD, limit=10) == (0, 0)


def test_a_staged_row_carries_its_model_dimension_and_release(db, monkeypatch):
    import parsed_embed as pe
    monkeypatch.setattr(pe, "CORPUS_RELEASE", "test-parsed-suite")
    monkeypatch.setattr(embed_common, "embed_texts",
                        lambda texts, *a, **k: [[0.02] * pe.EMBED_DIM for _ in texts])
    _queue(db, ["a chunk of text long enough to be real"])
    pe.drain_sync(db, TEST_SHARD, limit=10)
    with db.cursor() as cur:
        cur.execute("""SELECT kind, ref_id, embed_model, embed_dim, task_type, chunker_version,
                              corpus_release, coord FROM chunks_stage_v3
                       WHERE publication_id = %s""", (TEST_PID,))
        r = cur.fetchone()
    assert r["ref_id"] is None                      # never the description backfill's namespace
    assert r["embed_model"] == pe.EMBED_MODEL and r["embed_dim"] == pe.EMBED_DIM
    assert r["corpus_release"] == "test-parsed-suite"
    assert r["coord"]["pub"] == "TEST-PARSED-1"


def test_a_failed_synchronous_call_requeues_its_items_rather_than_losing_them(db, monkeypatch):
    import parsed_embed as pe

    def boom(*a, **k):
        raise RuntimeError("503 unavailable")

    monkeypatch.setattr(embed_common, "embed_texts", boom)
    _queue(db, ["alpha beta gamma delta", "epsilon zeta eta theta"])
    done, _ = pe.drain_sync(db, TEST_SHARD, limit=10)
    assert done == 0
    with db.cursor() as cur:
        cur.execute("SELECT state, attempts FROM parsed_embed_item WHERE shard = %s", (TEST_SHARD,))
        rows = cur.fetchall()
    assert [r["state"] for r in rows] == ["queued", "queued"]
    assert all(r["attempts"] == 1 for r in rows)


def test_a_poison_item_is_parked_instead_of_looping_for_ever(db, monkeypatch):
    import parsed_embed as pe
    monkeypatch.setattr(pe, "MAX_ITEM_ATTEMPTS", 2)
    rows, _ = _queue(db, ["alpha beta gamma delta"])
    keys = [r["item_key"] for r in rows]
    pe.requeue(db, keys, "boom")
    with db.cursor() as cur:
        cur.execute("SELECT state FROM parsed_embed_item WHERE item_key = ANY(%s)", (keys,))
        assert cur.fetchone()["state"] == "queued"
    pe.requeue(db, keys, "boom")
    with db.cursor() as cur:
        cur.execute("SELECT state FROM parsed_embed_item WHERE item_key = ANY(%s)", (keys,))
        assert cur.fetchone()["state"] == "failed"


class StubEmbedder:
    """A batch job that succeeds for the first item and fails the second."""

    def __init__(self, vectors, errors, state="JOB_STATE_SUCCEEDED"):
        self.vectors, self.errors, self.state = vectors, errors, state

    def submit(self, items, tag=None):
        return {"job_name": f"projects/p/locations/l/batchPredictionJobs/{tag}",
                "state": "JOB_STATE_PENDING", "input_uri": "gs://b/in.jsonl",
                "output_prefix": "gs://b/out/", "n_items": len(items)}

    def poll(self, job_name):
        return {"state": self.state, "terminal": True,
                "ok": self.state in batch_embed.GOOD, "output_dir": "gs://b/out/x/", "error": ""}

    def collect(self, output_dir, items):
        keys = [i["item_key"] for i in items]
        return ({k: self.vectors for k in keys[:1]},
                {k: self.errors for k in keys[1:]})


def test_a_partial_batch_stages_what_worked_and_requeues_only_what_did_not(db, monkeypatch):
    import parsed_embed as pe
    monkeypatch.setattr(pe, "CORPUS_RELEASE", "test-parsed-suite")
    _queue(db, ["alpha beta gamma delta", "epsilon zeta eta theta"])
    stub = StubEmbedder([0.03] * pe.EMBED_DIM, "input too long")
    batch_id = pe.submit_batch(db, TEST_SHARD, embedder=stub, limit=10)
    assert batch_id
    with db.cursor() as cur:
        cur.execute("SELECT state, count(*) c FROM parsed_embed_item WHERE shard = %s GROUP BY 1",
                    (TEST_SHARD,))
        assert cur.fetchall() == [{"state": "submitted", "c": 2}]
    staged = pe.resume_batches(db, TEST_SHARD, embedder=stub)
    assert staged == 1
    with db.cursor() as cur:
        cur.execute("SELECT state, last_error FROM parsed_embed_item WHERE shard = %s",
                    (TEST_SHARD,))
        left = cur.fetchall()
        cur.execute("SELECT count(*) c FROM chunks_stage_v3 WHERE publication_id = %s", (TEST_PID,))
        assert cur.fetchone()["c"] == 1
        cur.execute("SELECT state, n_ok, n_failed FROM parsed_embed_batch WHERE id = %s",
                    (batch_id,))
        b = cur.fetchone()
    assert len(left) == 1 and left[0]["state"] == "queued"
    assert "input too long" in left[0]["last_error"]
    assert (b["state"], b["n_ok"], b["n_failed"]) == ("collected", 1, 1)


def test_a_failed_batch_job_requeues_every_item(db):
    import parsed_embed as pe
    _queue(db, ["alpha beta gamma delta", "epsilon zeta eta theta"])
    stub = StubEmbedder(None, None, state="JOB_STATE_FAILED")
    pe.submit_batch(db, TEST_SHARD, embedder=stub, limit=10)
    assert pe.resume_batches(db, TEST_SHARD, embedder=stub) == 0
    with db.cursor() as cur:
        cur.execute("SELECT state, count(*) c FROM parsed_embed_item WHERE shard = %s GROUP BY 1",
                    (TEST_SHARD,))
        assert cur.fetchall() == [{"state": "queued", "c": 2}]


def test_a_worker_that_died_before_naming_its_job_gets_its_items_back(db):
    """The kill window between claiming the items and the submission returning. The items are
    definitely real; the job may not be. Paying twice for a job nobody collects is the cheaper
    mistake than losing the rows."""
    import parsed_embed as pe
    rows, _ = _queue(db, ["alpha beta gamma delta"])
    with db.cursor() as cur:
        cur.execute("INSERT INTO parsed_embed_batch (state, n_items, shard) "
                    "VALUES ('building', 1, %s) RETURNING id", (TEST_SHARD,))
        batch_id = cur.fetchone()["id"]
        cur.execute("UPDATE parsed_embed_item SET state='submitted', batch_id=%s WHERE shard=%s",
                    (batch_id, TEST_SHARD))
    db.commit()
    assert pe.resume_batches(db, TEST_SHARD, embedder=StubEmbedder(None, None)) == 0
    with db.cursor() as cur:
        cur.execute("SELECT state FROM parsed_embed_item WHERE shard = %s", (TEST_SHARD,))
        assert cur.fetchone()["state"] == "queued"
        cur.execute("SELECT state FROM parsed_embed_batch WHERE id = %s", (batch_id,))
        assert cur.fetchone()["state"] == "failed"


def test_the_ledger_records_a_rejection_with_its_numbers(db):
    import parsed_embed as pe

    class OneDoc:
        name = "stub"

        def fetch(self, watermark, limit=200):
            return ([parsed_sources_RawDoc()], "w1", None)

    import parsed_sources

    def parsed_sources_RawDoc():
        return parsed_sources.RawDoc(
            source_key="test:parsed:reject", publication_number="TEST-PARSED-3",
            payload={"publication_number": "TEST-PARSED-3", "n_claims": 20,
                     "claims": ["A device comprising a housing and a suction cup that seals.",
                                "The device of claim 1, wherein the housing is polymer."]},
            watermark="w1")

    prog = {"watermarks": {}}
    counts = pe.ingest_round(db, [OneDoc()], prog, shard=pe.shard_of("test:parsed:reject", 1),
                             shards=1)
    assert counts["rejected"] == 1
    with db.cursor() as cur:
        cur.execute("SELECT state, code, detail FROM parsed_doc_ledger WHERE source_key = %s",
                    ("test:parsed:reject",))
        r = cur.fetchone()
    assert r["state"] == "rejected" and r["code"] == "CLAIM_COUNT_MISMATCH"
    assert r["detail"]["declared"] == 20
