"""Email the company DQ recon summary without failed-record details."""

from __future__ import annotations

import html
import platform
import shutil
import smtplib
import subprocess
import tempfile
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Literal

from .dq import (
    assert_summary_only_mail,
    calculate_metrics,
    recommended_actions,
    render_markdown,
    utc_now,
)
from .errors import ConfigurationError, MailDeliveryError

Transport = Literal["auto", "smtp", "sendmail", "mailapp"]

_SEVERITY_STYLE = {
    "Critical": ("#991B1B", "#FEE2E2"),
    "High": ("#9A3412", "#FFEDD5"),
    "Medium": ("#92400E", "#FEF3C7"),
    "Low": ("#065F46", "#D1FAE5"),
}


def summary_mail_subject(run_id: str, rule_count: int) -> str:
    return f"EIM DQ recon summary — run {run_id} ({rule_count} rule(s))"


def message_plain_text(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain",))
    return part.get_content() if part is not None else ""


def message_html(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("html",))
    return part.get_content() if part is not None else ""


def build_summary_mail_body(
    results: list[dict[str, Any]],
    *,
    run_id: str,
    generated_at: str | None = None,
) -> str:
    """Render the governed summary report. Failed serials are never included."""
    heading = (
        f"Company DQ recon summary for RUN_ID `{run_id}`.\n"
        "This message contains rule-level metrics only. "
        "Failed-record detail rows are omitted.\n"
    )
    report = render_markdown(results, generated_at or utc_now())
    return assert_summary_only_mail(heading + "\n" + report)


def build_summary_mail_html(
    results: list[dict[str, Any]],
    *,
    run_id: str,
    generated_at: str | None = None,
) -> str:
    """Leadership HTML: color-coded tables and KPI cards. Summary metrics only."""
    generated_at = generated_at or utc_now()
    total = sum(int(row["total_records"]) for row in results)
    failed = sum(int(row["failed_records"]) for row in results)
    score = calculate_metrics(total, failed)
    critical = [row for row in results if row["severity"] == "Critical"]
    deteriorated = [row for row in results if row.get("trend", {}).get("deteriorated")]
    sev_fg, sev_bg = _SEVERITY_STYLE.get(score["severity"], _SEVERITY_STYLE["Low"])

    critical_html = (
        "".join(_finding_row(row) for row in critical)
        if critical
        else _empty_note("No critical findings were identified.")
    )
    trend_html = (
        "".join(_finding_row(row, trend=True) for row in deteriorated)
        if deteriorated
        else _empty_note("No deterioration was detected against available previous runs.")
    )
    root_html = "".join(_root_cause_row(row) for row in results)
    action_items: list[str] = []
    for row in results:
        for action in row.get("recommended_actions") or recommended_actions(row):
            if action not in action_items:
                action_items.append(action)
    actions_html = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #E5E7EB;'
        f'font-size:13px;color:#111827;">{html.escape(action)}</td></tr>'
        for action in action_items
    )
    rule_rows = "".join(_rule_table_row(row) for row in results)

    markup = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>EIM DQ recon summary</title></head>
