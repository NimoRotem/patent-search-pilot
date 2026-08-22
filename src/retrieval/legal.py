"""The citability filter that is AND-ed into every candidate query.

Date/status filtering (spec §5) is not a nicety bolted on after retrieval: a document that is not
prior art against this subject under this mode is not a candidate at all, so the window is pushed
down into the SQL of every channel. The rules themselves live in `search_modes`; this module is
only the SQL adapter, plus the one rule that has no home there, a subject is never prior art
against itself.
"""
from __future__ import annotations

from search_modes import Mode, citable_where          # re-exported: callers import both from here

from .family import family_id_sql


def _date_clause(subject, mode, alias="p"):
    """(where_fragment, params) restricting to citable prior art; ('', []) if no subject/mode."""
    if subject is None or mode is None:
        return "", []
    frag, params = citable_where(mode, subject, alias)
    # never return the subject's own family as prior art
    if subject.number:
        row = family_id_sql(alias)
        subj = f"(SELECT {family_id_sql()} FROM publications WHERE publication_number=%s LIMIT 1)"
        #  THE FIRST TERM IS LOAD BEARING. DOCDB writes '-1' rather than NULL for a publication
        #  with no simple family, and 21,862 rows of the live corpus carry it. Comparing the raw
        #  column excluded all 21,862 as "the subject's family" whenever the subject was one of
        #  them. Folding the sentinel to NULL fixes that but introduces the opposite hazard: with
        #  a NULL subject family, `row <> NULL` is NULL for every row and the filter would discard
        #  the entire corpus. A subject with no family has no family to exclude, so say so first.
        frag += f" AND ({subj} IS NULL OR {row} IS NULL OR {row} <> {subj})"
        params = list(params) + [subject.number, subject.number]
    return "AND " + frag, params


def as_mode(mode):
    """Coerce a mode name to the enum, leaving None and an enum alone."""
    return Mode(mode) if isinstance(mode, str) else mode
