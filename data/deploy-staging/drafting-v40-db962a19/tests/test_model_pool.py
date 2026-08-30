"""Tiers, round robin and health latching — and the reason `read` is not `fast`.

MEASURED on US-11999030-B2, the reference an examiner applied under 102(a)(2) to thirteen claims,
asking the same 68 limitations with the same prompt and the same 12,000 max_tokens:

    vertex-flash   20 disclosed,  8 partial, 40 absent   7 claims with a DISCLOSED, 28 quoted
    haiku           9 disclosed, 14 partial, 45 absent   3 claims with a DISCLOSED, 23 quoted
    sonnet         21 disclosed,  8 partial, 39 absent   8 claims with a DISCLOSED, 29 quoted

Haiku scores a candidate from a title and an abstract perfectly well and is materially worse at
finding a teaching in 90,000 characters and quoting it. Speed on the screen, evidence on the read.
"""
import model_pool as MP


def test_read_is_not_the_fast_pool():
    """If these ever collapse into one tier, the reader silently gets the cheaper model."""
    assert MP.READ != MP.FAST
    assert "haiku" not in MP.READ


def test_a_tier_is_never_empty(monkeypatch):
    """A strong tier with no key must fall back rather than fail the call: a degraded refuter is
    far better than no chart at all."""
    monkeypatch.setattr(MP, "STRONG", ["sonnet"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert MP.providers("strong"), "empty tier"


def test_an_unknown_tier_falls_back_to_fast():
    assert MP.providers("nonsense")


def test_round_robin_spreads_the_load(monkeypatch):
    monkeypatch.setattr(MP, "FAST", ["vertex-flash", "haiku"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    firsts = [MP._order("fast")[0].name for _ in range(4)]
    assert len(set(firsts)) == 2, firsts


def test_a_failing_provider_latches_off(monkeypatch):
    monkeypatch.setattr(MP, "_state", {})
    monkeypatch.setattr(MP, "FAIL_LIMIT", 2)
    MP._mark("vertex-flash", False)
    assert MP._ALL["vertex-flash"].available()
    MP._mark("vertex-flash", False)
    assert not MP._ALL["vertex-flash"].available(), "should be latched off after FAIL_LIMIT"


def test_a_success_resets_the_failure_run(monkeypatch):
    monkeypatch.setattr(MP, "_state", {})
    monkeypatch.setattr(MP, "FAIL_LIMIT", 3)
    MP._mark("vertex-flash", False)
    MP._mark("vertex-flash", True)
    MP._mark("vertex-flash", False)
    MP._mark("vertex-flash", False)
    assert MP._ALL["vertex-flash"].available(), "a success must clear the run"


def test_an_empty_body_counts_as_a_failure(monkeypatch):
    """This is exactly how muse-spark fails when its budget went on reasoning tokens: HTTP 200,
    content null. Treating it as success would return an empty answer as if it were real."""
    monkeypatch.setattr(MP, "_state", {})
    monkeypatch.setattr(MP, "FAST", ["vertex-flash"])
    monkeypatch.setattr(MP._ALL["vertex-flash"], "_call", lambda s, u, m: ("   ", 0, 0))
    try:
        MP.call("s", "u", 100, tier="fast")
    except RuntimeError as e:
        assert "empty response body" in str(e)
    else:
        raise AssertionError("an empty body must not be returned as an answer")


def test_the_reasoning_floor_is_applied(monkeypatch):
    """muse-spark returns EMPTY below ~2,500 max_tokens: measured, max_tokens=600 produced 597
    reasoning tokens and no content. A caller's sensible 1,200 must be floored, not honoured."""
    seen = {}
    monkeypatch.setattr(MP, "_post", lambda url, payload, headers: seen.update(payload) or
                        {"choices": [{"message": {"content": "{}"}}], "usage": {}})
    monkeypatch.setenv("META_API_KEY", "x")
    MP._ALL["muse"]._call("sys", "user", 1200)
    assert seen["max_tokens"] >= MP.REASONING_MIN_TOKENS, seen["max_tokens"]
