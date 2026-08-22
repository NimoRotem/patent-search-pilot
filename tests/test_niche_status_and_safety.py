from __future__ import annotations

import csv
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from corpus.niche.manifest import PublicationRecord
from corpus.niche.providers.local import guard_read_only_sql
from corpus.niche.repository import PostgresNicheRepository
from corpus.niche.status import (
    DATABASE_FAMILY_SQL,
    DATABASE_SUMMARY_SQL,
    build_status_report,
    write_status_artifacts,
)


def test_no_active_corpus_writes_guard_allows_reads_and_rejects_mutation():
    guard_read_only_sql("SELECT id FROM publications WHERE id > %s ORDER BY id LIMIT %s")
    guard_read_only_sql("WITH ids AS (SELECT id FROM publications) SELECT * FROM ids")

    for statement in (
        "UPDATE publications SET title = ''",
        "INSERT INTO chunks(publication_id) VALUES (1)",
        "DELETE FROM claims",
        "CREATE INDEX ON publications(id)",
    ):
        with pytest.raises(ValueError, match="read-only"):
            guard_read_only_sql(statement)


def test_staging_writer_launchers_export_the_durable_database_identity():
    root = Path(__file__).parents[1]
    for name in (
        "run-niche-discovery.sh",
        "run-niche-embed.sh",
        "run-niche-fetch.sh",
        "run-niche-parse.sh",
        "run-niche-status.sh",
    ):
        script = (root / "ops" / name).read_text()
        assert 'export NICHE_EXPECTED_DATABASE="niche_full_v1"' in script
        assert 'export NICHE_DATABASE_FINGERPRINT="niche-full-v1-20260822"' in script


