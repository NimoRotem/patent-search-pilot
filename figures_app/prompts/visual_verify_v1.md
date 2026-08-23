You are reading a patent drawing. Report what is on the sheet.

You are not reviewing the drawing, you are not judging whether it is correct, and you are not
being asked whether it matches anything. Another program will do the comparing. Your one job is
to reconstruct, from the image alone, what a person looking at this sheet would see.

Answer all of the following, in the JSON structure below.

1. Every reference numeral printed on the sheet. Read the digits as printed. Do not correct a
   numeral you think is wrong and do not add one you expect to be there.
2. For each numeral, what its leader line points at: describe the object or region the leader
   ends on, in your own words ("the outer enclosure", "the small rectangle inside the
   enclosure"). If a leader ends in empty space or you cannot tell which object it means, say
   so in `ambiguous_leaders`.
3. Every visible connection between objects: which numeral is at each end.
4. For each connection, whether it carries an arrowhead, and if so which end it is on.
5. Any object drawn on the sheet that carries no reference numeral.
6. Any two labels that overlap or are too close to tell apart.
7. Every piece of text on the sheet, including the figure caption and the sheet number.

If the sheet is blank or you cannot read it, return empty lists rather than guesses.

Return ONLY JSON:

```
{"visible_references": [{"reference": "120", "target_description": "...", "bbox": [0,0,0,0],
                         "confidence": 0.0}],
 "visible_components": [{"observed_id": "obj_1", "description": "...", "bbox": [0,0,0,0],
                         "confidence": 0.0}],
 "connections": [{"from_reference": "120", "to_reference": "130",
                  "direction": "forward" | "backward" | "bidirectional" | "none",
                  "confidence": 0.0}],
 "visible_text": ["120", "FIG. 1"],
 "overlapping_labels": [],
 "ambiguous_leaders": [],
 "possible_errors": []}
```

`direction` is `forward` when the arrowhead is at the `to_reference` end, `backward` when it is
at the `from_reference` end, `bidirectional` when both ends carry one, and `none` when the line
has no arrowhead at all.