<body style="margin:0;padding:0;background:#F3F4F6;font-family:'Segoe UI',Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F4F6;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="720" cellpadding="0" cellspacing="0" style="max-width:720px;background:#FFFFFF;border:1px solid #E5E7EB;">
  <tr>
    <td style="background:#003D6B;color:#FFFFFF;padding:28px 28px 22px 28px;">
      <div style="font-size:11px;letter-spacing:1.4px;text-transform:uppercase;opacity:0.85;">
        EIM Data Quality · Leadership review
      </div>
      <div style="font-size:24px;font-weight:700;margin-top:8px;line-height:1.25;">
        Company DQ recon summary
      </div>
      <div style="font-size:13px;margin-top:10px;opacity:0.92;">
        RUN_ID {html.escape(run_id)} · {html.escape(str(generated_at))}
      </div>
    </td>
  </tr>
  <tr>
    <td style="padding:20px 20px 8px 20px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          {_kpi_cell("Overall DQ score", f"{score['pass_percentage']:.2f}%", sev_fg, sev_bg)}
          {_kpi_cell("Overall severity", str(score["severity"]), sev_fg, sev_bg)}
          {_kpi_cell("Rules evaluated", str(len(results)), "#1F2937", "#F3F4F6")}
          {_kpi_cell("Failed / total", f"{failed:,} / {total:,}", "#1F2937", "#F3F4F6")}
        </tr>
      </table>
    </td>
  </tr>
  {_section("Executive summary",
    f'<p style="margin:0;font-size:14px;line-height:1.55;color:#111827;">'
    f"{len(results)} active data-quality rule(s) were evaluated. "
    f"The blended DQ score is <strong>{score['pass_percentage']:.2f}%</strong> "
    f"({html.escape(score['severity'])} overall). "
    f"{failed:,} of {total:,} records failed at the rule grain. "
    f"This briefing contains rule-level metrics only; failed-record detail rows are omitted."
    f"</p>")}
  {_section("Critical findings", critical_html)}
  {_section("Trend analysis", trend_html)}
  {_section("Root cause analysis",
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
    + root_html + "</table>")}
  {_section("Recommended actions",
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
    + actions_html + "</table>")}
  {_section("Detailed rule results",
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
    'style="border-collapse:collapse;">'
    "<thead><tr>"
    + "".join(
        _th(label)
        for label in (
            "Rule",
            "Dimension",
            "Total",
            "Failed",
            "Pass %",
            "Fail %",
            "Severity",
            "Trend",
        )
    )
    + "</tr></thead><tbody>"
    + rule_rows
    + "</tbody></table>")}
  <tr>
    <td style="padding:16px 28px 24px 28px;font-size:11px;color:#6B7280;line-height:1.45;">
      Population: ACTIVE installed products, End Customer ROLE_ID = 1.
      Source of this mail: EIM_APPS.EIM_DQ_RECON_SUMMARY. Failed-record tables are not attached.
    </td>
  </tr>
