"""Research on a draft: one control, four depths, all of them the product's own search.

WHY THIS REPLACED THREE BUTTONS
    The studio grew three ways to look for prior art, side by side in the Sources tab: a quick
    pass, a re-search round, and "search the current draft". They read as three names for one
    thing, they reported in three different vocabularies, and two of them could be running at
    once against the same draft with neither able to say what the other had found. The answer was
    never a fourth one. It is ONE control with an effort setting, and results that look like every
    other search this product runs, because they ARE every other search this product runs.

WHAT THE SETTING ACTUALLY CHANGES
    Nothing here reimplements retrieval, ranking or reading. Every level hands `ensure_report` the
    same kind of work the front page hands it, and differs in exactly two things: how much of the
    draft becomes the query, and which depth tier the pipeline runs. That is why a research run
    has a slug, appears in the user's history, opens as a full report, and renders through the
    same card markup: it is not "like" a search, it is one.

        scan     one 30-word blurb of the invention, quick tier, local corpus
        find     the whole draft, quick tier, local corpus
        ledger   the whole draft, ledger tier: every retrieved passage checked against every
                 requirement
        full     the whole draft, deep tier, federated: each reference read in full and charted
                 claim by claim

    THE ETAs ARE MEASURED ON THIS CORPUS, not the pipeline's published ones. `search_profile`
    says the quick tier is 60 to 120 seconds; timed end to end on 2026-09-01 against the 5M
    publication corpus, Scan took 288s and Find took 277s. Those published numbers predate this
    corpus, and a control that promises a minute and takes five is the thing this docstring was
    written to avoid, so the labels say five.

    The measurement also says something worth printing: SCAN AND FIND COST THE SAME. Both are the
    quick tier, and the tier's time goes on retrieval and screening rather than on the length of
    the query, so the cheapest stop is not cheaper. It is a DIFFERENT QUESTION: one sentence finds
    a different neighbourhood than the whole draft does, which is worth having and is not worth
    pretending is faster.

THE ONE THING THE LEVELS DO NOT DO
    They do not measure novelty except at the deepest tier, because only that tier builds a claim
    chart. A ranked list is a ranked list; calling the top of it "the closest art to your claims"
    when nothing has read it against the claims would be the same overclaim the quick pass had to
    be corrected for. Each level says what it did and what it did not do, in its own words, and
    the page prints that rather than a number.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

#  Ordered weakest to strongest; the slider's position IS the index. `depth` and `wide` are the
#  pipeline's own parameters, passed straight through: see webapp.run(), which is the front page
#  making the same call.
LEVELS: tuple[dict[str, Any], ...] = (
    {
        "id": "scan",
        "label": "Scan",
        "depth": "quick",
        "wide": False,
        "material": "essence",
        "reads": False,
        "charts": False,
        "eta": "about 5 minutes",
        "what": "Boils the whole invention down to one sentence and asks the corpus what is "
                "nearest to it. Nothing is read and nothing is measured. It is not cheaper than "
                "Find, measured: it asks a different question, and one sentence lands in a "
                "different neighbourhood than a whole application does.",
    },
    {
        "id": "find",
        "label": "Find",
        "depth": "quick",
        "wide": False,
        "material": "draft",
        "reads": False,
        "charts": False,
        "eta": "about 5 minutes",
        "what": "The search the front page runs. Breaks the draft into its requirements, runs a "
                "set of queries rather than one, and ranks what comes back with the passage that "
                "matched each requirement. Still reads nothing in full.",
    },
    {
        "id": "ledger",
        "label": "Ledger",
        "depth": "ledger",
        "wide": False,
        "material": "draft",
        "reads": False,
        "charts": False,
        "eta": "5 to 20 minutes",
        "what": "Takes every passage the search retrieved and checks it against every requirement "
                "of your claims, so the ranking is by what a reference actually reaches rather "
                "than by how near its text sits. Reads no document in full.",
    },
    {
        "id": "full",
        "label": "Full reading",
        "depth": "deep",
        "wide": True,
        "material": "draft",
        "reads": True,
        "charts": True,
        "eta": "20 minutes to an hour",
        "what": "The whole attack, and the tier a third-party observation is written from. Adds "
                "the external patent APIs, fetches the specifications, reads each of the closest "
                "references in full, and charts it claim element by claim element with the "
                "passage that discloses each one. This is the only level that measures how much "
                "of your independent claims the nearest single reference reaches.",
    },
)
BY_ID = {level["id"]: level for level in LEVELS}
DEFAULT = "find"


def level(level_id: str) -> dict[str, Any]:
    found = BY_ID.get(str(level_id or "").strip().lower())
    if not found:
        raise ValueError(f"{level_id!r} is not a research level.")
    return found


def public() -> list[dict[str, Any]]:
    """The slider, for a page that has to explain what each stop costs before it is pulled."""
    return [{key: item[key] for key in
             ("id", "label", "what", "eta", "reads", "charts", "depth")} for item in LEVELS]


# =============================================================================================
# What becomes the query
# =============================================================================================
#  The whole draft, in the order a searcher would read it. Cut at 20k because the slug is derived
#  from the query text and the pipeline embeds it; a 200k disclosure would be neither.
MAX_QUERY_CHARS = 20_000
MIN_QUERY_CHARS = 40


def draft_query(sections: Mapping[str, Any], fallback_title: str = "") -> str:
    blocks = [str(sections.get("title") or fallback_title or "").strip(),
              str(sections.get("summary") or "").strip(),
              str(sections.get("claims") or "").strip(),
              str(sections.get("detailed_description") or "").strip()[:9000]]
    return "\n\n".join(block for block in blocks if block)[:MAX_QUERY_CHARS]


def essence_query(full_query: str, title: str = "") -> str:
    """One sentence describing the invention, for the cheapest level.

    `query_set` already produces this and has the measurement behind it: on the live corpus a
    30-word essence sentence put a reference the searcher named as highly relevant at dense rank
    2, where the full brief put it at 35 and claim 1 verbatim did not place it in the top 1,000.
    So the cheapest level is not a truncated version of the expensive one, it is the query that
    was measured to be the strongest single vector.

    Falls back to the title plus the first line of the summary. A model outage costs the phrasing,
    never the level.
    """
    try:
        import query_set
        specs = query_set.build(full_query, elements=None, claims=None, want_llm=True)
        for spec in specs:
            if spec.kind == "essence" and len(spec.text) >= 20:
                return spec.text
    except Exception:                                          # noqa: BLE001 - never block a run
        pass
    head = " ".join(str(full_query or "").split())
    return (str(title).strip() + ". " + head[:400]).strip(". ").strip() or head[:400]


def material_for(level_id: str, sections: Mapping[str, Any], title: str = "") -> dict[str, str]:
    """The query this level searches on, and a one-line note saying what it is.

    The note is shown with the result. A reader who cannot tell whether they searched a sentence
    or the whole application cannot judge a thin result set, and a thin result set is exactly what
    the cheap level is expected to produce sometimes.
    """
    item = level(level_id)
    full = draft_query(sections, title)
    if len(full) < MIN_QUERY_CHARS:
        raise ValueError("The draft does not have enough technical detail to search from yet. "
                         "Wait for the first version.")
    if item["material"] == "essence":
        text = essence_query(full, title)
        return {"query": text,
                "note": "searched on a one-sentence summary of the invention"}
    return {"query": full,
            "note": "searched on the title, summary, claims and description"}


# =============================================================================================
# Handing a finished run to the drafting agent
# =============================================================================================
def redraft_request(*, label: str, level_id: str, slug: str, note: str,
                    references: Sequence[Mapping[str, Any]],
                    reading: Mapping[str, Any] | None = None) -> str:
    """What "Use to redraft" says to the drafting agent.

    THE POINT OF THE BUTTON is that a result which changes nothing is a result nobody acts on. A
    search that finishes and sits there is how a drafter ends up filing an application that the
    search already told them was anticipated. So this is not "here is some art": it names each
    reference, says what the search established about it and, at the deepest level, which
    elements of the independent claims nothing disclosed, which is the ground the claims should be
    built on.

    It is careful about ONE thing above all: what warrant the numbers carry. A quick tier ranked
    these references by text; it did not read them. Telling an agent that the top of a dense
    ranking "discloses" a limitation would put a fabricated concession into an application, so
    the tiers that did not read say so in as many words and instruct the agent to read the
    documents itself.
    """
    item = level(level_id)
    lines = [
        f"A prior-art search has just finished on the CURRENT draft. Level: {label} "
        f"({item['eta']}), search id {slug}, {note}.",
        "",
        f"The {len(references)} nearest references it found are now attached to this project. "
        "They are in prior_art/ with their text, and prior_art/INDEX.md lists the citation key "
        "for each one.",
        "",
    ]
    if references:
        lines.append("WHAT IT FOUND, nearest first:")
        for index, reference in enumerate(references, 1):
            publication = str(reference.get("publication_number") or "")
            title = str(reference.get("title") or "untitled")[:120]
            date = str(reference.get("publication_date") or "")[:10]
            lines.append(f"  {index}. {publication}{f' ({date})' if date else ''} - {title}")
            why = " ".join(str(reference.get("relevance_summary") or "").split())[:400]
            if why:
                lines.append(f"     {why}")
        lines.append("")

    if item["charts"] and reading and reading.get("ok"):
        elements = int(reading.get("n_elements") or 0)
        closest = float(reading.get("closest_coverage") or 0.0)
        lines += [
            f"MEASURED AGAINST YOUR INDEPENDENT CLAIMS. This level read each reference in full "
            f"and charted it. The nearest single reference is {reading.get('closest_pub')} "
            f"({reading.get('closest_title') or 'untitled'}), which discloses "
            f"{int(round(closest * elements))} of their {elements} elements ({closest:.0%}).",
            "",
        ]
        uncovered = list(reading.get("uncovered_elements") or [])
        if uncovered:
            lines += ["NO reference disclosed these elements. They are the strongest ground the "
                      "independent claims have, so build on them rather than adding new "
                      "limitations. They are quoted in the SEARCH's words, not the inventor's: "
                      "treat each as a pointer to look in input/disclosure.md, never as text to "
                      "import:"]
            lines += [f"  - {text}" for text in uncovered[:12]]
        else:
            lines.append("Every element of the independent claims was disclosed by something in "
                         "this art. The claims need a feature that is in the inventor's "
                         "disclosure and is in none of it.")
        lines.append("")
    elif item["reads"]:
        lines += ["This level read the references in full but produced no claim chart, so nothing "
                  "here is a measurement. Read them yourself before you concede anything.", ""]
    else:
        lines += [
            "WHAT THIS LEVEL DID NOT DO. It ranked these references by their text against yours. "
            "It did NOT read them in full and it did NOT chart them against your claims, so "
            "nothing above says that any of them discloses any limitation. Read every one in "
            "prior_art/ before you change a claim on account of it, and if the reading disagrees "
            "with the ranking, the reading wins.",
            "",
        ]

    lines += [
        "Do this, in this order:",
        "  1. Read prior_art/INDEX.md and then every file it lists.",
        "  2. Run `python3 tools/novelty_check.py`. It charts the current independent claims "
        "element by element against every attached reference and names the nearest single "
        "reference and the elements nothing was found to disclose. That is the measurement to "
        "move, and it is the same one the page shows the inventor.",
        "  3. Amend each independent claim so it recites, in its own terms, at least one concrete "
        "feature or relationship that NONE of these references discloses and that does real "
        "technical work. Search again with tools/prior_art_search.py on the feature you choose "
        "before you commit to it: the art nearest the amended wording is not the art nearest the "
        "original.",
        "  4. Make the detailed description support that feature in the same words, and say what "
        "technical problem it solves, because that explanation is what a later argument is built "
        "from.",
        "  5. Address every reference above in the Background, accurately, and CITE IT THERE with "
        "`[REF:KEY]` using exactly the key in prior_art/INDEX.md. A reference discussed without "
        "its citation token is invisible to the citation check and to the IDS. Put each citation "
        "where the text actually relies on the reference, not in a list at the end.",
        "  6. Run the novelty check again on the amended claims and put both figures, before and "
        "after, in your reply. Then publish.",
        "",
        "IF THE DISCLOSURE SUPPORTS NO DISTINCTION from a reference that reaches most of a claim, "
        "do not narrow: write what the inventor would have to add as a proposal in "
        "review/proposals.md (one `## heading` per proposal: the feature, why it clears which "
        "reference, what the inventor must confirm). It appears on the page for the inventor to "
        "adopt, and adopted text becomes part of the disclosure.",
        "",
        "DO NOT buy distance from the art with scope. Narrowing claim 1 until nothing reads on it "
        "is always available and is almost always the wrong trade: add the distinguishing feature "
        "the disclosure supports, do not pile on limitations.",
        "",
        "DO NOT INVENT SUPPORT FOR THE FEATURE YOU CHOOSE. Introduce no new numbered part, "
        "passage, region, port, chamber or surface, and no new definition, sign convention or "
        "reference point. Every numeral you rely on must already be in draft/numerals.md and "
        "trace to an affirmative passage in input/disclosure.md. What you MAY do is recite an "
        "element the disclosure already has, more precisely, in the disclosure's own words.",
        "",
        "AND THE OPTION THAT IS NOT A FAILURE: if the disclosure supports no further distinction, "
        "say exactly that in your reasoning, name what the inventor would have to add, and leave "
        "the claims alone. A report that the art is close is worth more than one that invents its "
        "way past it, because the second cannot be filed.",
        "",
        "In `reasoning`, say which feature now carries which independent claim clear of which "
        "specific reference.",
    ]
    return "\n".join(lines)
