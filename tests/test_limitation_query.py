"""One query portfolio per limitation: the shapes that were measured, pinned.

Each test names the measurement it protects. The numbers in the docstrings come from running the
real portfolio against the BigQuery working set that holds all ten references a patent attorney
filed against US 2026/0109053 A1 (`nimo-gpt.patent_pilot.ws_36208e56133485ea`, 2,392,628
publications, every one with full text).
"""
import re

import limitation_query as LQ

PLAN = {
    "claim 1[c]": {
        "thing": ["muffler", "silenc", "sound absorb", "acoustic"],
        "place": ["exhaust", "air flow", "discharg"],
        "apparatus": ["handle", "grip", "portable"],
        "cpc": ["F01N", "G10K"],
        "why": "a sound-damping device in the exhaust air path",
        "text": "a sound-damping device arranged in the exhaust air path",
        "claim_label": "claim 1",
    },
    "claim 3[a]": {
        "thing": ["lining", "foam", "fibrous"],
        "place": ["sound absorb", "acoustic"],
        "apparatus": ["handle", "grip"],
        "cpc": ["G10K"],
        "why": "the grip lined with sound-damping material",
        "text": "the grip portion is lined with a sound-damping material",
        "claim_label": "claim 3",
    },
}


# ---------------------------------------------------------------------------
# the query shape
# ---------------------------------------------------------------------------
def test_proximity_is_a_sentence_not_a_document():
    """The limitation is a thing IN a place. A document that mentions both, pages apart, is a
    different and much larger question: 18,258 hits against 14,373 on the measured working set."""
    p = LQ.proximity(["muffler", "silenc"], ["exhaust"], window=60)
    assert re.search(p, "a muffler mounted in the exhaust duct")
    assert re.search(p, "exhaust gases leave through the silencer")
    assert not re.search(p, "a muffler. " + "word " * 40 + "an exhaust port")


def test_proximity_compiles_under_python_re_not_only_bigquery():
    """`(?s)` mid-pattern is legal in RE2 and a hard error in Python's `re`, so a pattern that
    worked in the query raised in every local test and fallback that touched it."""
    for a, b in ((["muffler"], ["exhaust"]), (["sound absorb", "acoustic"], ["air flow", "duct"])):
        assert re.compile(LQ.proximity(a, b))


def test_a_facet_with_no_partner_produces_no_query_rather_than_a_wide_one():
    """A missing `place` facet must not silently degrade to "documents mentioning exhaust"."""
    assert LQ.proximity([], ["exhaust"]) == ""
    assert LQ.proximity(["muffler"], []) == ""


def test_model_terms_are_neutralised_not_escaped():
    """A term carrying a regex metacharacter is a mistake, and an ESCAPED one is a term that can
    never match. Drop the character and keep the word."""
    got = LQ._terms(["sound (absorb)", "x*y|z", "ok", "muffler", "muffler"])
    assert "sound absorb" in got
    assert "muffler" in got
    assert got.count("muffler") == 1                     # deduped
    assert all("(" not in t and "|" not in t and "*" not in t for t in got)
    assert all(len(t) >= LQ.MIN_TERM for t in got)       # 'ok' is too short to be a term


def test_sql_uses_the_aliased_date_column():
    """The era CASE ran against `publication_date` while the CTE had already aliased it to `pd`,
    so the whole portfolio failed with an unresolved-column error and returned 0 of 10."""
    sql, _ = LQ.build_sql(PLAN, "p.d.t", date_max="20241018")
    #  The CASE expression alone. The `b` CTE's own WHERE legitimately names the source column,
    #  because the alias does not exist yet at that point.
    era = sql[sql.index("CASE WHEN"):sql.index("AS era")]
    assert "pd <" in era and "publication_date" not in era


def test_class_is_a_boost_never_a_filter():
    """Narrowing to the subject's own CPC symbols returned 0 of 12 cited references. A class guess
    that is wrong must cost rank, not visibility."""
    sql, _ = LQ.build_sql(PLAN, "p.d.t")
    assert "_cpc" in sql
    body = sql[sql.index("WHERE rank_era"):]
    assert "cpc" not in body.lower(), "CPC must not appear in the final WHERE"


def test_the_date_bound_excludes_art_that_is_not_prior_art():
    sql, _ = LQ.build_sql(PLAN, "p.d.t", date_max="2024-10-18")
    assert "publication_date < 20241018" in sql