def test_discovery_launcher_accepts_an_explicit_nonempty_range(tmp_path):
    root = Path(__file__).parents[1]
    factory = tmp_path / "factory"
    executable = factory / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    executable.chmod(0o750)
    (factory / ".source.env").write_text(
        "PGHOST=source-host\nPGPORT=5432\nPGDATABASE=patents\n"
        "PGUSER=reader\nPGPASSWORD=test-only\n"
    )
    (factory / ".niche-db-password").write_text("test-only\n")

    completed = subprocess.run(
        [str(root / "ops" / "run-niche-discovery.sh"), "100-200"],
        env={**os.environ, "NICHE_FACTORY_ROOT": str(factory)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "-u",
        "-m",
        "src.corpus.niche.discover",
        "--id-start",
        "100",
        "--id-end",
        "200",
        "--batch-size",
        "1000",
        "--max-batches",
        "0",
        "--db-read-delay",
        "1.0",
    ]


def test_discovery_launcher_tail_starts_after_the_completed_snapshot(tmp_path):
    root = Path(__file__).parents[1]
    factory = tmp_path / "factory"
    executable = factory / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    executable.chmod(0o750)
    (factory / ".source.env").write_text(
        "PGHOST=source-host\nPGPORT=5432\nPGDATABASE=patents\n"
        "PGUSER=reader\nPGPASSWORD=test-only\n"
    )
    (factory / ".niche-db-password").write_text("test-only\n")

    completed = subprocess.run(
        [str(root / "ops" / "run-niche-discovery.sh"), "tail"],
        env={
            **os.environ,
            "NICHE_FACTORY_ROOT": str(factory),
            "NICHE_DISCOVERY_TAIL_ONCE": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines()[3:7] == [
        "--id-start",
        "6436391",
        "--id-end",
        "0",
    ]


def test_status_report_includes_publication_and_family_completeness():
    publications = [
        {
            "publication_id": "US1A1",
            "publication_number": "US1A1",
            "family_id": "F1",
            "authority": "US",
            "language": "en",
            "cpc_codes": ["B25J15/06"],
            "has_title": True,
            "has_abstract": True,
            "has_complete_claims": True,
            "has_complete_description": False,
            "has_citations": True,
            "preferred_source": "local",
            "fetch_status": "completed",
        },
        {
            "publication_id": "EP1A1",
            "publication_number": "EP1A1",
            "family_id": "F1",
            "authority": "EP",
            "language": "de",
            "cpc_codes": ["B25J15/06"],
            "has_title": True,
            "has_abstract": False,
            "has_complete_claims": False,
            "has_complete_description": True,
            "has_citations": False,
            "preferred_source": "epo",
            "fetch_status": "pending",
        },
        {
            "publication_id": "JP2A",
            "publication_number": "JP2A",
            "family_id": "F2",
            "authority": "JP",
            "language": "ja",
            "cpc_codes": [],
            "has_title": False,
            "has_abstract": False,
            "has_complete_claims": False,
            "has_complete_description": False,
            "has_citations": False,
            "preferred_source": "",
            "fetch_status": "failed",
        },
    ]
    attempts = [
        {"provider": "epo", "status": "success", "credits_used": 0, "attempted_at": "2026-08-22T00:00:00Z"},
        {"provider": "firecrawl", "status": "error", "credits_used": 1, "attempted_at": "2026-08-22T00:10:00Z"},
    ]
    jobs = [
        {"status": "pending", "heartbeat_at": None},
        {"status": "leased", "heartbeat_at": "2026-08-22T00:11:00Z"},
        {"status": "completed", "heartbeat_at": "2026-08-22T00:09:00Z"},
    ]

    report = build_status_report(
        publications,
        attempts,
        jobs,
        watermarks={"publication_id": 250, "source_max_publication_id": 1000},
    )

    assert report["total_publications"] == 3
    assert report["total_families"] == 2
    assert report["publication_completeness"]["claims_complete_pct"] == 33.33
    assert report["publication_completeness"]["description_complete_pct"] == 33.33
    assert report["family_completeness"]["claims_complete_pct"] == 50.0
    assert report["family_completeness"]["description_complete_pct"] == 50.0
    assert report["remaining_missing_claims"] == 2
    assert report["remaining_missing_descriptions"] == 2
    assert report["remaining_missing_full_text"] == 3
    assert report["provider_success_rates"]["epo"] == 100.0
    assert report["provider_success_rates"]["firecrawl"] == 0.0
    assert report["credits_spent_by_provider"] == {"epo": 0, "firecrawl": 1}
    assert report["queue"]["leased"] == 1
    assert report["last_heartbeat"] == "2026-08-22T00:11:00Z"
    assert report["discovery"]["source_scan_progress_pct"] == 25.0
    assert report["discovery"]["source_scan_complete"] is False


def test_discovery_progress_unions_resharded_ranges_without_double_counting():
    from corpus.niche.status import _discovery_progress

    in_progress = _discovery_progress({
        "publication_id:0:100": 40,
        "source_max_publication_id:0:100": 100,
        "publication_id:40:70": 55,
        "source_max_publication_id:40:70": 70,
        "publication_id:70:100": 70,
        "source_max_publication_id:70:100": 100,
    })

    assert in_progress["source_scan_progress_pct"] == 55.0
    assert in_progress["source_scan_complete"] is False

    complete = _discovery_progress({
        **in_progress["watermarks"],
        "publication_id:40:70": 70,
        "publication_id:70:100": 100,
    })
    assert complete["source_scan_progress_pct"] == 100.0
    assert complete["source_scan_complete"] is True


def test_status_artifacts_are_json_and_csv(tmp_path):
    report = build_status_report([], [], [])

    json_path, csv_path = write_status_artifacts(report, tmp_path)

    assert json_path.name == "niche_corpus_status.json"
    assert csv_path.name == "niche_corpus_status.csv"
    assert json.loads(json_path.read_text())["total_publications"] == 0
    assert b"\r\n" not in csv_path.read_bytes()
    rows = list(csv.DictReader(csv_path.open()))
    assert any(row["metric"] == "total_publications" and row["value"] == "0" for row in rows)


def test_database_status_uses_aggregate_sql_instead_of_materializing_manifest():
    summary = " ".join(DATABASE_SUMMARY_SQL.upper().split())
    families = " ".join(DATABASE_FAMILY_SQL.upper().split())

    assert "COUNT(*) FILTER" in summary
    assert "SELECT *" not in summary
    assert "BOOL_OR" in families
    assert "GROUP BY" in families


def test_saving_parsed_content_does_not_complete_manifest_before_lease_commit():
    class Cursor:
        sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params):
            self.sql = sql

    class Connection:
        def __init__(self, cursor):
            self._cursor = cursor

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return self._cursor

    class Stored:
        uri = "memory://object"

    cursor = Cursor()
    repository = PostgresNicheRepository(lambda: Connection(cursor))
    repository.save_parsed(
        "US1A1",
        {"completeness": {"has_complete_claims": True, "has_complete_description": True}},
        Stored(),
        Stored(),
    )

    assert "fetch_status" not in cursor.sql.lower()


def test_complete_local_family_is_enqueued_until_normalized():
    complete = PublicationRecord(
        "US1234567A1",
        family_id="F1",
        has_complete_claims=True,
        has_complete_description=True,
    )
    normalized = PublicationRecord(
        "US7654321A1",
        family_id="F2",
        has_complete_claims=True,
        has_complete_description=True,
        parsed_object_uri="gs://bucket/patents/parsed/US/US7654321A1.json",
    )
    repository = PostgresNicheRepository(None)
    complete_row = asdict(complete)
    complete_row.update(language=None, title=None, parsed_object_uri=None)
    repository.iter_publications = lambda: iter((complete_row, asdict(normalized)))

    class Queue:
        def __init__(self):
            self.publications = []

        def enqueue(self, publication_id, _priority):
            self.publications.append(publication_id)

    queue = Queue()

    assert repository.enqueue_incomplete_families(queue) == 1
    assert queue.publications == ["US1234567A1"]
