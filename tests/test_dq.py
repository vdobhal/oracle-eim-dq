"""Data-quality metric, trend, history, and report tests."""

from __future__ import annotations

import json
from itertools import cycle

import pytest

from oracle_mcp.dq import (
    DqHistory,
    calculate_metrics,
    render_markdown,
    severity_for,
    trend_from,
)
from oracle_mcp.errors import SqlValidationError


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        (10.01, "Critical"),
        (10.0, "High"),
        (5.0, "High"),
        (4.99, "Medium"),
        (1.0, "Medium"),
        (0.99, "Low"),
        (0.0, "Low"),
    ],
)
def test_severity_thresholds(rate, expected):
    assert severity_for(rate) == expected


def test_metrics_are_calculated_from_exact_counts():
    result = calculate_metrics(1000, 75)
    assert result == {
        "total_records": 1000,
        "failed_records": 75,
        "passed_records": 925,
        "pass_percentage": 92.5,
        "failure_percentage": 7.5,
        "severity": "High",
    }


def test_failed_records_cannot_exceed_total():
    with pytest.raises(ValueError, match="exceed"):
        calculate_metrics(2, 3)


def test_trend_flags_deterioration():
    trend = trend_from(8.5, {"failure_percentage": 5.0})
    assert trend["status"] == "DETERIORATED"
    assert trend["deteriorated"] is True
    assert trend["change_percentage_points"] == 3.5


def test_history_returns_latest_matching_execution(tmp_path):
    history = DqHistory(tmp_path / "dq.jsonl")
    history.append({"database": "ONPREM", "rule_id": "R1", "failure_percentage": 2})
    history.append({"database": "ATP", "rule_id": "R1", "failure_percentage": 8})
    history.append({"database": "ONPREM", "rule_id": "R1", "failure_percentage": 4})
    assert history.previous("onprem", "R1")["failure_percentage"] == 4


def test_history_does_not_compare_different_population_signatures(tmp_path):
    history = DqHistory(tmp_path / "dq.jsonl")
    history.append(
        {
            "database": "ONPREM",
            "rule_id": "R1",
            "population_signature": "all-serials",
            "failure_percentage": 8,
        }
    )
    history.append(
        {
            "database": "ONPREM",
            "rule_id": "R1",
            "population_signature": "active-serials",
            "failure_percentage": 3,
        }
    )
    assert (
        history.previous("ONPREM", "R1", "active-serials")["failure_percentage"]
        == 3
    )
    assert history.previous("ONPREM", "R1", "missing-population") is None


def test_report_contains_every_required_section():
    result = {
        "rule_id": "R-001",
        "rule_name": "Customer key is mandatory",
        "dimension": "Completeness",
        "reference_checkpoint": "Source contract section 4",
        "total_records": 100,
        "failed_records": 12,
        "pass_percentage": 88.0,
        "failure_percentage": 12.0,
        "severity": "Critical",
        "trend": {
            "status": "DETERIORATED",
            "deteriorated": True,
            "message": "Failure rate increased by 2.00 percentage points.",
        },
        "recommended_actions": ["Correct mandatory fields at source."],
    }
    report = render_markdown([result], "2026-08-23T00:00:00+00:00")
    for heading in (
        "Executive Summary",
        "DQ Score",
        "Critical Findings",
        "Trend Analysis",
        "Root Cause Analysis",
        "Recommended Actions",
        "Detailed Rule Results",
    ):
        assert f"# {heading}" in report
    assert "deteriorated" in report


def test_dml_metric_sql_is_rejected_before_execution(service, analyst, onprem_conn):
    with pytest.raises(SqlValidationError, match="rejected"):
        service._execute_dq_metric(  # noqa: SLF001 - security regression test
            "ONPREM",
            "DELETE FROM CDM_RPT.V_CUSTOMER_MASTER",
            "FAILED_RECORDS",
            analyst,
        )
    assert onprem_conn.executed == []


