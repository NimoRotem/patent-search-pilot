You are looking at a patent line drawing and locating the parts in it.

You will be given the drawing and a list of the parts it is supposed to show, in the words the
patent uses for them. For each part, give the box that encloses it.

Report boxes as `[x0, y0, x1, y1]` on a 0 to 1000 scale, where 0,0 is the top left of the image
and 1000,1000 is the bottom right.

Rules:

1. **One box per part, and only for parts you can actually see.** If you cannot find a part,
   leave it out of the list. Guessing a box puts a reference numeral on empty paper, which is
   worse than reporting the part as missing.
2. **The box is the part, not the region around it.** An enclosing part — a housing, a frame, a
   body — gets the box of its own outline, which will contain the other parts. Say so by setting
   `encloses_others` to true.
3. **Report every piece of text you can see on the drawing**, in `visible_text`. Digits, letters,
   captions, anything. If there is none, return an empty list. This matters: the drawing is
   supposed to carry no text at all, and anything you find is a defect.
4. **Report anything drawn that is not on the list**, in `unlisted_objects`, described in your
   own words. A drawing that has grown a component nobody asked for is a drawing that has to be
   redone.
5. Where the same kind of part appears more than once and the list names them separately (a
   first plate and a second plate), match them left to right, then top to bottom, and say in
   `note` that is what you did.

Return ONLY JSON:

```
{"parts": [{"name": "<the part's name exactly as given to you>",
            "box": [0, 0, 0, 0], "encloses_others": false, "confidence": 0.0,
            "note": ""}],
 "visible_text": [],
 "unlisted_objects": []}
```
