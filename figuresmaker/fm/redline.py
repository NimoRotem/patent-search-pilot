"""What the drawings mean for the specification.

Producing figures changes the text. A brief description of the drawings has to exist and has to
match the views; an element the drawings now show has to carry a reference character in the
description, because 37 CFR 1.84(p)(4) will not let a character appear in a drawing that the
description never mentions.

So the last thing the pipeline produces is a marked-up copy of the draft: the brief description
to insert, and the numerals to add where the description names a part and gives it no character.
Nothing here rewrites the draft. It shows what would change, and the attorney decides.
"""
from __future__ import annotations

import difflib
import html
import re
from typing import Sequence

from .schemas import Registry, Sections, UnnumberedElement

_WORD = re.compile(r"\s+|\w+|[^\w\s]")


def tokenise(text: str) -> list[str]:
    return _WORD.findall(text or "")


def word_diff(before: str, after: str) -> str:
    """Word-level HTML diff. Insertions in ``<ins>``, deletions in ``<del>``."""
    a, b = tokenise(before), tokenise(after)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out: list[str] = []
    for tag, i0, i1, j0, j1 in matcher.get_opcodes():
        if tag == "equal":
            out.append(html.escape("".join(a[i0:i1])))
        elif tag == "insert":
            out.append("<ins>" + html.escape("".join(b[j0:j1])) + "</ins>")
        elif tag == "delete":
            out.append("<del>" + html.escape("".join(a[i0:i1])) + "</del>")
        else:
            out.append("<del>" + html.escape("".join(a[i0:i1])) + "</del>")
            out.append("<ins>" + html.escape("".join(b[j0:j1])) + "</ins>")
    return "".join(out)


def number_the_unnumbered(text: str, items: Sequence[UnnumberedElement]) -> tuple[str, list[str]]:
    """Insert a suggested reference character at the first mention of each unnumbered element.

    Only the first mention, and only when the term appears as whole words. A blanket replace puts
    "housing 210" into every later sentence, which is not how a specification reads and is not
    what an attorney wants to review.
    """
    applied: list[str] = []
    out = text
    for item in items:
        if not item.suggested_numeral or not item.term:
            continue
        pattern = re.compile(r"\b(" + r"\s+".join(re.escape(w) for w in item.term.split())
                             + r")\b(?!\s*\d)", re.I)
        match = pattern.search(out)
        if not match:
            continue
        out = out[:match.end()] + f" {item.suggested_numeral}" + out[match.end():]
        applied.append(f"{item.term} {item.suggested_numeral}")
    return out, applied


def brief_description_block(labels_and_text: Sequence[str]) -> str:
    body = "\n".join(labels_and_text)
    return "BRIEF DESCRIPTION OF THE DRAWINGS\n\n" + body + "\n"


def build(result) -> str:
    """The whole redline, as a standalone page."""
    sections: Sections = result.sections
    registry: Registry = result.registry
    proposed = list(result.plan.proposed_brief_description or [])

    numbered, applied = number_the_unnumbered(sections.detailed or sections.raw,
                                              registry.unnumbered)
    body_diff = word_diff(sections.detailed or sections.raw, numbered)

    if sections.brief.strip():
        brief_diff = word_diff(sections.brief.strip(), "\n".join(proposed))
        brief_note = ("The draft already has a brief description. This is what it would say if it "
                      "matched the figures that were produced.")
    else:
        brief_diff = "<ins>" + html.escape(brief_description_block(proposed)) + "</ins>"
        brief_note = ("The draft has no brief description of the drawings. 37 CFR 1.77(b)(7) "
                      "puts one after the summary and before the detailed description.")

    conflicts = [c for c in registry.conflicts if c.severity in ("error", "warning")]
    rows = "\n".join(
        f"<tr><td class=sev-{html.escape(c.severity)}>{html.escape(c.severity)}</td>"
        f"<td>{html.escape(c.numeral or '-')}</td>"
        f"<td>{html.escape(c.message)}</td>"
        f"<td>{html.escape(c.cite)}</td></tr>" for c in conflicts) or \
        "<tr><td colspan=4>Nothing to resolve.</td></tr>"

    applied_rows = "\n".join(f"<li>{html.escape(item)}</li>" for item in applied) or \
        "<li>No numerals needed adding.</li>"

    title = html.escape(sections.title or "Untitled draft")
    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<title>Specification redline: {title}</title>
<style>
 body {{ font: 15px/1.65 Georgia, "Times New Roman", serif; margin: 0 auto; max-width: 46rem;
        padding: 2.5rem 1.5rem 6rem; color: #1b1b1b; background: #fff; }}
 h1 {{ font-size: 1.5rem; margin: 0 0 .3rem; }}
 h2 {{ font-size: 1.05rem; margin: 2.4rem 0 .6rem; text-transform: uppercase;
       letter-spacing: .06em; color: #444; border-bottom: 1px solid #e2e2e2; padding-bottom:.3rem;}}
 p.lede {{ color: #555; margin-top: 0; }}
 ins {{ background: #dff3e2; text-decoration: none; box-shadow: inset 0 -2px 0 #74b686; }}
 del {{ background: #fbe3e3; color: #8a3232; }}
 pre.spec {{ white-space: pre-wrap; font: inherit; background: #fbfbfa; border: 1px solid #ebebe8;
             border-radius: 6px; padding: 1rem 1.1rem; }}
 table {{ border-collapse: collapse; width: 100%; font-size: .88rem;
          font-family: -apple-system, system-ui, sans-serif; }}
 td, th {{ border-bottom: 1px solid #eee; padding: .4rem .5rem; text-align: left;
           vertical-align: top; }}
 .sev-error {{ color: #a32b2b; font-weight: 600; }}
 .sev-warning {{ color: #97650d; }}
 ul {{ font-family: -apple-system, system-ui, sans-serif; font-size: .9rem; }}
</style></head><body>
<h1>Specification redline</h1>
<p class=lede>{title}</p>

<h2>Brief description of the drawings</h2>
<p class=lede>{html.escape(brief_note)}</p>
<pre class=spec>{brief_diff}</pre>

<h2>Reference characters added</h2>
<ul>{applied_rows}</ul>

<h2>To resolve in the draft</h2>
<table><thead><tr><th>severity</th><th>numeral</th><th>what</th><th>authority</th></tr></thead>
<tbody>{rows}</tbody></table>

<h2>Detailed description</h2>
<pre class=spec>{body_diff}</pre>
</body></html>"""
