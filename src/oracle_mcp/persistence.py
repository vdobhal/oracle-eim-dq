"""Least-privilege persistence for DQ summaries and failed-record details.

This module is intentionally separate from the SELECT-only query path. It can
only issue two fixed, parameterized INSERT statements with a dedicated writer
credential; no caller-provided DML reaches Oracle.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .db import OracleConnection
from .errors import ConfigurationError, PersistenceError
from .settings import OracleProfile

_FQN = re.compile(r"^[A-Z][A-Z0-9_$#]*\.[A-Z][A-Z0-9_$#]*$")


def _fixed_table(value: str, expected: str) -> str:
    table = str(value or "").strip().upper()
    if table != expected or not _FQN.fullmatch(table):
        raise ConfigurationError(
            f"DQ persistence table must be the fixed governed object {expected}."
        )
    return table


class DqPersistenceRepository:
    """Atomically insert one rule summary and its complete failed-detail set."""

    def __init__(
        self,
        *,
        profile: OracleProfile,
        summary_table: str,
        detail_table: str,
        batch_size: int,
        max_details: int,
        query_timeout_seconds: int,
    ) -> None:
        self.summary_table = _fixed_table(
            summary_table, "EIM_APPS.EIM_DQ_RECON_SUMMARY"
        )
        self.detail_table = _fixed_table(
            detail_table, "EIM_APPS.EIM_DQ_FAILED_RECORDS"
        )
        self.batch_size = int(batch_size)
        self.max_details = int(max_details)
        self.connection = OracleConnection(
            profile, query_timeout_seconds=query_timeout_seconds
        )

    def persist(
        self,
        summary: dict[str, Any],
        details: Iterable[dict[str, Any]],
    ) -> int:
        """Commit summary and details together, or roll the complete run back."""
        summary_sql = f"""
            INSERT INTO {self.summary_table} (
                RUN_ID, RULE_ID, RULE_NAME, DIMENSION, ATTRIBUTE_NAME,
                SOURCE_DATABASE, POPULATION_SIGNATURE, TOTAL_SQL_SIGNATURE,
                DETAIL_SQL_SIGNATURE, TOTAL_RECORDS, FAILED_RECORDS,
                PASSED_RECORDS, PASS_PERCENTAGE, FAILURE_PERCENTAGE, SEVERITY,
                TREND_STATUS, CHANGE_PERCENTAGE_POINTS, SOURCE_OBJECTS_JSON,
                REPORT_MARKDOWN, EXECUTED_BY, EXECUTED_AT
            ) VALUES (
                :run_id, :rule_id, :rule_name, :dimension, :attribute_name,
                :source_database, :population_signature, :total_sql_signature,
                :detail_sql_signature, :total_records, :failed_records,
                :passed_records, :pass_percentage, :failure_percentage, :severity,
                :trend_status, :change_percentage_points, :source_objects_json,
                :report_markdown, :executed_by, :executed_at
            )
        """
        detail_sql = f"""
            INSERT INTO {self.detail_table} (
                RUN_ID, RULE_ID, DETAIL_SEQUENCE, SYSTEM_SERIAL_NUMBER,
                SOURCE_RECORD_KEY, FAILURE_REASON, DQ_ATTRIBUTES_JSON
            ) VALUES (
                :run_id, :rule_id, :detail_sequence, :system_serial_number,
                :source_record_key, :failure_reason, :dq_attributes_json
            )
        """
        pool = self.connection._pool_or_create()  # noqa: SLF001
        connection = pool.acquire()
        count = 0
        try:
            with connection.cursor() as cursor:
                cursor.execute(summary_sql, self._summary_binds(summary))
                batch: list[dict[str, Any]] = []
                for detail in details:
                    count += 1
                    if count > self.max_details:
                        raise PersistenceError(
                            f"Failed-detail count exceeds the configured maximum "
                            f"of {self.max_details:,}; no rows were committed."
                        )
                    batch.append(self._detail_binds(summary, detail, count))
                    if len(batch) >= self.batch_size:
                        cursor.executemany(detail_sql, batch)
                        batch.clear()
                if batch:
                    cursor.executemany(detail_sql, batch)
                expected = int(summary["failed_records"])
                if count != expected:
                    raise PersistenceError(
                        f"Failed-detail count {count:,} does not match the governed "
                        f"failed count {expected:,}; no rows were committed."
                    )
            connection.commit()
            return count
        except PersistenceError:
            connection.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - never expose Oracle details
            connection.rollback()
            raise PersistenceError(
                "Oracle rejected the DQ persistence transaction; no rows were committed."
            ) from exc
        finally:
            pool.release(connection)

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _summary_binds(summary: dict[str, Any]) -> dict[str, Any]:
        executed_at = summary["execution_timestamp"]
        if isinstance(executed_at, str):
            executed_at = datetime.fromisoformat(executed_at)
        trend = summary.get("trend") or {}
        return {
            "run_id": summary["run_id"],
            "rule_id": summary["rule_id"],
            "rule_name": str(summary.get("rule_name") or "")[:500],
            "dimension": str(summary.get("dimension") or "")[:200],
            "attribute_name": str(summary.get("attribute") or "")[:500],
            "source_database": str(summary["database"])[:30],
            "population_signature": summary["population_signature"],
            "total_sql_signature": summary["total_sql_signature"],
            "detail_sql_signature": summary["detail_sql_signature"],
            "total_records": int(summary["total_records"]),
            "failed_records": int(summary["failed_records"]),
            "passed_records": int(summary["passed_records"]),
            "pass_percentage": float(summary["pass_percentage"]),
            "failure_percentage": float(summary["failure_percentage"]),
            "severity": str(summary["severity"])[:20],
            "trend_status": str(trend.get("status") or "BASELINE")[:30],
            "change_percentage_points": trend.get("change_percentage_points"),
            "source_objects_json": json.dumps(summary.get("source_objects") or []),
            "report_markdown": str(summary.get("report_markdown") or ""),
            "executed_by": str(summary.get("executed_by") or "unknown")[:100],
            "executed_at": executed_at,
        }

    @staticmethod
    def _detail_binds(
        summary: dict[str, Any], detail: dict[str, Any], sequence: int
    ) -> dict[str, Any]:
        attributes = detail.get("DQ_ATTRIBUTES_JSON")
        if attributes is None:
            attributes = {}
        if not isinstance(attributes, str):
            attributes = json.dumps(attributes, ensure_ascii=False, default=str)
        return {
            "run_id": summary["run_id"],
            "rule_id": summary["rule_id"],
            "detail_sequence": sequence,
            "system_serial_number": str(
                detail.get("SYSTEM_SERIAL_NUMBER") or ""
            )[:500]
            or None,
            "source_record_key": str(detail.get("SOURCE_RECORD_KEY") or "")[:1000]
            or None,
            "failure_reason": str(detail.get("FAILURE_REASON") or "")[:2000],
            "dq_attributes_json": attributes,
        }
