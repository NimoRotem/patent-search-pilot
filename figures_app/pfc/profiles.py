"""Jurisdiction drawing rules, loaded from versioned files rather than scattered constants.

Official drawing requirements change, and they differ between offices in ways that are easy to
get wrong from memory. Keeping them in ``drawing_profiles/*.yaml`` with a version on each file
means a rule change is a file edit with a diff, and every artifact records which profile
version produced it.

Everything a renderer or validator needs is exposed here already converted into scene units
(``units_per_mm`` units to the millimetre), so no downstream module ever does a unit conversion
and no two of them can disagree about one.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

PROFILE_DIR = Path(__file__).resolve().parents[1] / "drawing_profiles"

# The job configuration names a jurisdiction; the jurisdiction names a profile file. Keeping the
# mapping explicit means an unknown jurisdiction fails loudly instead of silently falling back
# to whatever file happens to sort first.
JURISDICTION_PROFILES = {
    "generic": "generic_v1",
    "uspto_utility": "uspto_utility_v1",
    "pct": "pct_v1",
    "epo": "epo_v1",
}


class ProfileError(ValueError):
    """An unusable drawing profile. Never recovered from with a default."""


class DrawingProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: str
    version: str
    title: str
    file_stem: str

    units_per_mm: float
    sheet_width: float
    sheet_height: float
    margin_top: float
    margin_left: float
    margin_right: float
    margin_bottom: float

    allowed_fonts: tuple[str, ...]
    font_family: str
    min_reference_height: float
    reference_height: float
    caption_height: float
    char_width_ratio: float

    min_line_width: float
    stroke: float
    thin_stroke: float
    arrow_length: float
    arrow_width: float

    min_component_gap: float
    min_label_gap: float
    leader_clearance: float
    min_node_width: float
    min_node_height: float
    rank_gap: float
    sibling_gap: float
    container_padding: float

    monochrome: bool
    allow_component_captions: bool
    label_format: str
    sheet_number_format: str

    raw: dict[str, Any] = Field(default_factory=dict)

    # -- derived geometry ---------------------------------------------------
    @property
    def drawing_left(self) -> float:
        return self.margin_left

    @property
    def drawing_top(self) -> float:
        return self.margin_top

    @property
    def drawing_right(self) -> float:
        return self.sheet_width - self.margin_right

    @property
    def drawing_bottom(self) -> float:
        return self.sheet_height - self.margin_bottom

    @property
    def drawing_width(self) -> float:
        return self.drawing_right - self.drawing_left

    @property
    def drawing_height(self) -> float:
        return self.drawing_bottom - self.drawing_top

    @property
    def version_tag(self) -> str:
        return f"{self.profile_id}_v{self.version}"

    def text_width(self, text: str, height: float | None = None) -> float:
        """Advance width of a string, from the profile's character ratio.

        Deliberately font-metric-free. The renderer and the geometry validator must agree
        exactly on how wide a label is, and the only way to guarantee that without shipping a
        font parser is for both to call this one function.
        """
        h = self.reference_height if height is None else height
        return max(1.0, len(str(text)) * h * self.char_width_ratio)

    def mm(self, units: float) -> float:
        return units / self.units_per_mm


def _need(mapping: dict[str, Any], *path: str) -> Any:
    node: Any = mapping
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise ProfileError(f"drawing profile is missing {'.'.join(path)}")
        node = node[key]
    return node


@functools.lru_cache(maxsize=8)
def load_profile(name: str) -> DrawingProfile:
    """Load a profile by file stem or by jurisdiction name."""
    stem = JURISDICTION_PROFILES.get(str(name), str(name))
    if not stem or not stem.replace("_", "").replace(".", "").isalnum():
        raise ProfileError(f"unknown drawing profile {name!r}")
    path = PROFILE_DIR / f"{stem}.yaml"
    if not path.is_file():
        raise ProfileError(f"unknown drawing profile {name!r}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    upm = float(_need(data, "page", "units_per_mm"))
    if upm <= 0:
        raise ProfileError("units_per_mm must be positive")

    def u(*path: str) -> float:
        return float(_need(data, *path)) * upm

    margins = _need(data, "page", "margins_mm")
    profile = DrawingProfile(
        profile_id=str(_need(data, "profile_id")),
        version=str(_need(data, "version")),
        title=str(data.get("title") or stem),
        file_stem=stem,
        units_per_mm=upm,
        sheet_width=u("page", "width_mm"),
        sheet_height=u("page", "height_mm"),
        margin_top=float(margins["top"]) * upm,
        margin_left=float(margins["left"]) * upm,
        margin_right=float(margins["right"]) * upm,
        margin_bottom=float(margins["bottom"]) * upm,
        allowed_fonts=tuple(str(f) for f in _need(data, "typography", "allowed_fonts")),
        font_family=str(_need(data, "typography", "font_family")),
        min_reference_height=u("typography", "min_reference_height_mm"),
        reference_height=u("typography", "reference_height_mm"),
        caption_height=u("typography", "caption_height_mm"),
        char_width_ratio=float(_need(data, "typography", "char_width_ratio")),
        min_line_width=u("lines", "min_line_width_mm"),
        stroke=u("lines", "stroke_mm"),
        thin_stroke=u("lines", "thin_stroke_mm"),
        arrow_length=u("lines", "arrow_length_mm"),
        arrow_width=u("lines", "arrow_width_mm"),
        min_component_gap=u("spacing", "min_component_gap_mm"),
        min_label_gap=u("spacing", "min_label_gap_mm"),
        leader_clearance=u("spacing", "leader_clearance_mm"),
        min_node_width=u("spacing", "min_node_width_mm"),
        min_node_height=u("spacing", "min_node_height_mm"),
        rank_gap=u("spacing", "rank_gap_mm"),
        sibling_gap=u("spacing", "sibling_gap_mm"),
        container_padding=u("spacing", "container_padding_mm"),
        monochrome=bool(_need(data, "annotations", "monochrome")),
        allow_component_captions=bool(data["annotations"].get("allow_component_captions", True)),
        label_format=str(_need(data, "figure_numbering", "label_format")),
        sheet_number_format=str(_need(data, "figure_numbering", "sheet_number_format")),
        raw=data,
    )
    if profile.reference_height < profile.min_reference_height:
        raise ProfileError("the profile draws numerals below its own stated minimum")
    if profile.stroke < profile.min_line_width:
        raise ProfileError("the profile strokes lines below its own stated minimum")
    if profile.drawing_width <= 0 or profile.drawing_height <= 0:
        raise ProfileError("the profile's margins leave no drawing area")
    return profile


def available_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))
