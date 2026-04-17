"""
analyst_agent.py
----------------
Receives the anomaly list from monitor_agent, calls Claude (claude-sonnet-4-5)
using tool_use to produce a structured root-cause diagnosis for each anomaly,
upserts results into anomaly_alerts, and logs the run to agent_run_log.

Falls back to deterministic mock diagnoses when ANTHROPIC_API_KEY is a
placeholder value ("add_later" / empty), so the pipeline can run end-to-end
without a live API key.
"""

import os
import sys
import time
import uuid
from datetime import date
from typing import Any

import anthropic
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# ── constants ─────────────────────────────────────────────────────────────────

MODEL = "claude-sonnet-4-5"

# Deviation thresholds for auto-severity when Claude isn't called
_CRITICAL_THRESHOLD = -30.0
_HIGH_THRESHOLD = -20.0

# Placeholder values that indicate the key hasn't been filled in yet
_PLACEHOLDER_KEYS = {"add_later", "", "your_anthropic_api_key_here"}

# ── tool definition ───────────────────────────────────────────────────────────

DIAGNOSIS_TOOL: dict = {
    "name": "submit_diagnosis",
    "description": (
        "Submit a structured root-cause diagnosis for a retail store performance anomaly. "
        "Call this tool exactly once with your complete analysis."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "store_id": {
                "type": "string",
                "description": "Store identifier, e.g. STR_003",
            },
            "store_name": {
                "type": "string",
                "description": "Full store name",
            },
            "metric_name": {
                "type": "string",
                "description": "The KPI that triggered this anomaly",
            },
            "diagnosis": {
                "type": "string",
                "description": (
                    "2-3 sentences explaining the most likely root cause(s) "
                    "based on the metric values and store context provided"
                ),
            },
            "severity": {
                "type": "string",
                "enum": ["critical", "high", "medium"],
                "description": (
                    "critical = >30 % drop or all-metrics failure; "
                    "high = 20-30 % drop; medium = 15-20 % drop"
                ),
            },
            "recommended_action": {
                "type": "string",
                "description": "One concise sentence: the most important corrective action right now",
            },
        },
        "required": [
            "store_id",
            "store_name",
            "metric_name",
            "diagnosis",
            "severity",
            "recommended_action",
        ],
    },
}


# ── client helpers ────────────────────────────────────────────────────────────

def _get_supabase() -> Client:
    """Return an authenticated Supabase client from env vars."""
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _get_anthropic() -> anthropic.Anthropic:
    """Return an Anthropic SDK client."""
    return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


def _is_mock_mode() -> bool:
    """True when the API key is a known placeholder — skip real Claude calls."""
    return os.environ.get("ANTHROPIC_API_KEY", "") in _PLACEHOLDER_KEYS


# ── data fetching ─────────────────────────────────────────────────────────────

def _fetch_mtd_summaries(
    sb: Client,
    store_ids: list[str],
    year: int,
    month: int,
) -> dict[str, dict]:
    """
    Fetch store_mtd_summary rows for the given stores and month.
    Returns a dict keyed by store_id so downstream code can look up context fast.
    """
    resp = (
        sb.table("store_mtd_summary")
        .select("*")
        .in_("store_id", store_ids)
        .eq("year", year)
        .eq("month", month)
        .execute()
    )
    return {row["store_id"]: row for row in (resp.data or [])}


# ── prompt construction ───────────────────────────────────────────────────────

def _build_prompt(anomaly: dict, mtd: dict | None) -> str:
    """
    Build the user prompt passed to Claude.
    Includes the specific anomaly details plus the full MTD context for that
    store so Claude can reason about whether the anomaly is isolated or systemic.
    """
    lines = [
        "You are a senior retail analytics expert. Diagnose the store performance anomaly below.",
        "",
        "## Anomaly Details",
        f"Store         : {anomaly['store_name']} ({anomaly['store_id']}) — {anomaly['region']} region",
        f"Metric        : {anomaly['metric_name']}",
        f"Current value : {anomaly['actual_value']}",
        f"Expected value: {anomaly['expected_value']}  (same days elapsed, prior month)",
        f"Deviation     : {anomaly['deviation_pct']:+.1f}%",
        f"Anomaly type  : {anomaly['anomaly_type']}",
        "",
    ]

    if mtd:
        lines += [
            "## Full Month-to-Date Context",
            f"Days elapsed              : {mtd.get('days_elapsed', 'N/A')}",
            f"MTD gross sales           : ₹{float(mtd.get('mtd_sales', 0)):,.0f}",
            f"MTD sales target          : ₹{float(mtd.get('mtd_sales_target', 0)):,.0f}",
            f"Sales achievement         : {float(mtd.get('sales_achievement_pct', 0)):.1f}%",
            f"MTD transactions          : {int(mtd.get('mtd_transactions', 0)):,}",
            f"MTD walk-ins              : {int(mtd.get('mtd_walkins', 0)):,}",
            f"Walk-in achievement       : {float(mtd.get('walkin_achievement_pct', 0)):.1f}%",
            f"Avg basket value (ABV)    : ₹{float(mtd.get('avg_basket_value', 0)):,.0f}",
            f"Projected monthly sales   : ₹{float(mtd.get('projected_monthly_sales', 0)):,.0f}",
            "",
        ]
    else:
        lines += ["## MTD Context\n(Not available — diagnosing from anomaly data only)\n"]

    lines += [
        "## Instructions",
        "Call submit_diagnosis with:",
        "  • diagnosis          — 2-3 sentences on the most likely root cause(s)",
        "  • severity           — critical / high / medium",
        "  • recommended_action — one concrete action the store manager should take today",
    ]
    return "\n".join(lines)


