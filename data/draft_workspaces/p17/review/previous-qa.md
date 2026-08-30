# Previous review - verdict: fail

3 drawing issues require automatic repair.

## Mechanical checks that did not pass

- **Every drawing sheet passes geometry, leader, and OCR inspection** (fail): 3 drawing issues failed. Each failure is listed below so the next repair can address the full set.
  - FIG. 2: OCR label review failed: duplicates 50
  - FIG. 7: semantic drawing review failed: The workpiece is shown with a defined rectangular boundary, but the specification states 'showing no edges of the workpiece'.; The centerline is visible where it passes under the rail, but the specification states 'The rail lies on top of the workpiece and hides the part of the centerline beneath it. The centerline is therefore shown only on the parts of the workpiece not covered by the rail'.; The specification requires a ragged break line only on the left side of the rail, but the drawing shows a ragged break line on the right side of the rail as well.; The specification requires the longitudinal slot to be 'shown schematically', but the drawing depicts it with a specific rounded end, which may not be considered purely schematic if a simpler representation was intended.; Unexpected geometry: Inner rectangular boundary of the workpiece; Unexpected geometry: Portion of the centerline visible under the rail; Unexpected geometry: Ragged break line on the right side of the rail; Unexpected geometry: Drawing area border; Unexpected geometry: Outer rectangular boundary of the drawing area: The outermost black rectangular frame of the image.; Unexpected geometry: Inner rectangular bounda
  - FIG. 8: semantic drawing review failed: The cut surfaces of the rail (10) and the second guide carriage (70) are hatched in the same direction, but the specification requires them to be hatched in different directions to distinguish them.; The specification requires the rail (10) and the second guide carriage (70) to have hatching in different directions, but they are shown with hatching in the same direction.; The part identified as the drill bushing (74) is shown with the same hatching as the surrounding second guide carriage (70), indicating they are a single part, which contradicts the specification listing them as separate components.

Fix every listed item before returning. If an advisory is a false positive, make
the wording or figure specification unambiguous enough that the check passes.
