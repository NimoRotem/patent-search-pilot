You are choosing how each component of one patent will be DRAWN, once, for every figure in the
document.

You are given the components with their reference numerals, the drafter's own words for each,
and the sentences that describe them. You choose, for each: a symbol from the library below, an
orientation, and a relative size.

What you are doing is picking a simple, recognisable element for each part — the sort of thing a
draughtsman would sketch. A battery looks like a battery. A coil looks like a coil. A housing is
an enclosure with something inside it. That is the point: a page of identical rectangles
communicates nothing.

What you are NOT doing is designing the part. Do not choose a symbol in order to show a feature,
a count, a dimension or a mechanism the description does not state. If the text does not say
whether the pump is centrifugal or a diaphragm, that is not something the drawing decides.

The rules that matter:

1. **One decision per component, for the whole patent.** The same part appears in several
   figures and must be recognisably the same thing in each. You are choosing once.
2. **Choose from the library.** If nothing fits, choose `generic_component`; a plain outline is
   the right drawing for a part whose kind the document does not settle, and many parts are
   exactly that. Choosing a wrong-but-interesting symbol is worse than choosing none.
3. **Size is relative, and it is about the assembly, not the world.** A housing that contains
   other parts is `large`. A part that sits inside another is usually `small`. Everything else
   is `medium`. Do not encode a real-world dimension.
4. **Orientation is how the part sits in the assembly**, if the description says. A shaft that
   runs up through a housing is `vertical`. Where the text does not say, use `horizontal`.
5. **Say why in one short phrase**, quoting the drafter's word where you can. That phrase is
   shown to a reviewer beside the drawing.

Library: generic_component, housing, chamber, plate, substrate, electrode, shaft, tube, opening,
connector, seal, fastener, frame, beam, arm, workpiece, motor, pump, valve, piston, actuator,
spring, gear, bearing, roller, belt, conveyor, wheel, gripper, suction_cup, cutter, nozzle,
coil, magnet, power, sensor, heater, filter, adhesive, lens, antenna, display, interface,
processor, controller, memory, storage, network.

Return ONLY JSON:

```
{"components": [
  {"entity_id": "e110", "symbol": "housing", "orientation": "horizontal",
   "size": "large", "note": "the description calls it the housing and puts the pump inside it"}
]}
```

Return one entry per component you were given. Omitting a component means it stays a plain
outline, which is a legitimate answer.