# ── Claude diagnosis ──────────────────────────────────────────────────────────

def _diagnose_with_claude(
    client: anthropic.Anthropic,
    anomaly: dict,
    mtd: dict | None,
) -> dict:
    """
    Call claude-sonnet-4-5 with a forced tool_use so the response is always
    structured JSON matching the submit_diagnosis schema.

    Returns the tool input dict:
      {store_id, store_name, metric_name, diagnosis, severity, recommended_action}
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        tools=[DIAGNOSIS_TOOL],
        tool_choice={"type": "tool", "name": "submit_diagnosis"},
        messages=[
            {"role": "user", "content": _build_prompt(anomaly, mtd)}
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_diagnosis":
            return dict(block.input)

    raise RuntimeError(
        f"[analyst_agent] Claude did not return submit_diagnosis for {anomaly['store_id']}"
    )


# ── mock fallback ─────────────────────────────────────────────────────────────

def _severity_from_deviation(deviation_pct: float) -> str:
    """Map deviation % to severity label for use in mock and override logic."""
    if deviation_pct <= _CRITICAL_THRESHOLD:
        return "critical"
    if deviation_pct <= _HIGH_THRESHOLD:
        return "high"
    return "medium"


_MOCK_TEMPLATES: dict[str, tuple[str, str]] = {
    "low_kra": (
        "The store is significantly underperforming against its sales target, "
        "most likely driven by reduced footfall combined with lower conversion rates. "
        "This pattern often indicates localised competitive pressure, weak promotional execution, "
        "or a decline in frontline staff productivity.",
        "Run an immediate floor audit and compare last 7 days of promotional adherence "
        "against the top-performing peer store in the region.",
    ),
    "abv_drop": (
        "Average basket value has declined sharply, suggesting customers are buying fewer items "
        "per visit or trading down to lower-priced SKUs. "
        "Root causes typically include stockouts in high-margin categories, "
        "inadequate upselling by the sales team, or a shift in customer mix.",
        "Audit availability of the top-20 revenue SKUs and brief the floor team on "
        "upselling and bundling scripts for premium categories.",
    ),
    "walkin_drop": (
        "Walk-in traffic has fallen materially month-on-month, "
        "pointing to an external footfall issue rather than in-store conversion. "
        "Possible causes include poor storefront visibility, a nearby competitor campaign, "
        "reduced local marketing spend, or seasonal migration of the target demographic.",
        "Review local competitor activity and activate a short-term traffic-driving "
        "in-store event or targeted digital promotion within 48 hours.",
    ),
    "all_metrics_weak": (
        "All key metrics—sales, transactions, and walk-ins—are simultaneously suppressed, "
        "which points to a systemic or operational issue rather than a single KPI problem. "
        "Common root causes include staff shortages, recent management changes, "
        "infrastructure disruption, or a broader catchment-area downturn.",
        "Escalate to the regional manager for an on-site diagnostic visit within 48 hours "
        "and assess staffing levels, operational logs, and any recent local incidents.",
    ),
}


def _mock_diagnosis(anomaly: dict) -> dict:
    """
    Return a deterministic diagnosis when ANTHROPIC_API_KEY is a placeholder.
    Severity is derived from the actual deviation so it reflects the data.
    """
    atype = anomaly.get("anomaly_type", "low_kra")
    diag_text, action = _MOCK_TEMPLATES.get(atype, _MOCK_TEMPLATES["low_kra"])
    return {
        "store_id":           anomaly["store_id"],
        "store_name":         anomaly["store_name"],
        "metric_name":        anomaly["metric_name"],
        "diagnosis":          diag_text,
        "severity":           _severity_from_deviation(float(anomaly.get("deviation_pct", -15.0))),
        "recommended_action": action,
    }


# ── Supabase writes ───────────────────────────────────────────────────────────

def _insert_alert(sb: Client, anomaly: dict, diagnosis: dict) -> None:
    """
    Upsert one row into anomaly_alerts, combining anomaly metadata with the
    Claude-generated diagnosis.  On conflict (store_id, alert_date, anomaly_type,
    metric_name) the row is updated so re-runs are idempotent.
    """
    payload = {
        "store_id":       anomaly["store_id"],
        "store_name":     anomaly["store_name"],
        "region":         anomaly["region"],
        "alert_date":     anomaly["alert_date"],
        "year":           anomaly["year"],
        "month":          anomaly["month"],
        "anomaly_type":   anomaly["anomaly_type"],
        "metric_name":    anomaly["metric_name"],
        "actual_value":   float(anomaly["actual_value"]),
        "expected_value": float(anomaly["expected_value"]),
        "deviation_pct":  float(anomaly["deviation_pct"]),
        "severity":       diagnosis["severity"],
        "description":    diagnosis["diagnosis"],
        "status":         "open",
    }
    sb.table("anomaly_alerts").upsert(
        payload,
        on_conflict="store_id,alert_date,anomaly_type,metric_name",
    ).execute()


def _log_run(
    sb: Client,
    run_id: str,
    run_date: date,
    status: str,
    **kwargs: Any,
) -> None:
    """
    Insert on first call (status='started'), update on subsequent calls.
    Mirrors the pattern used in monitor_agent to avoid needing a unique
    constraint on run_id beyond the PK.
    """
    if status == "started":
        sb.table("agent_run_log").insert({
            "run_id":     run_id,
            "agent_name": "analyst_agent",
            "run_date":   run_date.isoformat(),
            "status":     status,
            **kwargs,
        }).execute()
    else:
        sb.table("agent_run_log").update(
            {"status": status, **kwargs}
        ).eq("run_id", run_id).execute()


# ── public entry point ────────────────────────────────────────────────────────

def run(anomalies: list[dict], ref_date: date | None = None) -> list[dict]:
    """
    Execute the analyst agent.

    Parameters
    ----------
    anomalies : list[dict]
        Anomaly records produced by monitor_agent.run().  Each dict must
        contain at minimum: store_id, store_name, region, alert_date, year,
        month, anomaly_type, metric_name, actual_value, expected_value,
        deviation_pct.
    ref_date : date, optional
        Date to stamp on the agent_run_log row. Defaults to date.today().

    Returns
    -------
    list[dict]
        One enriched dict per anomaly: original fields merged with
        diagnosis, severity, and recommended_action from Claude (or mock).
    """
    ref    = ref_date or date.today()
    run_id = str(uuid.uuid4())
    t0     = time.monotonic()
    mock   = _is_mock_mode()

    sb        = _get_supabase()
    ai_client = None if mock else _get_anthropic()

    _log_run(sb, run_id, ref, "started")
    print(f"[analyst_agent] run_id={run_id}  ref_date={ref}  mock={mock}")

    if not anomalies:
        print("[analyst_agent] No anomalies received — nothing to diagnose.")
        _log_run(sb, run_id, ref, "completed",
                 stores_analyzed=0, anomalies_found=0, alerts_created=0,
                 duration_seconds=round(time.monotonic() - t0, 2),
                 completed_at="now()")
        return []

    try:
        # ── fetch MTD context for all anomaly stores ──────────────────────────
        store_ids = list({a["store_id"] for a in anomalies})
        year      = anomalies[0]["year"]
        month     = anomalies[0]["month"]
        mtd_map   = _fetch_mtd_summaries(sb, store_ids, year, month)
        print(f"[analyst_agent] MTD context loaded for {len(mtd_map)} store(s)")

        results: list[dict] = []
        alerts_created = 0

        for i, anomaly in enumerate(anomalies, 1):
            sid = anomaly["store_id"]
            mtd = mtd_map.get(sid)

            print(
                f"[analyst_agent] [{i}/{len(anomalies)}] "
                f"{sid} | {anomaly['anomaly_type']} | {anomaly['metric_name']} "
                f"({anomaly['deviation_pct']:+.1f}%)"
            )

            diagnosis = _mock_diagnosis(anomaly) if mock else _diagnose_with_claude(ai_client, anomaly, mtd)

            print(
                f"  → severity={diagnosis['severity']}  "
                f"action={diagnosis['recommended_action'][:72]}..."
            )

            _insert_alert(sb, anomaly, diagnosis)
            alerts_created += 1
            results.append({**anomaly, **diagnosis})

        duration = round(time.monotonic() - t0, 2)
        _log_run(sb, run_id, ref, "completed",
                 stores_analyzed=len(store_ids),
                 anomalies_found=len(anomalies),
                 alerts_created=alerts_created,
                 duration_seconds=duration,
                 completed_at="now()")
        print(f"[analyst_agent] Done in {duration}s — {alerts_created} alert(s) inserted.")
        return results

    except Exception as exc:
        duration = round(time.monotonic() - t0, 2)
        _log_run(sb, run_id, ref, "failed",
                 error_message=str(exc),
                 duration_seconds=duration,
                 completed_at="now()")
        print(f"[analyst_agent] FAILED: {exc}")
        raise


# ── standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Allow running as: python agents/analyst_agent.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agents.monitor_agent import run as monitor_run  # noqa: E402

    anomalies = monitor_run(ref_date=date(2025, 4, 17))
    diagnoses = run(anomalies, ref_date=date(2025, 4, 17))
    print(f"\nTotal diagnoses returned: {len(diagnoses)}")
