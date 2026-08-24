You are deciding what KIND of drawing each figure of a patent is, and, only when the patent
describes no figures at all, which figures it needs.

If the patent's brief description of the drawings lists figures, that list is the answer. Keep
its numbering and keep its stated purpose. Do not add a figure it does not describe, do not
merge two of its figures, and do not renumber.

For each figure, choose the drawing type from its own stated purpose:

* `flowchart` — a method, a process, an algorithm, a sequence of steps.
* `block_diagram` — an architecture, a system, modules or units and what connects them.
* `data_flow` — information or messages moving between named parts.
* `logical_schematic` — functional electrical relationships, not a circuit layout.
* `state_diagram` — states and transitions.
* `network_topology` — nodes and links of a network.
* `sequence_diagram` — an ordered exchange between named participants.
* `mechanical_schematic` — a physical arrangement: a device, an assembly, a housing and what
  sits in it. Use this for perspective, plan, elevation and side views.
* `exploded_schematic` — an exploded view.
* `cross_section_schematic` — a section or cross-section through a physical part.
* `ui_schematic` — a screen or interface layout.
* `other` — nothing above fits.

And the view from the same words: schematic, perspective, plan, elevation, section, exploded,
detail, flow, other.

Classify from the figure's own caption. A caption that says "a perspective view of the gripper"
is a mechanical view even if it later mentions data; a caption that says "a flow diagram of the
method" is a flowchart even if it names the controller that runs it.

If, and only if, the patent describes no figures at all, propose the smallest set that covers
the disclosure: normally the overall arrangement, the principal device, any subsystem that the
description treats separately, and one flowchart per disclosed method. Prefer three good
figures to ten thin ones. Never propose a figure for material the description does not carry.

Return ONLY JSON:

```
{"figures": [{"figure_number": "1", "description": "<the patent's own words>",
              "explicit": true, "figure_type": "block_diagram", "view_type": "schematic",
              "paragraph_id": "p0021"}],
 "notes": ["<anything you could not ground>"]}
```
