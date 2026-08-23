"""Render dispatch, and the SVG -> PDF/PNG exports.

The SVG is the artifact. The PDF and the PNG are conversions of that exact file, never
independently drawn, so the three can never disagree about what the figure contains — which
matters because the PNG is what the vision verifier reads and the PDF is what gets filed.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ..profiles import DrawingProfile
from ..schemas import LayoutScene
from . import block, common, flowchart, mechanical

RENDERER_VERSION = common.RENDERER_VERSION

_DIAGRAM = {"block_diagram", "data_flow", "logical_schematic", "network_topology",
            "state_diagram", "sequence_diagram", "other"}
_PHYSICAL = {"mechanical_schematic", "exploded_schematic", "ui_schematic"}


class ExportUnavailable(RuntimeError):
    """No converter on this machine can turn the SVG into the requested format."""


def render_svg(scene: LayoutScene, profile: DrawingProfile) -> str:
    if scene.figure_type == "flowchart":
        return flowchart.render(scene, profile)
    if scene.figure_type in _PHYSICAL:
        return mechanical.render(scene, profile)
    return block.render(scene, profile)


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------
def _cairosvg():
    try:
        import cairosvg
    except Exception as exc:  # pragma: no cover - deployment dependent
        raise ExportUnavailable(f"cairosvg is not installed: {exc}") from exc
    return cairosvg


def svg_to_pdf(svg: str, profile: DrawingProfile) -> bytes:
    """Vector PDF at the profile's real sheet size."""
    return _cairosvg().svg2pdf(
        bytestring=svg.encode("utf-8"),
        output_width=profile.mm(profile.sheet_width) * 72.0 / 25.4,
        output_height=profile.mm(profile.sheet_height) * 72.0 / 25.4)


def svg_to_png(svg: str, profile: DrawingProfile, dpi: int = 200) -> bytes:
    """Raster of the same SVG.

    The vision verifier reads this, so the resolution has to be enough for a 3.2 mm numeral to
    be legible: at 200 dpi that numeral is about 25 pixels tall, which is comfortably readable
    and keeps a full sheet under a megabyte.
    """
    scale = dpi / 25.4
    return _cairosvg().svg2png(
        bytestring=svg.encode("utf-8"),
        output_width=int(profile.mm(profile.sheet_width) * scale),
        output_height=int(profile.mm(profile.sheet_height) * scale),
        background_color="#ffffff")


def png_via_pdf(svg: str, profile: DrawingProfile, dpi: int = 200) -> bytes:
    """Fallback raster path for a host with poppler but no cairosvg bindings."""
    if not shutil.which("pdftoppm"):
        raise ExportUnavailable("neither cairosvg nor pdftoppm is available")
    pdf = svg_to_pdf(svg, profile)
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "figure.pdf"
        source.write_bytes(pdf)
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-singlefile",
                        str(source), str(Path(tmp) / "out")],
                       check=True, capture_output=True, timeout=120)
        return (Path(tmp) / "out.png").read_bytes()


def export_all(svg: str, profile: DrawingProfile, destination: Path, stem: str,
               dpi: int = 200) -> dict[str, str]:
    """Write ``<stem>.svg`` and, where a converter exists, the PDF and PNG beside it."""
    destination.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    svg_path = destination / f"{stem}.svg"
    svg_path.write_text(svg, encoding="utf-8")
    written["svg"] = svg_path.name
    try:
        (destination / f"{stem}.pdf").write_bytes(svg_to_pdf(svg, profile))
        written["pdf"] = f"{stem}.pdf"
    except Exception:
        pass
    try:
        (destination / f"{stem}.png").write_bytes(svg_to_png(svg, profile, dpi))
        written["png"] = f"{stem}.png"
    except Exception:
        try:
            (destination / f"{stem}.png").write_bytes(png_via_pdf(svg, profile, dpi))
            written["png"] = f"{stem}.png"
        except Exception:
            pass
    return written


__all__ = ["ExportUnavailable", "RENDERER_VERSION", "export_all", "png_via_pdf",
           "render_svg", "svg_to_pdf", "svg_to_png"]