# ---------------------------------------------------------------------------
# the caps — every one of these is a measured regression
# ---------------------------------------------------------------------------
def test_era_rank_is_the_only_rank_filter_in_sql():
    """THE CAP THAT BOUND. With `rank_era <= 350` AND `rank_q <= 800` the global cap silently won:
    Blatt sits at era-rank 200 / limitation-rank 1,397, Quackenbush at 219 / 2,429 and Hukelmann
    at 334 / 1,024 — all inside the era quota, all cut by the global one — and the portfolio
    returned 2 of 10 instead of 5."""
    sql, _ = LQ.build_sql(PLAN, "p.d.t", per_era=350, per_limitation=800)
    where = sql[sql.index("WHERE rank_era"):sql.index("ORDER BY q")]
    assert "rank_era <= 350" in where
    assert "rank_q" not in where


def _rows(lim_id, per_era):
    """One limitation's rows: a big modern bucket and a small old one, modern scoring higher."""
    out = {}
    for era, n, base in (("2015_now", per_era, 100.0), ("pre1980", per_era, 10.0)):
        out[era] = [{"pub": f"{lim_id}-{era}-{i}", "fam": f"{lim_id}-{era}-{i}",
                     "title": "", "abstract": "", "publication_date": 20200101,
                     "era": era, "score": base - i, "n_cooc": 1, "in_claims": False,
                     "in_title": False, "in_class": False, "for_limitation": lim_id,
                     "acquired": "limitation_portfolio", "why": ""}
                    for i in range(n)]
    return out


def test_the_per_limitation_cap_interleaves_eras(monkeypatch):
    """Taking the top N of a limitation by score re-imposes the single ranked list the era buckets
    exist to escape: the modern bucket outscores the old one everywhere, so it would take the whole
    allowance and the 1960s art — which is what kills claims — would never be offered."""
    monkeypatch.setattr(LQ, "PER_LIMITATION", 10)
    picked = _run_selection({"L1": _rows("L1", 50)}, max_total=1000)
    eras = [c["era"] for c in picked["by_limitation"]["L1"]]
    assert len(eras) == 10
    assert eras.count("pre1980") == 5, f"eras were {eras}"


def test_no_limitation_is_starved_by_another(monkeypatch):
    """`list(found)[:200]` over a dict filled limitation by limitation gave the first limitation
    the entire budget and every later one nothing. An empty row for claim 7 reads on the page as
    "no such art exists", which is the opposite of what happened."""
    monkeypatch.setattr(LQ, "PER_LIMITATION", 100)
    got = _run_selection({f"L{i}": _rows(f"L{i}", 50) for i in range(4)}, max_total=40)
    per = {}
    for c in got["candidates"]:
        per[c["for_limitation"]] = per.get(c["for_limitation"], 0) + 1
    assert len(per) == 4, f"only {sorted(per)} got any candidates"
    assert min(per.values()) >= 9, per


def test_a_family_is_offered_once(monkeypatch):
    monkeypatch.setattr(LQ, "PER_LIMITATION", 100)
    rows = _rows("L1", 5)
    for r in rows["2015_now"]:
        r["fam"] = "SAME"
    got = _run_selection({"L1": rows}, max_total=100)
    assert sum(1 for c in got["candidates"] if c["fam"] == "SAME") == 1


def _run_selection(by_era, max_total):
    """Drive search()'s selection without BigQuery, by feeding it the rows a query would return."""
    rows = []
    for lim_id, eras in by_era.items():
        for era_rows in eras.values():
            for i, r in enumerate(era_rows):
                rows.append({"q": f"q{sorted(by_era).index(lim_id)}", "pub": r["pub"],
                             "pd": r["publication_date"], "family_id": r["fam"],
                             "title": r["title"], "abstract": r["abstract"], "era": r["era"],
                             "score": r["score"], "n_cooc": 1, "in_claims": False,
                             "in_title": False, "in_class": False, "rank_era": i + 1,
                             "rank_q": i + 1, "pool_q": len(era_rows)})
    plan = {lim: dict(PLAN["claim 1[c]"]) for lim in sorted(by_era)}
    import types
    fake_bq = types.SimpleNamespace(run_guarded=lambda *a, **k: (rows, 1.0, 1.0))
    fake_ws = types.SimpleNamespace(QUERY_CEILING_GB=400.0)
    import sys
    sys.modules["bqclient"], saved_bq = fake_bq, sys.modules.get("bqclient")
    sys.modules["worldset"], saved_ws = fake_ws, sys.modules.get("worldset")
    try:
        return LQ.search([{"id": k, "text": "t"} for k in sorted(by_era)], "p.d.t",
                         plan=plan, max_total=max_total, log=lambda *a: None)
    finally:
        for name, saved in (("bqclient", saved_bq), ("worldset", saved_ws)):
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved
