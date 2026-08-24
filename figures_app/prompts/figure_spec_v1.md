You are choosing what ONE figure of a patent shows.

You are given that figure's own description from the patent, the reference numerals the
description associates with it, and the semantic model already extracted from the document
(entities with their numerals, and the relationships between them, each with the paragraph that
supports it).

Select the entities and the relationships this figure must show, and nothing else.

Rules:

1. Choose only from the entities and relations you are given. You cannot add either. If
   something you think the figure needs is absent from the model, say so in `missing` and
   continue.
2. Include an entity when the figure's stated purpose requires it. A figure described as "an
   overall view of the system" takes the top-level parts, not every screw named in the
   description.
3. Include the containing entity when the figure shows things inside it, so that containment
   can be drawn.
4. Do not include an entity merely because it shares a paragraph with one that belongs.
5. Keep the figure legible: prefer at most twelve entities. When the stated purpose genuinely
   needs more, say so in `notes` rather than silently dropping them.
6. `layout_constraints` may only express what the document states: `left_of` for a disclosed
   flow order, `above`/`inside` for a disclosed physical arrangement, `same_rank` for parts the
   text treats as peers. Do not add a constraint for appearance.

Return ONLY JSON:

```
{"entities": [{"entity_id": "<id>", "role": "primary" | "context" | "boundary"}],
 "relations": ["<relation_id>"],
 "layout_constraints": [{"type": "left_of", "a": "<entity_id>", "b": "<entity_id>"}],
 "title": "<short title from the patent's own caption>",
 "missing": ["<what the figure's description needs that the model does not carry>"],
 "notes": []}
```

`boundary` marks an entity that is drawn as the outline containing the others, such as a system
or a housing. Use it for at most one entity.
