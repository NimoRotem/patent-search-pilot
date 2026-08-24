"""Patent figure compiler.

A patent draft in, validated vector figures out, with every line on every sheet traceable to a
paragraph of the document that produced it. The design principle throughout is exact semantics
and conservative geometry: the compiler would rather draw a rectangle that is right than a
mechanism that is convincing.
"""
from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