</table>
</td></tr>
</table>
</body>
</html>
"""
    return assert_summary_only_mail(markup)


def build_summary_message(
    *,
    run_id: str,
    results: list[dict[str, Any]],
    to_address: str,
    from_address: str,
    generated_at: str | None = None,
) -> EmailMessage:
    plain = build_summary_mail_body(
        results, run_id=run_id, generated_at=generated_at
    )
    html_body = build_summary_mail_html(
        results, run_id=run_id, generated_at=generated_at
    )
    message = EmailMessage()
    message["Subject"] = summary_mail_subject(run_id, len(results))
    message["From"] = from_address
    message["To"] = to_address
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")
    return message


def send_summary_mail(
    message: EmailMessage,
    *,
    transport: Transport = "auto",
    smtp_host: str = "",
    smtp_port: int = 25,
    smtp_user: str = "",
    smtp_password: str = "",
    smtp_starttls: bool = True,
) -> str:
    """Deliver a summary-only message. Returns the transport that was used."""
    chosen = _resolve_transport(transport, smtp_host=smtp_host)
    if chosen == "smtp":
        _send_smtp(
            message,
            host=smtp_host,
            port=smtp_port,
            user=smtp_user,
            password=smtp_password,
            starttls=smtp_starttls,
        )
        return "smtp"
    if chosen == "sendmail":
        _send_sendmail(message)
        return "sendmail"
    if chosen == "mailapp":
        _send_mailapp(message)
        return "mailapp"
    raise ConfigurationError(
        "No DQ mail transport is available. Set ORACLE_MCP_DQ_MAIL_SMTP_HOST "
        "or use Mail.app / sendmail on this host."
    )


def _kpi_cell(label: str, value: str, fg: str, bg: str) -> str:
    return (
        '<td width="25%" style="padding:6px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'<tr><td style="background:{bg};border:1px solid #E5E7EB;padding:14px 12px;">'
        f'<div style="font-size:11px;color:#6B7280;text-transform:uppercase;'
        f'letter-spacing:0.6px;">{html.escape(label)}</div>'
        f'<div style="font-size:20px;font-weight:700;color:{fg};margin-top:6px;">'
        f"{html.escape(value)}</div>"
        f"</td></tr></table></td>"
    )


def _section(title: str, inner: str) -> str:
    return (
        "<tr><td style='padding:8px 20px 16px 20px;'>"
        f"<div style='font-size:13px;font-weight:700;color:#003D6B;margin:0 0 10px 0;"
        f"text-transform:uppercase;letter-spacing:0.5px;'>{html.escape(title)}</div>"
        f"{inner}</td></tr>"
    )


def _th(label: str) -> str:
    return (
        f'<th align="left" style="background:#0F172A;color:#F8FAFC;font-size:11px;'
        f"font-weight:600;padding:9px 8px;border:1px solid #0F172A;"
        f'text-transform:uppercase;letter-spacing:0.3px;">{html.escape(label)}</th>'
    )


def _badge(severity: str) -> str:
    fg, bg = _SEVERITY_STYLE.get(severity, _SEVERITY_STYLE["Low"])
    return (
        f'<span style="display:inline-block;padding:3px 8px;border-radius:999px;'
        f"background:{bg};color:{fg};font-size:11px;font-weight:700;"
        f'letter-spacing:0.3px;">{html.escape(severity)}</span>'
    )


def _rule_table_row(row: dict[str, Any]) -> str:
    severity = str(row.get("severity") or "Low")
    _fg, bg = _SEVERITY_STYLE.get(severity, _SEVERITY_STYLE["Low"])
    trend = html.escape(str((row.get("trend") or {}).get("status") or "N/A"))
    rule = html.escape(f"{row['rule_id']} — {row['rule_name']}")
    dimension = html.escape(str(row.get("dimension") or "N/A"))
    return (
        f'<tr style="background:{bg};">'
        f'<td style="padding:9px 8px;border:1px solid #E5E7EB;font-size:12px;color:#111827;">{rule}</td>'
        f'<td style="padding:9px 8px;border:1px solid #E5E7EB;font-size:12px;">{dimension}</td>'
        f'<td align="right" style="padding:9px 8px;border:1px solid #E5E7EB;font-size:12px;">{int(row["total_records"]):,}</td>'
        f'<td align="right" style="padding:9px 8px;border:1px solid #E5E7EB;font-size:12px;font-weight:600;">{int(row["failed_records"]):,}</td>'
        f'<td align="right" style="padding:9px 8px;border:1px solid #E5E7EB;font-size:12px;">{float(row["pass_percentage"]):.2f}%</td>'
        f'<td align="right" style="padding:9px 8px;border:1px solid #E5E7EB;font-size:12px;">{float(row["failure_percentage"]):.2f}%</td>'
        f'<td style="padding:9px 8px;border:1px solid #E5E7EB;">{_badge(severity)}</td>'
        f'<td style="padding:9px 8px;border:1px solid #E5E7EB;font-size:12px;">{trend}</td>'
        f"</tr>"
    )


def _finding_row(row: dict[str, Any], *, trend: bool = False) -> str:
    if trend:
        text = f"{row['rule_id']}: {(row.get('trend') or {}).get('message') or 'deteriorated'}"
    else:
        text = (
            f"{row['rule_id']} — {row['rule_name']}: "
            f"{float(row['failure_percentage']):.2f}% failure rate."
        )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin-bottom:8px;"><tr>'
        f'<td style="background:#FEE2E2;border-left:4px solid #991B1B;padding:10px 12px;'
        f'font-size:13px;color:#7F1D1D;">{html.escape(text)}</td>'
        f"</tr></table>"
    )


def _root_cause_row(row: dict[str, Any]) -> str:
    checkpoint = row.get("reference_checkpoint") or "No reference checkpoint was supplied."
    text = (
        f"{row['rule_id']}: The failure pattern relates to "
        f"{row.get('dimension') or 'the governed rule dimension'}. "
        f"Reference checkpoint: {checkpoint}"
    )
    return (
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #E5E7EB;'
        f'font-size:13px;color:#111827;">{html.escape(text)}</td></tr>'
    )


def _empty_note(text: str) -> str:
    return (
        f'<p style="margin:0;font-size:13px;color:#374151;background:#F9FAFB;'
        f'border:1px solid #E5E7EB;padding:10px 12px;">{html.escape(text)}</p>'
    )


def _resolve_transport(transport: Transport, *, smtp_host: str) -> str:
    if transport == "auto":
        if smtp_host.strip():
            return "smtp"
        if platform.system() == "Darwin":
            return "mailapp"
        if shutil.which("sendmail"):
            return "sendmail"
        raise ConfigurationError(
            "DQ mail auto-transport needs SMTP settings, Mail.app, or sendmail."
        )
    if transport == "smtp" and not smtp_host.strip():
        raise ConfigurationError("ORACLE_MCP_DQ_MAIL_SMTP_HOST is required for SMTP.")
    if transport == "mailapp" and platform.system() != "Darwin":
        raise ConfigurationError("Mail.app delivery is only available on macOS.")
    if transport == "sendmail" and not shutil.which("sendmail"):
        raise ConfigurationError("sendmail was not found on this host.")
    return transport


def _send_smtp(
    message: EmailMessage,
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    starttls: bool,
) -> None:
    try:
        with smtplib.SMTP(host, port, timeout=30) as client:
            client.ehlo()
            if starttls:
                client.starttls()
                client.ehlo()
            if user:
                client.login(user, password)
            client.send_message(message)
    except OSError as exc:
        raise MailDeliveryError("The SMTP server rejected the DQ summary mail.") from exc


def _send_sendmail(message: EmailMessage) -> None:
    sendmail = shutil.which("sendmail")
    if not sendmail:
        raise ConfigurationError("sendmail was not found on this host.")
    try:
        completed = subprocess.run(
            [sendmail, "-t", "-oi"],
            input=message.as_bytes(),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except OSError as exc:
        raise MailDeliveryError("sendmail could not be started.") from exc
    if completed.returncode != 0:
        raise MailDeliveryError("sendmail rejected the DQ summary mail.")


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _send_mailapp(message: EmailMessage) -> None:
    to_address = str(message["To"])
    subject = str(message["Subject"])
    html_body = message_html(message) or message_plain_text(message)
    handle = tempfile.NamedTemporaryFile(
        prefix="eim-dq-mail-", suffix=".html", delete=False
    )
    html_path = Path(handle.name)
    try:
        handle.write(html_body.encode("utf-8"))
        handle.close()
        posix = str(html_path)
        script = (
            f'set htmlBody to do shell script "cat " & quoted form of "{_escape_applescript(posix)}"\n'
            'tell application "Mail"\n'
            f'set newMessage to make new outgoing message with properties '
            f'{{subject:"{_escape_applescript(subject)}", visible:false}}\n'
            "tell newMessage\n"
            "try\n"
            "set html content to htmlBody\n"
            "on error\n"
            "set content to htmlBody\n"
            "end try\n"
            f'make new to recipient at end of to recipients with properties '
            f'{{address:"{_escape_applescript(to_address)}"}}\n'
            "send\n"
            "end tell\n"
            "end tell\n"
        )
        try:
            completed = subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except OSError as exc:
            raise MailDeliveryError("Mail.app could not be started.") from exc
        if completed.returncode != 0:
            raise MailDeliveryError(
                "Mail.app could not send the DQ recon summary. "
                "Sign in to Mail and retry, or configure SMTP."
            )
    finally:
        html_path.unlink(missing_ok=True)
