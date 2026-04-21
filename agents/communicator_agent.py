"""
communicator_agent.py
---------------------
Receives diagnosed anomalies from analyst_agent, groups them by store, composes
a professional HTML email per store, and sends via Gmail SMTP.

Falls back to console output when GMAIL_APP_PASSWORD is a placeholder value,
matching the mock-mode pattern of analyst_agent.

Prerequisite — run this SQL once in Supabase before using:
    ALTER TABLE anomaly_alerts
        ADD COLUMN IF NOT EXISTS alert_sent     boolean     NOT NULL DEFAULT false,
        ADD COLUMN IF NOT EXISTS alert_sent_at  timestamptz;
"""

import os
import sys
import smtplib
import time
import uuid
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# ── constants ─────────────────────────────────────────────────────────────────

_PLACEHOLDER_VALUES = {"add_later", "", "your_gmail_app_password_here"}

_GMAIL_SMTP_HOST = "smtp.gmail.com"
_GMAIL_SMTP_PORT = 587

# Severity → (hex background, hex text)
_SEVERITY_COLOURS: dict[str, tuple[str, str]] = {
    "critical": ("#FDECEA", "#B71C1C"),
    "high":     ("#FFF3E0", "#E65100"),
    "medium":   ("#FFFDE7", "#F57F17"),
}
_SEVERITY_BADGE: dict[str, tuple[str, str]] = {
    "critical": ("#B71C1C", "#FFFFFF"),
    "high":     ("#E65100", "#FFFFFF"),
    "medium":   ("#F9A825", "#000000"),
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_supabase() -> Client:
    """Return an authenticated Supabase client."""
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _is_mock_mode() -> bool:
    return os.environ.get("GMAIL_APP_PASSWORD", "") in _PLACEHOLDER_VALUES


def _fmt_inr(value: float) -> str:
    """Format a number as Indian Rupee with commas, no decimals."""
    return f"₹{int(value):,}"


def _fmt_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


# ── HTML email builder ────────────────────────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Store Alert: {store_name}</title>
<style>
  body  {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
           background: #F5F5F5; margin: 0; padding: 24px; color: #212121; }}
  .card {{ background: #FFFFFF; border-radius: 8px; max-width: 760px; margin: 0 auto;
           box-shadow: 0 2px 8px rgba(0,0,0,.10); overflow: hidden; }}
  .header {{ background: #1A237E; color: #FFFFFF; padding: 28px 32px; }}
  .header h1 {{ margin: 0 0 4px; font-size: 22px; font-weight: 700; }}
  .header p  {{ margin: 0; font-size: 14px; opacity: .85; }}
  .meta {{ display: flex; gap: 24px; padding: 20px 32px; background: #E8EAF6;
           border-bottom: 1px solid #C5CAE9; flex-wrap: wrap; }}
  .meta-item {{ font-size: 13px; color: #3949AB; }}
  .meta-item strong {{ display: block; font-size: 15px; color: #1A237E; }}
  .section {{ padding: 24px 32px; }}
  .section-title {{ font-size: 13px; font-weight: 700; color: #616161;
                    text-transform: uppercase; letter-spacing: .06em;
                    margin: 0 0 14px; }}
  table  {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  th     {{ background: #1A237E; color: #FFFFFF; padding: 10px 12px;
            text-align: left; font-weight: 600; }}
  td     {{ padding: 9px 12px; border-bottom: 1px solid #E0E0E0; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px;
            font-size: 11px; font-weight: 700; text-transform: uppercase;
            letter-spacing: .05em; }}
  .anomaly-card {{ border: 1px solid #E0E0E0; border-radius: 6px;
                   margin-bottom: 14px; overflow: hidden; }}
  .anomaly-card:last-child {{ margin-bottom: 0; }}
  .anomaly-header {{ padding: 10px 14px; font-size: 13px; font-weight: 600;
                     display: flex; justify-content: space-between; align-items: center; }}
  .anomaly-body   {{ padding: 12px 14px; font-size: 13.5px; line-height: 1.6;
                     background: #FAFAFA; }}
  .anomaly-body .label {{ font-weight: 600; color: #424242; margin-bottom: 4px; }}
  .anomaly-body .action-box {{ background: #E3F2FD; border-left: 3px solid #1565C0;
                               padding: 8px 12px; margin-top: 10px; border-radius: 0 4px 4px 0; }}
  .footer {{ padding: 16px 32px; background: #FAFAFA; border-top: 1px solid #E0E0E0;
             font-size: 12px; color: #9E9E9E; text-align: center; }}
</style>
</head>
<body>
<div class="card">

  <!-- HEADER -->
  <div class="header">
    <h1>&#x26A0;&#xFE0F;&nbsp; Store Performance Alert</h1>
    <p>{store_name} &nbsp;|&nbsp; {region} Region &nbsp;|&nbsp; {n_anomalies} anomaly flag{plural} detected</p>
  </div>

  <!-- META -->
  <div class="meta">
    <div class="meta-item">Report Date<strong>{alert_date}</strong></div>
    <div class="meta-item">Store ID<strong>{store_id}</strong></div>
    <div class="meta-item">Region<strong>{region}</strong></div>
    <div class="meta-item">Period<strong>MTD April 2025 (17 days)</strong></div>
  </div>

  <!-- SUMMARY TABLE -->
  <div class="section">
    <div class="section-title">Flagged Metrics Summary</div>
    <table>
      <thead>
        <tr>
          <th>Metric</th>
          <th>Current Value</th>
          <th>vs Last Month</th>
          <th>Deviation</th>
          <th>Severity</th>
        </tr>
      </thead>
      <tbody>
        {summary_rows}
      </tbody>
    </table>
  </div>

  <!-- DIAGNOSES -->
  <div class="section" style="padding-top:0">
    <div class="section-title">Detailed Diagnosis</div>
    {diagnosis_cards}
  </div>

  <!-- FOOTER -->
  <div class="footer">
    Generated by Store Anomaly Monitor &nbsp;&bull;&nbsp; {generated_at} UTC
  </div>

</div>
</body>
</html>
"""


def _metric_display_name(metric_name: str) -> str:
    mapping = {
        "kra_ach_pct":       "KRA Achievement %",
        "pdps":              "Sales per Day (PDPS)",
        "avg_basket_value":  "Avg Basket Value (ABV)",
        "walkin_count":      "Walk-in Count",
    }
    return mapping.get(metric_name, metric_name.replace("_", " ").title())


def _format_metric_value(metric_name: str, value: float) -> str:
    """Format a metric value appropriately for display."""
    pct_metrics  = {"kra_ach_pct"}
    inr_metrics  = {"pdps", "avg_basket_value"}
    count_metrics = {"walkin_count"}

    if metric_name in pct_metrics:
        return f"{value:.1f}%"
    if metric_name in inr_metrics:
        return _fmt_inr(value)
    if metric_name in count_metrics:
        return f"{int(value):,}"
    return str(value)


def _build_summary_row(a: dict) -> str:
    """Render one <tr> for the summary table."""
    sev   = a.get("severity", "medium")
    bg, _ = _SEVERITY_COLOURS.get(sev, ("#FFFFFF", "#000000"))
    bdg_bg, bdg_fg = _SEVERITY_BADGE.get(sev, ("#757575", "#FFFFFF"))

    metric_label   = _metric_display_name(a["metric_name"])
    current_fmt    = _format_metric_value(a["metric_name"], float(a["actual_value"]))
    expected_fmt   = _format_metric_value(a["metric_name"], float(a["expected_value"]))
    deviation_fmt  = _fmt_pct(float(a["deviation_pct"]))
    badge_html     = (
        f'<span class="badge" style="background:{bdg_bg};color:{bdg_fg}">{sev.upper()}</span>'
    )

    return (
        f'<tr style="background:{bg}">'
        f"<td><strong>{metric_label}</strong></td>"
        f"<td>{current_fmt}</td>"
        f"<td>{expected_fmt}</td>"
        f"<td><strong>{deviation_fmt}</strong></td>"
        f"<td>{badge_html}</td>"
        f"</tr>"
    )


def _build_diagnosis_card(a: dict) -> str:
    """Render one anomaly card with diagnosis and recommended action."""
    sev    = a.get("severity", "medium")
    bg, fg = _SEVERITY_COLOURS.get(sev, ("#FAFAFA", "#000000"))
    bdg_bg, bdg_fg = _SEVERITY_BADGE.get(sev, ("#757575", "#FFFFFF"))

    metric_label  = _metric_display_name(a["metric_name"])
    diagnosis     = a.get("diagnosis", a.get("description", "No diagnosis available."))
    action        = a.get("recommended_action", "")
    badge_html    = (
        f'<span class="badge" style="background:{bdg_bg};color:{bdg_fg}">{sev.upper()}</span>'
    )

    action_block = ""
    if action:
        action_block = (
            f'<div class="action-box">'
            f"<strong>&#x1F4CC; Recommended Action:</strong> {action}"
            f"</div>"
        )

    return (
        f'<div class="anomaly-card">'
        f'  <div class="anomaly-header" style="background:{bg};color:{fg}">'
        f"    <span>{metric_label}</span>{badge_html}"
        f"  </div>"
        f'  <div class="anomaly-body">'
        f'    <div class="label">Root Cause Analysis</div>'
        f"    <div>{diagnosis}</div>"
        f"    {action_block}"
        f"  </div>"
        f"</div>"
    )


def _build_html(store_anomalies: list[dict]) -> str:
    """Compose the full HTML email body for one store."""
    first = store_anomalies[0]

    # Sort: critical → high → medium for consistent ordering
    order = {"critical": 0, "high": 1, "medium": 2}
    sorted_anomalies = sorted(store_anomalies, key=lambda a: order.get(a.get("severity", "medium"), 3))

    summary_rows    = "\n".join(_build_summary_row(a) for a in sorted_anomalies)
    diagnosis_cards = "\n".join(_build_diagnosis_card(a) for a in sorted_anomalies)
    n               = len(sorted_anomalies)

    return _HTML_TEMPLATE.format(
        store_name      = first["store_name"],
        store_id        = first["store_id"],
        region          = first["region"],
        alert_date      = first.get("alert_date", str(date.today())),
        n_anomalies     = n,
        plural          = "s" if n != 1 else "",
        summary_rows    = summary_rows,
        diagnosis_cards = diagnosis_cards,
        generated_at    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    )


def _build_subject(store_anomalies: list[dict]) -> str:
    first      = store_anomalies[0]
    n          = len(store_anomalies)
    alert_date = first.get("alert_date", str(date.today()))
    return f"Store Alert: {first['store_name']} — {n} anomal{'y' if n == 1 else 'ies'} detected ({alert_date})"


# ── email sending ─────────────────────────────────────────────────────────────

def _send_email(subject: str, html_body: str, recipient: str) -> None:
    """
    Send an HTML email via Gmail SMTP (port 587 / STARTTLS).
    Reads GMAIL_SENDER and GMAIL_APP_PASSWORD from environment.
    """
    sender   = os.environ["GMAIL_SENDER"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(_GMAIL_SMTP_HOST, _GMAIL_SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())


def _print_email(subject: str, html_body: str, recipient: str) -> None:
    """Console fallback used in mock mode."""
    separator = "─" * 72
    print(f"\n{separator}")
    print(f"[MOCK EMAIL — would send to: {recipient}]")
    print(f"Subject : {subject}")
    print(separator)
    # Print a readable plain-text summary instead of raw HTML
    import re
    text = re.sub(r"<[^>]+>", " ", html_body)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    print(text.strip())
    print(f"{separator}\n")


# ── Supabase writes ───────────────────────────────────────────────────────────

def _mark_alerts_sent(sb: Client, store_id: str, alert_date: str) -> None:
    """
    Set alert_sent=true and alert_sent_at=now() for all rows matching
    (store_id, alert_date).  Requires the columns to exist — see module docstring
    for the required ALTER TABLE.
    """
    try:
        sb.table("anomaly_alerts").update({
            "alert_sent":    True,
            "alert_sent_at": datetime.now(timezone.utc).isoformat(),
        }).eq("store_id", store_id).eq("alert_date", alert_date).execute()
    except Exception as exc:
        # Gracefully degrade if the columns haven't been added yet
        print(
            f"[communicator_agent] WARNING: could not mark alerts sent for "
            f"{store_id} ({exc}). Run the ALTER TABLE from the module docstring."
        )


def _log_run(
    sb: Client,
    run_id: str,
    run_date: date,
    status: str,
    **kwargs: Any,
) -> None:
    """Insert on start, update on completion/failure — mirrors other agents."""
    if status == "started":
        sb.table("agent_run_log").insert({
            "run_id":     run_id,
            "agent_name": "communicator_agent",
            "run_date":   run_date.isoformat(),
            "status":     status,
            **kwargs,
        }).execute()
    else:
        sb.table("agent_run_log").update(
            {"status": status, **kwargs}
        ).eq("run_id", run_id).execute()


# ── public entry point ────────────────────────────────────────────────────────

def run(
    diagnoses: list[dict],
    recipient: str | None = None,
    ref_date: date | None = None,
) -> int:
    """
    Execute the communicator agent.

    Parameters
    ----------
    diagnoses : list[dict]
        Enriched anomaly records returned by analyst_agent.run().
    recipient : str, optional
        Email address to send alerts to. Falls back to GMAIL_SENDER env var
        (i.e. send to yourself) if not provided.
    ref_date : date, optional
        Date stamp for agent_run_log. Defaults to date.today().

    Returns
    -------
    int
        Number of emails sent (or printed in mock mode).
    """
    ref      = ref_date or date.today()
    run_id   = str(uuid.uuid4())
    t0       = time.monotonic()
    mock     = _is_mock_mode()
    to_addr  = recipient or os.environ.get("ALERT_RECIPIENT") or os.environ.get("GMAIL_SENDER", "")

    sb = _get_supabase()
    _log_run(sb, run_id, ref, "started")
    print(f"[communicator_agent] run_id={run_id}  ref_date={ref}  mock={mock}")

    if not diagnoses:
        print("[communicator_agent] No diagnoses received — nothing to send.")
        _log_run(sb, run_id, ref, "completed",
                 stores_analyzed=0, alerts_created=0, emails_sent=0,
                 duration_seconds=round(time.monotonic() - t0, 2),
                 completed_at="now()")
        return 0

    try:
        # ── group by store ────────────────────────────────────────────────────
        store_groups: dict[str, list[dict]] = {}
        for d in diagnoses:
            store_groups.setdefault(d["store_id"], []).append(d)

        print(f"[communicator_agent] {len(diagnoses)} anomaly/ies across "
              f"{len(store_groups)} store(s) → {len(store_groups)} email(s)")

        emails_sent = 0

        for store_id, anomalies in store_groups.items():
            subject   = _build_subject(anomalies)
            html_body = _build_html(anomalies)
            alert_date = anomalies[0].get("alert_date", str(ref))

            print(f"[communicator_agent] {store_id} ({len(anomalies)} flag(s)) → {subject}")

            if mock:
                _print_email(subject, html_body, to_addr)
            else:
                _send_email(subject, html_body, to_addr)
                print(f"  ✓ Sent to {to_addr}")

            _mark_alerts_sent(sb, store_id, alert_date)
            emails_sent += 1

        duration = round(time.monotonic() - t0, 2)
        _log_run(sb, run_id, ref, "completed",
                 stores_analyzed=len(store_groups),
                 alerts_created=len(diagnoses),
                 emails_sent=emails_sent,
                 duration_seconds=duration,
                 completed_at="now()")
        print(f"[communicator_agent] Done in {duration}s — {emails_sent} email(s) {'printed' if mock else 'sent'}.")
        return emails_sent

    except Exception as exc:
        duration = round(time.monotonic() - t0, 2)
        _log_run(sb, run_id, ref, "failed",
                 error_message=str(exc),
                 duration_seconds=duration,
                 completed_at="now()")
        print(f"[communicator_agent] FAILED: {exc}")
        raise


# ── standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agents.monitor_agent  import run as monitor_run   # noqa: E402
    from agents.analyst_agent  import run as analyst_run   # noqa: E402

    ref = date(2025, 4, 17)
    anomalies = monitor_run(ref_date=ref)
    diagnoses = analyst_run(anomalies, ref_date=ref)
    run(diagnoses, ref_date=ref)
