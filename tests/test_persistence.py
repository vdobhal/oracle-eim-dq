"""DQ persistence security and transaction tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from oracle_mcp.errors import ConfigurationError, PersistenceError
from oracle_mcp.persistence import DqPersistenceRepository


class FakeCursor:
    def __init__(self) -> None:
        self.executed = []
        self.batches = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, binds):
        self.executed.append((sql, binds))

    def executemany(self, sql, binds):
        self.batches.append((sql, list(binds)))


class FakeWriteConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeWriteConnection()
        self.released = []

    def acquire(self):
        return self.connection

    def release(self, connection):
        self.released.append(connection)


class FakeOracleConnection:
    def __init__(self) -> None:
        self.pool = FakePool()

    def _pool_or_create(self):
        return self.pool

    def close(self):
        return None


def repository(oracle_profile, *, batch_size=2, max_details=10):
    repo = DqPersistenceRepository(
        profile=oracle_profile,
        summary_table="EIM_APPS.EIM_DQ_RECON_SUMMARY",
        detail_table="EIM_APPS.EIM_DQ_FAILED_RECORDS",
        batch_size=batch_size,
        max_details=max_details,
        query_timeout_seconds=30,
    )
    repo.connection = FakeOracleConnection()
    return repo


def summary(failed=3):
    return {
        "run_id": "a" * 32,
        "rule_id": "EC-CO-01",
        "rule_name": "Required site",
        "dimension": "Completeness",
        "attribute": "End Customer Site",
        "database": "ONPREM",
        "population_signature": "b" * 64,
        "total_sql_signature": "c" * 64,
        "detail_sql_signature": "d" * 64,
        "total_records": 10,
        "failed_records": failed,
        "passed_records": 10 - failed,
        "pass_percentage": 70.0,
        "failure_percentage": 30.0,
        "severity": "Critical",
        "trend": {"status": "BASELINE", "change_percentage_points": None},
        "source_objects": ["EIM.EIM_PR_SYSTEM"],
        "report_markdown": "# Executive Summary",
        "executed_by": "test",
        "execution_timestamp": datetime.now(timezone.utc),
    }


def details(count):
    for number in range(count):
        yield {
            "SYSTEM_SERIAL_NUMBER": f"SN{number}",
            "SOURCE_RECORD_KEY": f"SN{number}",
            "FAILURE_REASON": "Missing End Customer Site",
            "DQ_ATTRIBUTES_JSON": {"installed_product_status": "ACTIVE"},
        }


def test_fixed_table_names_cannot_be_redirected(oracle_profile):
    with pytest.raises(ConfigurationError, match="fixed governed object"):
        DqPersistenceRepository(
            profile=oracle_profile,
            summary_table="ATTACKER.RESULTS",
            detail_table="EIM_APPS.EIM_DQ_FAILED_RECORDS",
            batch_size=100,
            max_details=100,
            query_timeout_seconds=30,
        )


def test_summary_and_details_commit_atomically_in_batches(oracle_profile):
    repo = repository(oracle_profile)
    assert repo.persist(summary(), details(3)) == 3
    connection = repo.connection.pool.connection
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert [len(rows) for _, rows in connection.cursor_instance.batches] == [2, 1]
    assert "INSERT INTO EIM_APPS.EIM_DQ_RECON_SUMMARY" in (
        connection.cursor_instance.executed[0][0]
    )
    assert all(
        "INSERT INTO EIM_APPS.EIM_DQ_FAILED_RECORDS" in sql
        for sql, _ in connection.cursor_instance.batches
    )


def test_detail_count_mismatch_rolls_back(oracle_profile):
    repo = repository(oracle_profile)
    with pytest.raises(PersistenceError, match="does not match"):
        repo.persist(summary(failed=3), details(2))
    connection = repo.connection.pool.connection
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_detail_hard_limit_rolls_back(oracle_profile):
    repo = repository(oracle_profile, max_details=2)
    with pytest.raises(PersistenceError, match="configured maximum"):
        repo.persist(summary(failed=3), details(3))
    connection = repo.connection.pool.connection
    assert connection.commits == 0
    assert connection.rollbacks == 1
