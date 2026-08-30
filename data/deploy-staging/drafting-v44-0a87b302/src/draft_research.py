"""Re-search: search from the draft, measure how close the art is, then draft away from it.

THE LOOP, AND THE CLAIM IT MAKES
    current draft -> prior-art search -> read the claim chart -> attach what it found
                  -> a drafting turn that is TOLD the measurement and told to move
                  -> next round searches the draft it produced

The claim being made is falsifiable and is the reason every round stores a number: if the drafting
agent is doing its job, the nearest single reference the NEXT search can find should disclose less
of the independent claims than this one did. If it does not, the loop is not working and the rounds
table says so in as many words rather than leaving it to impression.

WHAT MAKES A ROUND HONEST
  * The search query is built from the draft as it stands, never from the previous query, so a
    round cannot inherit the last round's blind spot.
  * The measurement comes from the report's own claim chart, the same evidence the user can open
    and read, not from a second opinion invented here.
  * The drafting turn is given the number, the nearest reference, and the elements NOTHING
    disclosed. That last one is what it anchors to; without it the agent narrows blindly, which
    buys a lower score by giving away scope and is the failure mode this design has to avoid.
  * Nothing here decides patentability. It measures what this search found against these claims.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from typing import Any, Callable, Mapping, Sequence

import draft_novelty

try:
    import db
except ModuleNotFoundError:                    # the reading tests need no driver
    db = None                                  # type: ignore[assignment]

#  How long to wait for a search to finish before giving up on the round. Deep searches on this
#  corpus run 11 to 46 minutes; the cap is generous because the cost of abandoning a round that was
#  nearly done is another full search.
SEARCH_TIMEOUT_SECONDS = max(600, int(__import__("os").environ.get(
    "DRAFT_RESEARCH_TIMEOUT", "5400")))
POLL_SECONDS = 20.0
#  References attached per round. Enough for the agent to have something to work around, few enough
#  that twenty rounds do not bury the draft in a hundred documents it must all cite in the
#  Background.
IMPORT_PER_ROUND = 5

_RUNNING: set[int] = set()


# =============================================================================================
# Persistence
# =============================================================================================
def _cursor(**kwargs: Any):
    if db is None:
        raise RuntimeError("The Postgres driver is required for research rounds.")
    return db.cursor(**kwargs)


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value if value is not None else fallback


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["reading"] = _json(out.get("reading"), {})
    return out


def rounds(project_id: int, *, limit: int = 40) -> list[dict[str, Any]]:
    with _cursor() as cur:
        cur.execute("SELECT * FROM app_draft_research_rounds WHERE project_id=%s "
                    "ORDER BY round_no DESC LIMIT %s", (int(project_id), int(limit)))
        return [_row(row) for row in cur.fetchall()]


def active_round(project_id: int) -> dict[str, Any] | None:
    with _cursor() as cur:
        cur.execute("SELECT * FROM app_draft_research_rounds WHERE project_id=%s "
                    "AND status NOT IN ('complete','failed') ORDER BY round_no DESC LIMIT 1",
                    (int(project_id),))
        row = cur.fetchone()
    return _row(row) if row else None


def open_round(project_id: int, *, version_no: int, slug: str) -> dict[str, Any]:
    with _cursor() as cur:
        cur.execute("SELECT coalesce(max(round_no),0)+1 AS n FROM app_draft_research_rounds "
                    "WHERE project_id=%s", (int(project_id),))
        round_no = int(cur.fetchone()["n"])
        cur.execute(
            "INSERT INTO app_draft_research_rounds (project_id,round_no,version_no,slug,status) "
            "VALUES (%s,%s,%s,%s,'searching') RETURNING *",
            (int(project_id), round_no, int(version_no), str(slug)[:120]))
        return _row(dict(cur.fetchone()))


def update_round(round_id: int, **values: Any) -> None:
    allowed = ("status", "imported_count", "closest_coverage", "mean_top3", "combination",
               "n_elements", "n_charted", "closest_pub", "closest_title", "turn_id", "note")
    fields = {key: value for key, value in values.items() if key in allowed}
    sets = ", ".join(f"{key}=%s" for key in fields)
    args: list[Any] = list(fields.values())
    if "reading" in values:
        sets = (sets + ", " if sets else "") + "reading=%s::jsonb"
        args.append(json.dumps(values["reading"], ensure_ascii=False, default=str))
    if not sets:
        return
    args.append(int(round_id))
    with _cursor() as cur:
        cur.execute(f"UPDATE app_draft_research_rounds SET {sets}, updated_at=now() "
                    f"WHERE id=%s", args)


# =============================================================================================
# The request put to the drafting agent
# =============================================================================================
def drafting_request(round_no: int, reading: Mapping[str, Any],
                     references: Sequence[Mapping[str, Any]],
                     previous: Mapping[str, Any] | None = None) -> str:
    """What round N tells the drafting agent, including the number it has to move.

    Telling the agent the measurement is the whole mechanism. Told only "here is more prior art",
    it tidies the Background and leaves the claims where they were; told which reference is nearest
    and which elements nothing disclosed, it has somewhere concrete to take the independent claims.
    The instruction NOT to buy the number with scope is explicit for the same reason: narrowing
    claim 1 until nothing reads on it always lowers the score and is almost always the wrong trade.
    """
    lines = [
        f"A fresh prior-art search was run from the CURRENT draft. This is re-search round "
        f"{round_no}, and the {len(references)} closest references it found are now attached to "
        f"this project.",
        "",
    ]
    if reading.get("ok"):
        elements = int(reading.get("n_elements") or 0)
        closest = float(reading.get("closest_coverage") or 0.0)
        lines += [
            f"MEASURED AGAINST THE CURRENT INDEPENDENT CLAIMS: the nearest single reference is "
            f"{reading.get('closest_pub')} ({reading.get('closest_title') or 'untitled'}), which "
            f"discloses {int(round(closest * elements))} of their {elements} elements "
            f"({closest:.0%}).",
        ]
        if previous and previous.get("closest_coverage") is not None:
            lines.append(
                f"Last round that figure was {float(previous['closest_coverage']):.0%}. The point "
                f"of this turn is to make the NEXT search find less, not to explain this one.")
        uncovered = list(reading.get("uncovered_elements") or [])
        if uncovered:
            lines += ["", "NO reference disclosed these elements. They are the strongest ground "
                          "the independent claims have, so build on them rather than adding new "
                          "limitations:"]
            lines += [f"  - {item}" for item in uncovered[:12]]
        else:
            lines += ["", "Every element of the independent claims was disclosed by something in "
                          "the attached art. The claims need a feature that is in the disclosure "
                          "and is not in any of it."]
    else:
        lines.append("The search charted no reference against the claims, so there is no "
                     "measurement this round. Read the attached references and address them.")
    lines += [
        "",
        "Do this:",
        "  1. Read every newly attached reference in prior_art/ before changing anything.",
        "  2. Amend the independent claims so each recites, in its own terms, at least one "
        "concrete feature or relationship that NO attached reference discloses, and that does "
        "real technical work. Prefer a feature the search already showed nothing disclosed.",
        "  3. Make sure the detailed description supports that feature in the same words, and "
        "explain the technical problem it solves, because that explanation is what a later "
        "argument is built from.",
        "  4. Address the new references accurately in the Background.",
        "",
        "DO NOT buy a lower score with scope. Narrowing claim 1 until nothing reads on it is "
        "always available and is almost always the wrong trade: add the distinguishing feature "
        "the disclosure supports, do not pile on limitations. If the honest answer is that the "
        "art is close and the disclosure supports no further distinction, say so plainly in your "
        "reasoning and leave the claim alone.",
        "",
        "In `reasoning`, say which feature now carries which independent claim clear of which "
        "specific reference.",
    ]
    return "\n".join(lines)


# =============================================================================================
# Running a round
# =============================================================================================
def run_round(*, project_id: int, user_id: int, round_id: int, slug: str,
              load_view: Callable[[str], Mapping[str, Any] | None],
              is_ready: Callable[[str], bool],
              attach: Callable[[str, Sequence[str]], int],
              enqueue: Callable[[str], int | None],
              previous: Mapping[str, Any] | None = None,
              timeout: float = SEARCH_TIMEOUT_SECONDS,
              poll: float = POLL_SECONDS) -> dict[str, Any]:
    """Wait for the search, measure it, attach what it found, and raise the drafting turn.

    Every collaborator is injected because the search pipeline, the reference store and the turn
    queue all live behind the web application's own wiring; passing them in keeps this module
    testable without a Flask app, a Postgres corpus or a model.
    """
    deadline = time.time() + timeout
    update_round(round_id, status="searching")
    while time.time() < deadline:
        try:
            if is_ready(slug):
                break
        except Exception:                                      # noqa: BLE001 - a poll may blip
            traceback.print_exc()
        time.sleep(poll)
    else:
        update_round(round_id, status="failed",
                     note="The search did not finish inside its time budget.")
        return {"ok": False, "reason": "timeout"}

    update_round(round_id, status="reading")
    view = load_view(slug) or {}
    reading = draft_novelty.read_view(view)
    #  The references worth attaching are the ones the chart says came closest, not simply the top
    #  of the ranked list: the point of the round is to write around what is actually near.
    ranked = sorted((reading.get("per_reference") or {}).items(),
                    key=lambda kv: (-kv[1], kv[0]))
    pubs = [pub for pub, _ in ranked[:IMPORT_PER_ROUND]]
    if not pubs:
        pubs = [str(card.get("pub")) for card in (view.get("cards") or [])[:IMPORT_PER_ROUND]
                if card.get("pub")]
    update_round(
        round_id, status="attaching", reading=reading,
        closest_coverage=reading.get("closest_coverage"),
        mean_top3=reading.get("mean_top3"), combination=reading.get("combination"),
        n_elements=int(reading.get("n_elements") or 0),
        n_charted=int(reading.get("n_charted") or 0),
        closest_pub=str(reading.get("closest_pub") or "")[:80],
        closest_title=str(reading.get("closest_title") or "")[:400])

    imported = 0
    try:
        imported = int(attach(slug, pubs))
    except Exception as exc:                                   # noqa: BLE001
        traceback.print_exc()
        update_round(round_id, note=f"Could not attach references: {str(exc)[:300]}")

    titles = draft_novelty.titles_from_view(view)
    references = [{"publication_number": pub, "title": titles.get(pub, "")} for pub in pubs]
    with _cursor() as cur:
        cur.execute("SELECT round_no FROM app_draft_research_rounds WHERE id=%s", (int(round_id),))
        round_no = int((cur.fetchone() or {}).get("round_no") or 1)

    turn_id = None
    try:
        update_round(round_id, status="drafting", imported_count=imported)
        turn_id = enqueue(drafting_request(round_no, reading, references, previous))
    except Exception as exc:                                   # noqa: BLE001
        traceback.print_exc()
        update_round(round_id, status="failed",
                     note=f"Could not start the drafting turn: {str(exc)[:300]}")
        return {"ok": False, "reason": "enqueue", "reading": reading}

    update_round(round_id, status="complete", turn_id=turn_id, imported_count=imported)
    return {"ok": True, "reading": reading, "imported": imported, "turn_id": turn_id}


def run_round_in_background(**kwargs: Any) -> threading.Thread:
    project_id = int(kwargs["project_id"])

    def _go() -> None:
        try:
            run_round(**kwargs)
        except Exception:                                      # noqa: BLE001 - never kill the thread
            traceback.print_exc()
            try:
                update_round(int(kwargs["round_id"]), status="failed",
                             note="The round stopped on an unexpected error.")
            except Exception:                                  # noqa: BLE001
                pass
        finally:
            _RUNNING.discard(project_id)

    _RUNNING.add(project_id)
    thread = threading.Thread(target=_go, name=f"draft-research-{project_id}", daemon=True)
    thread.start()
    return thread


def is_running(project_id: int) -> bool:
    return int(project_id) in _RUNNING


def series(project_id: int) -> dict[str, Any]:
    """The rounds as a series, with the verdict this feature has to be judged on."""
    history = sorted(rounds(project_id), key=lambda item: item["round_no"])
    measured = [item for item in history if item.get("closest_coverage") is not None]
    out = {"rounds": history, "measured": len(measured)}
    if len(measured) >= 2:
        first, last = measured[0], measured[-1]
        out["delta"] = round(float(last["closest_coverage"]) -
                             float(first["closest_coverage"]), 4)
        out["improvement"] = draft_novelty.improvement(first, last)
    return out