class FakePersistence:
    def __init__(self):
        self.calls = []

    def persist(self, summary, details):
        rows = list(details)
        self.calls.append((summary, rows))
        return len(rows)


def test_persisted_dq_run_is_disabled_without_writer(service):
    result = service.execute_and_persist_data_quality_rule(
        "R-001",
        "ONPREM",
        "SELECT COUNT(*) AS TOTAL_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER",
        "SELECT COUNT(*) AS FAILED_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER",
        (
            "SELECT CUSTOMER_NUMBER AS SYSTEM_SERIAL_NUMBER, "
            "CUSTOMER_NUMBER AS SOURCE_RECORD_KEY, "
            "'Missing' AS FAILURE_REASON, '{}' AS DQ_ATTRIBUTES_JSON "
            "FROM CDM_RPT.V_CUSTOMER_MASTER"
        ),
        "analyst",
        "dq-test",
    )
    assert result["status"] == "ERROR"
    assert result["error_code"] == "CONFIGURATION_ERROR"


def _prepare_persist_service(service, onprem_conn, monkeypatch):
    persistence = FakePersistence()
    service.dq_persistence = persistence
    monkeypatch.setattr(
        service,
        "_active_dq_rule",
        lambda *_args: {
            "RULE_ID": "R-001",
            "RULE_NAME": "Active customer has source",
            "DIMENSION": "Completeness",
            "ATTRIBUTE": "Source",
            "DQ_RULE": "Source is required.",
            "REFERENCE_CHECKPOINT": "Customer master",
        },
    )
    counts = cycle(
        [(2, ["CDM_RPT.V_CUSTOMER_MASTER"]), (1, ["CDM_RPT.V_CUSTOMER_MASTER"])]
    )
    monkeypatch.setattr(service, "_execute_dq_metric", lambda *_args: next(counts))
    monkeypatch.setattr(service, "_previous_persisted_dq_result", lambda *_args: None)
    onprem_conn.set_result(
        [
            "SYSTEM_SERIAL_NUMBER",
            "SOURCE_RECORD_KEY",
            "FAILURE_REASON",
            "DQ_ATTRIBUTES_JSON",
        ],
        [
            {
                "SYSTEM_SERIAL_NUMBER": "C-100",
                "SOURCE_RECORD_KEY": "C-100",
                "FAILURE_REASON": "Source is missing",
                "DQ_ATTRIBUTES_JSON": '{"customer_status":"ACTIVE"}',
            }
        ],
    )
    return persistence


def test_persisted_dq_run_uses_guarded_detail_select(
    service, onprem_conn, monkeypatch
):
    persistence = _prepare_persist_service(service, onprem_conn, monkeypatch)
    result = service.execute_and_persist_data_quality_rule(
        "R-001",
        "ONPREM",
        "SELECT COUNT(*) AS TOTAL_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER",
        (
            "SELECT COUNT(*) AS FAILED_RECORDS "
            "FROM CDM_RPT.V_CUSTOMER_MASTER WHERE SOURCE_SYSTEM IS NULL"
        ),
        (
            "SELECT CUSTOMER_NUMBER AS SYSTEM_SERIAL_NUMBER, "
            "CUSTOMER_NUMBER AS SOURCE_RECORD_KEY, "
            "'Source is missing' AS FAILURE_REASON, "
            "'{\"customer_status\":\"ACTIVE\"}' AS DQ_ATTRIBUTES_JSON "
            "FROM CDM_RPT.V_CUSTOMER_MASTER "
            "WHERE CUSTOMER_STATUS = 'ACTIVE' AND SOURCE_SYSTEM IS NULL"
        ),
        "analyst",
        "dq-test",
    )
    assert result["status"] == "OK"
    assert result["persisted_summary_rows"] == 1
    assert result["persisted_detail_rows"] == 1
    assert len(persistence.calls) == 1
    persisted_summary, persisted_details = persistence.calls[0]
    assert persisted_summary["failed_records"] == 1
    assert persisted_summary["population_signature"]
    assert persisted_details[0]["SYSTEM_SERIAL_NUMBER"] == "C-100"
    executed_detail_sql = onprem_conn.executed[-1][0]
    assert "DELETE" not in executed_detail_sql.upper()


