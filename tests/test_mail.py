"""Summary-only DQ recon mail tests."""

from __future__ import annotations

import smtplib

import pytest

from oracle_mcp.dq import assert_summary_only_mail, render_markdown
from oracle_mcp.mail import (
    build_summary_mail_body,
    build_summary_mail_html,
    build_summary_message,
    message_html,
    message_plain_text,
    send_summary_mail,
)


def _result() -> dict:
    return {
        "rule_id": "EC-CO-01",
        "rule_name": "EC Site required",
        "dimension": "Completeness",
        "reference_checkpoint": "End Customer Site must be present",
        "total_records": 100,
        "failed_records": 8,
        "pass_percentage": 92.0,
        "failure_percentage": 8.0,
        "severity": "High",
        "trend": {
            "status": "BASELINE",
            "deteriorated": False,
            "message": "This is the first recorded execution.",
        },
        "recommended_actions": ["Assign the issue to the accountable data steward."],
    }


def test_summary_mail_includes_report_sections_not_failed_rows():
    body = build_summary_mail_body([_result()], run_id="ab" * 16)
    assert "Executive Summary" in body
    assert "DQ Score" in body
    assert "Detailed Rule Results" in body
    assert "EC-CO-01" in body
    assert "Failed-record detail rows are omitted" in body
    assert_summary_only_mail(body)
    assert "SYSTEM_SERIAL_NUMBER" not in body
    assert "FAILURE_REASON" not in body
    assert "EIM_DQ_FAILED_RECORDS" not in body


def test_summary_mail_html_is_tabular_and_color_coded():
    markup = build_summary_mail_html([_result()], run_id="ab" * 16)
    assert "<table" in markup
    assert "Leadership review" in markup
    assert "Detailed rule results" in markup
    assert "EC-CO-01" in markup
    assert "#9A3412" in markup  # High severity color
    assert_summary_only_mail(markup)
    assert "SYSTEM_SERIAL_NUMBER" not in markup
    assert "EIM_DQ_FAILED_RECORDS" not in markup


def test_summary_mail_rejects_failed_record_payloads():
    leaked = render_markdown([_result()]) + "\nSYSTEM_SERIAL_NUMBER=ABC123\n"
    with pytest.raises(ValueError, match="failed-record"):
        assert_summary_only_mail(leaked)


def test_smtp_send_delivers_summary_only(monkeypatch):
    captured: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host
            captured["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            return None

        def starttls(self):
            captured["starttls"] = True

        def login(self, user, password):
            captured["user"] = user

        def send_message(self, message):
            captured["body"] = message_plain_text(message)
            captured["html"] = message_html(message)
            captured["to"] = message["To"]

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    message = build_summary_message(
        run_id="ab" * 16,
        results=[_result()],
        to_address="vdobhal@netapp.com",
        from_address="dq@example.com",
    )
    used = send_summary_mail(
        message,
        transport="smtp",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="dq",
        smtp_password="secret",
    )
    assert used == "smtp"
    assert captured["to"] == "vdobhal@netapp.com"
    assert "SYSTEM_SERIAL_NUMBER" not in str(captured["body"])
    assert "Executive Summary" in str(captured["body"])
    assert "<table" in str(captured["html"])
    assert "Leadership review" in str(captured["html"])
