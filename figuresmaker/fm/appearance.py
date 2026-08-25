"""Making a part look like itself in every view.

37 CFR 1.84(p)(5) is about characters: the same part carries the same reference character in every
view. The drawing convention that goes with it is not written down anywhere as a rule, but an
examiner and a reader both rely on it: the housing in FIG. 4 is recognisably the housing from
FIG. 1. A pipeline that asks a model for each figure independently will not do that. It will make
the housing a box in one view and a cylinder in the next, both perfectly valid, and the drawing
set will be incoherent.

So the first figure that draws a part decides what it is, and every later figure is given that
decision as a constraint. The same goes for the short label printed inside a block, and for the
hatching angle a part is cut with, which has to be the same angle wherever that part is
sectioned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# 37 CFR 1.84(h)(3): oblique parallel lines. Different angles tell adjacent parts apart.
HATCH_ANGLES = (45.0, 135.0, 30.0, 150.0, 60.0, 120.0, 15.0, 165.0, 75.0, 105.0)


@dataclass
class Appearance:
    """One drawing set's memory of how each part is drawn."""
    parts: dict[str, dict[str, Any]] = field(default_factory=dict)
    shapes: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    hatch_order: list[str] = field(default_factory=list)

    # -------------------------------------------------------------------------------- solids

    def remember_solid(self, numeral: str, part: str, params: dict[str, float]) -> None:
        if not numeral or numeral in self.parts:
            return
        self.parts[numeral] = {"part": part, "params": dict(params or {})}

    def constrain_mech(self, scene) -> None:
        """Force every already-seen numeral back to the shape it was first given."""
        for solid in scene.solids or []:
            known = self.parts.get(solid.numeral or "")
            if not known:
                continue
            solid.part = known["part"]
            merged = dict(known["params"])
            # A later figure may legitimately want a different position, never a different part.
            solid.params = merged

    def learn_mech(self, scene) -> None:
        for solid in scene.solids or []:
            self.remember_solid(solid.numeral or "", solid.part, solid.params or {})

    def mech_hint(self, numerals: list[str]) -> str:
        """What to tell the model about parts it has already drawn."""
        rows = []
        for numeral in numerals:
            known = self.parts.get(numeral)
            if not known:
                continue
            params = ", ".join(f"{k}={v:g}" for k, v in sorted(known["params"].items()))
            rows.append(f"{numeral} is already drawn as part={known['part']} {params}".rstrip())
        if not rows:
            return ""
        return ("\n\nPARTS ALREADY FIXED BY AN EARLIER FIGURE. Use exactly these parts and \n"
                "parameters for these numerals; you may place and rotate them differently.\n"
                + "\n".join(rows))

    # --------------------------------------------------------------------------------- graphs

    def constrain_graph(self, scene) -> None:
        for node in scene.nodes or []:
            if node.numeral in self.shapes:
                node.shape = self.shapes[node.numeral]
            if node.numeral in self.labels:
                node.label = self.labels[node.numeral]

    def learn_graph(self, scene) -> None:
        for node in scene.nodes or []:
            if not node.numeral:
                continue
            self.shapes.setdefault(node.numeral, node.shape)
            if node.label:
                self.labels.setdefault(node.numeral, node.label)

    # ------------------------------------------------------------------------------- hatching

    def hatch_angle(self, numeral: str) -> float:
        if numeral not in self.hatch_order:
            self.hatch_order.append(numeral)
        return HATCH_ANGLES[self.hatch_order.index(numeral) % len(HATCH_ANGLES)]

    # ----------------------------------------------------------------------------------- data

    def to_dict(self) -> dict[str, Any]:
        return {"parts": self.parts, "shapes": self.shapes, "labels": self.labels,
                "hatch_order": self.hatch_order}

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]]) -> "Appearance":
        raw = raw or {}
        return cls(parts=raw.get("parts") or {}, shapes=raw.get("shapes") or {},
                   labels=raw.get("labels") or {}, hatch_order=raw.get("hatch_order") or [])