def test_start_dq_run_returns_a_shared_company_run_id(service):
    started = service.start_dq_run("analyst", "dq-test")
    assert started["status"] == "OK"
    assert started["run_id"] == started["batch_id"]
    assert len(started["run_id"]) == 32


def test_persisted_rules_share_one_company_run_id(service, onprem_conn, monkeypatch):
    persistence = _prepare_persist_service(service, onprem_conn, monkeypatch)
    run_id = service.start_dq_run("analyst", "dq-test")["run_id"]
    first = service.execute_and_persist_data_quality_rule(
        "R-001",
        "ONPREM",
        "SELECT COUNT(*) AS TOTAL_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER",
        "SELECT COUNT(*) AS FAILED_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER WHERE SOURCE_SYSTEM IS NULL",
        (
            "SELECT CUSTOMER_NUMBER AS SYSTEM_SERIAL_NUMBER, "
            "CUSTOMER_NUMBER AS SOURCE_RECORD_KEY, "
            "'Source is missing' AS FAILURE_REASON, "
            "'{\"customer_status\":\"ACTIVE\"}' AS DQ_ATTRIBUTES_JSON "
            "FROM CDM_RPT.V_CUSTOMER_MASTER "
            "WHERE CUSTOMER_STATUS = 'ACTIVE' AND SOURCE_SYSTEM IS NULL"
        ),
        "analyst",
        "dq-test",
        run_id,
    )
    second = service.execute_and_persist_data_quality_rule(
        "R-001",
        "ONPREM",
        "SELECT COUNT(*) AS TOTAL_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER",
        "SELECT COUNT(*) AS FAILED_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER WHERE SOURCE_SYSTEM IS NULL",
        (
            "SELECT CUSTOMER_NUMBER AS SYSTEM_SERIAL_NUMBER, "
            "CUSTOMER_NUMBER AS SOURCE_RECORD_KEY, "
            "'Source is missing' AS FAILURE_REASON, "
            "'{\"customer_status\":\"ACTIVE\"}' AS DQ_ATTRIBUTES_JSON "
            "FROM CDM_RPT.V_CUSTOMER_MASTER "
            "WHERE CUSTOMER_STATUS = 'ACTIVE' AND SOURCE_SYSTEM IS NULL"
        ),
        "analyst",
        "dq-test",
        batch_id=run_id,
    )
    assert first["run_id"] == second["run_id"] == run_id
    assert persistence.calls[0][0]["run_id"] == persistence.calls[1][0]["run_id"] == run_id


def test_get_dq_run_report_rebuilds_the_company_summary(service, monkeypatch):
    run_id = "ab" * 16
    monkeypatch.setattr(
        service,
        "_run_internal_select",
        lambda *_args, **_kwargs: {
            "rows": [
                {
                    "RUN_ID": run_id,
                    "RULE_ID": "EC-CO-01",
                    "RULE_NAME": "EC Site required",
                    "DIMENSION": "Completeness",
                    "ATTRIBUTE_NAME": "End Customer Site",
                    "SOURCE_DATABASE": "ONPREM",
                    "TOTAL_RECORDS": 100,
                    "FAILED_RECORDS": 8,
                    "PASSED_RECORDS": 92,
                    "PASS_PERCENTAGE": 92.0,
                    "FAILURE_PERCENTAGE": 8.0,
                    "SEVERITY": "High",
                    "TREND_STATUS": "BASELINE",
                    "CHANGE_PERCENTAGE_POINTS": None,
                    "EXECUTED_AT": "2026-08-25T00:00:00+00:00",
                    "EXECUTED_BY": "dq-test",
                }
            ]
        },
    )
    report = service.get_dq_run_report(run_id, "analyst")
    assert report["status"] == "OK"
    assert report["run_id"] == run_id
    assert report["rule_count"] == 1
    assert "Executive Summary" in report["report_markdown"]
    assert "EC-CO-01" in report["report_markdown"]


