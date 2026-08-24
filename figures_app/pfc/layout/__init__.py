"""Layout dispatch: one engine per family of figure, never one engine for everything.

A flowchart laid out by a block-diagram solver reads as a graph of boxes rather than as a
method, and a mechanical arrangement laid out by rank asserts a flow the patent never
described. Each figure type is routed to the engine whose assumptions match it, and a type
whose geometry the text cannot supply is refused here rather than drawn badly.
"""
from __future__ import annotations

from ..profiles import DrawingProfile
from ..schemas import TIER_A, TIER_B, TIER_C, FigureSpec, LayoutScene, PatentGraph
from .flow import layout_flowchart
from .graphlayout import layout_graph
from .leaders import place_labels, relocate
from .mech import layout_mechanical

__all__ = ["build_scene", "layout_flowchart", "layout_graph", "layout_mechanical",
           "place_labels", "relocate", "UnsupportedFigure"]

# Diagram figures: topology decides position, and captions go inside the boxes.
_DIAGRAM = {"block_diagram", "data_flow", "logical_schematic", "network_topology",
            "state_diagram", "sequence_diagram"}
# Physical figures: containment decides position, and numerals carry the naming.
_PHYSICAL = {"mechanical_schematic", "exploded_schematic", "ui_schematic"}


class UnsupportedFigure(ValueError):
    """This figure type needs geometry the document does not carry."""


def build_scene(spec: FigureSpec, graph: PatentGraph, profile: DrawingProfile, *,
                sheet_number: int = 1, sheet_total: int = 1, seed: int = 0) -> LayoutScene:
    figure_type = spec.figure_type
    if figure_type == "flowchart":
        scene = layout_flowchart(spec, profile, sheet_number=sheet_number,
                                 sheet_total=sheet_total, seed=seed)
    elif figure_type in _DIAGRAM:
        scene = layout_graph(spec, graph, profile, sheet_number=sheet_number,
                             sheet_total=sheet_total, seed=seed, captions=True)
    elif figure_type in _PHYSICAL:
        scene = layout_mechanical(spec, graph, profile, sheet_number=sheet_number,
                                  sheet_total=sheet_total, seed=seed)
    elif figure_type in TIER_C:
        raise UnsupportedFigure(
            f"A {figure_type.replace('_', ' ')} needs exact geometry, and the description does "
            "not give it. Supply approved geometry, or ask for a schematic view instead.")
    else:
        scene = layout_graph(spec, graph, profile, sheet_number=sheet_number,
                             sheet_total=sheet_total, seed=seed, captions=True)
    return place_labels(scene, profile, seed=seed)
