"""A complete answer from a provider that does not have a JSON mode is not a truncated one.

Vertex returns bare JSON because it has a response-mime-type. Anthropic does not: it returns the
object inside a ```json fence or after a sentence of preamble. Measured on the reader's own prompt
over seven real documents, EVERY Anthropic reply failed `json.loads`, fell into the truncation
salvage, was logged as "a PREFIX, not a complete answer" and was flagged `_truncated` — while
returning all 84 of the 84 rows asked for.

`disclosures.extract` reads `_truncated` as "the checklist is incomplete", so a healthy provider's
complete answer was being treated as degraded.
"""
import llm


def test_bare_json_still_parses():
    assert llm._extract_json('{"results":[{"id":1}]}') == {"results": [{"id": 1}]}


def test_a_fenced_reply_is_complete_not_truncated():
    assert llm._extract_json('```json\n{"results":[{"id":1}]}\n```') == {"results": [{"id": 1}]}


def test_a_reply_with_preamble_is_complete_not_truncated():
    txt = 'Here is the analysis you asked for:\n{"results":[{"id":1}]}\nLet me know if you need more.'
    assert llm._extract_json(txt) == {"results": [{"id": 1}]}


def test_genuinely_truncated_text_falls_through_to_the_salvage():
    """The salvage and the _truncated flag must still fire for a real cut-off answer."""
    assert llm._extract_json('{"results": [{"id":1},{"id":2},{"id":') is None


def test_a_non_object_is_not_accepted():
    assert llm._extract_json("[1,2,3]") is None
    assert llm._extract_json("") is None
    assert llm._extract_json("no json here at all") is None


def test_chat_json_does_not_flag_a_fenced_answer_as_truncated(monkeypatch):
    monkeypatch.setattr(llm, "_call",
                        lambda s, u, m, tier="fast": ('```json\n{"disclosures":[{"text":"x"}]}\n```',
                                                      "test", 1, 1))
    out = llm.chat_json("sys", "user")
    assert out == {"disclosures": [{"text": "x"}]}
    assert "_truncated" not in out
