You are extracting the steps of ONE method so a flowchart can be drawn from them.

You are given the figure's own description, the method claims, and the description paragraphs
that set out the process. Every step you return must come from those.

Rules:

1. One step per action the document states. Use the document's own verb.
2. Keep the order the document gives. Do not reorder for tidiness and do not merge two steps
   because they are related.
3. If the document numbers its steps with reference numerals (`receiving the data 502`), carry
   that numeral on the step. If it does not, leave `reference_numeral` empty; do not make one
   up.
4. A step that the document states as a test or a condition ("determining whether ...", "if the
   value exceeds ...") is `decision`, and it must have two outgoing edges labelled with the
   document's own words for the two outcomes, normally "yes" and "no".
5. A loop is an edge that returns to an earlier step. Include it only when the document says
   the process repeats.
6. Every step cites the paragraph or claim it came from, and quotes the words verbatim.
7. Do not add a start or end box unless the document describes one.

Return ONLY JSON:

```
{"steps": [{"id": "step_1", "text": "<the document's own words, imperative or gerund>",
            "reference_numeral": "502", "kind": "process" | "decision" | "terminator",
            "paragraph_id": "p0061", "quote": "<verbatim>"}],
 "edges": [{"from_step": "step_1", "to_step": "step_2", "label": ""}],
 "notes": []}
```
