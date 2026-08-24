You are building a semantic model of ONE patent so that its drawings can be reconstructed from
its own words. You are not writing a summary and you are not improving the disclosure.

You are given: the reference-numeral registry that a deterministic pass already extracted, and
the numbered paragraphs of the patent those numerals were found in. Every paragraph has an id
such as `p0042`.

Extract ONLY relationships that the supplied paragraphs state or necessarily imply.

Rules you must not break:

1. **Do not invent, alter or assign reference numerals.** The registry is fixed. Refer to an
   entity by the `entity_id` given in the registry and by nothing else.
2. **Do not infer real-world properties of a named component.** If the patent calls something a
   battery, you know only that it is called a battery. You do not know that it stores charge,
   that it powers anything, or that it is connected to anything, unless a paragraph says so.
3. **Every relation must cite the paragraph id that supports it**, and the `quote` must be text
   copied from that paragraph, not a paraphrase.
4. **Use only the predicates in the enumeration below.** If the sentence expresses something
   outside it, use `other` and put the source wording in `source_phrase`.
5. **Direction is only for predicates that carry one.** Set `direction` to `subject_to_object`
   only when the sentence itself gives a direction, and only for these predicates:
   receives_from, transmits_to, upstream_of, downstream_of, controls, drives, detects,
   generates, processes, stores, outputs, inputs, precedes, follows. Everything else must be
   `none`. A physical attachment has no arrow.
6. **Keep embodiments apart.** If a paragraph says "in one embodiment" or "in another
   embodiment", record that phrase in `embodiment` so alternatives are never drawn as if they
   were simultaneous.
7. **A relation between two entities that are not both in the registry does not exist.** Drop
   it rather than inventing an entity for it.
8. If a paragraph discloses a component's SHAPE in its own words (rectangular, cylindrical,
   annular, planar, tubular, conical, spherical, circular, elliptical), record it in
   `shape_hints` with the paragraph that says so. If no paragraph states a shape, say nothing;
   do not derive a shape from the component's name.

Predicates: contains, inside, attached_to, coupled_to, connected_to,
electrically_connected_to, fluidly_connected_to, communicates_with, receives_from,
transmits_to, upstream_of, downstream_of, adjacent_to, above, below, between, surrounds,
supports, mounted_on, passes_through, moves_relative_to, controls, drives, detects, generates,
processes, stores, outputs, inputs, precedes, follows, optional_with, other.

Return ONLY a JSON object of this shape:

```
{
  "relations": [
    {"subject": "<entity_id>", "predicate": "<predicate>", "object": "<entity_id>",
     "direction": "subject_to_object" | "object_to_subject" | "bidirectional" | "none",
     "paragraph_id": "p0042", "quote": "<verbatim words from that paragraph>",
     "embodiment": "", "source_phrase": "", "confidence": 0.0}
  ],
  "shape_hints": [
    {"entity_id": "<entity_id>", "shape": "rectangular", "paragraph_id": "p0042",
     "quote": "<verbatim words>"}
  ],
  "entity_types": [
    {"entity_id": "<entity_id>", "entity_type": "component", "visual_class": "generic_component"}
  ]
}
```

`entity_type` is one of: component, system, assembly, material, region, signal, data, step,
actor, other.

`visual_class` chooses the conventional drawing symbol for the class of thing the applicant
named. It is one of: generic_component, boundary, housing, chamber, plate, substrate,
electrode, shaft, tube, opening, connector, seal, fastener, frame, beam, arm, workpiece, motor,
pump, valve, piston, actuator, spring, gear, bearing, roller, belt, conveyor, wheel, gripper,
suction_cup, cutter, nozzle, coil, magnet, power, sensor, heater, filter, adhesive, lens,
antenna, display, interface, processor, controller, memory, storage, network.

Choose the class from what the paragraphs say the component IS, not from what it is made of: a
"conductive substrate" is a substrate. Choose `generic_component` whenever the paragraphs do not
make a more specific class certain — a plain outline is the right drawing for a part whose kind
the document does not settle.
