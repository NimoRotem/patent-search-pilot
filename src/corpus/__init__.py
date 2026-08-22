"""Offline corpus release building.

Nothing in this package may run against a serving database. A release is built on scratch,
sealed, snapshotted and only then activated; the switch is one row in `corpus_release_active`
and one transaction. See docs/corpus_release.md.
"""