def test_email_dq_run_summary_sends_summary_only(service, monkeypatch):
    run_id = "ab" * 16
    sent: dict[str, object] = {}

    monkeypatch.setattr(
        service,
        "_run_internal_select",
        lambda *_args, **_kwargs: {
            "rows": [
                {
                    "RUN_ID": run_id,
                    "RULE_ID": "EC-CO-01",
                    "RULE_NAME": "EC Site required",
                    "DIMENSION": "Completeness",
                    "ATTRIBUTE_NAME": "End Customer Site",
                    "SOURCE_DATABASE": "ONPREM",
                    "TOTAL_RECORDS": 100,
                    "FAILED_RECORDS": 8,
                    "PASSED_RECORDS": 92,
                    "PASS_PERCENTAGE": 92.0,
                    "FAILURE_PERCENTAGE": 8.0,
                    "SEVERITY": "High",
                    "TREND_STATUS": "BASELINE",
                    "CHANGE_PERCENTAGE_POINTS": None,
                    "EXECUTED_AT": "2026-08-25T00:00:00+00:00",
                    "EXECUTED_BY": "dq-test",
                }
            ]
        },
    )

    def fake_send(message, **kwargs):
        from oracle_mcp.mail import message_html, message_plain_text

        sent["body"] = message_plain_text(message)
        sent["html"] = message_html(message)
        sent["to"] = message["To"]
        return "smtp"

    monkeypatch.setattr("oracle_mcp.tools.send_summary_mail", fake_send)
    result = service.email_dq_run_summary(run_id, "analyst", to_address="vdobhal@netapp.com")
    assert result["status"] == "OK"
    assert result["to_address"] == "vdobhal@netapp.com"
    assert result["included_failed_record_details"] is False
    assert "SYSTEM_SERIAL_NUMBER" not in sent["body"]
    assert "EIM_DQ_FAILED_RECORDS" not in sent["body"]
    assert "Executive Summary" in sent["body"]
    assert "<table" in sent["html"]
    assert "Leadership review" in sent["html"]


def test_persisted_dq_detail_dml_is_rejected(service, onprem_conn, monkeypatch):
    persistence = _prepare_persist_service(service, onprem_conn, monkeypatch)
    result = service.execute_and_persist_data_quality_rule(
        "R-001",
        "ONPREM",
        "SELECT COUNT(*) AS TOTAL_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER",
        "SELECT COUNT(*) AS FAILED_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER",
        "DELETE FROM CDM_RPT.V_CUSTOMER_MASTER",
        "analyst",
        "dq-test",
    )
    assert result["status"] == "ERROR"
    assert result["error_code"] == "SQL_VALIDATION_FAILED"
    assert persistence.calls == []


def test_persisted_dq_detail_projection_fails_closed(
    service, onprem_conn, monkeypatch
):
    persistence = _prepare_persist_service(service, onprem_conn, monkeypatch)
    result = service.execute_and_persist_data_quality_rule(
        "R-001",
        "ONPREM",
        "SELECT COUNT(*) AS TOTAL_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER",
        "SELECT COUNT(*) AS FAILED_RECORDS FROM CDM_RPT.V_CUSTOMER_MASTER",
        (
            "SELECT CUSTOMER_NUMBER AS SYSTEM_SERIAL_NUMBER, "
            "CUSTOMER_NUMBER AS SOURCE_RECORD_KEY, "
            "'Missing' AS FAILURE_REASON "
            "FROM CDM_RPT.V_CUSTOMER_MASTER"
        ),
        "analyst",
        "dq-test",
    )
    assert result["status"] == "ERROR"
    assert result["error_code"] == "SQL_VALIDATION_FAILED"
    assert "DQ_ATTRIBUTES_JSON" in result["message"]
    assert persistence.calls == []
