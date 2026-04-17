"""
monitor_agent.py
----------------
Reads store_daily_sales from Supabase, computes MTD KPIs for the current
and prior month (same days elapsed), flags anomalies, upserts results into
store_mtd_summary, and logs the run to agent_run_log.

Returns a list of anomaly dicts for the analyst agent to consume.
"""

import os
import time
import uuid
import calendar
from datetime import date, timedelta
from typing import Any

from dotenv import load_dotenv
from supabase import create_client, Client

# ── thresholds ────────────────────────────────────────────────────────────────
KRA_FLOOR = 90.0          # % — flag if MTD sales achievement falls below this
MOM_DROP_THRESHOLD = 15.0 # % — flag if a metric drops >15% vs same period LM

load_dotenv()


# ── Supabase client ───────────────────────────────────────────────────────────

def _get_client() -> Client:
    """Create and return an authenticated Supabase client from env vars."""
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


# ── date helpers ──────────────────────────────────────────────────────────────

def _period_bounds(ref: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """
    Given a reference date (today), return the ISO date ranges for:
      - current month  : month_start → ref
      - previous month : prev_month_start → prev_month_start + (ref.day - 1)

    Example: ref = 2025-04-17
      current  → (2025-04-01, 2025-04-17)
      previous → (2025-03-01, 2025-03-17)
    """
    cur_start = ref.replace(day=1)
    cur_end   = ref

    # Same day-of-month ceiling in the previous month, clamped to month length
    prev_month = ref.month - 1 if ref.month > 1 else 12
    prev_year  = ref.year if ref.month > 1 else ref.year - 1
    prev_max   = calendar.monthrange(prev_year, prev_month)[1]
    prev_day   = min(ref.day, prev_max)

    prev_start = date(prev_year, prev_month, 1)
    prev_end   = date(prev_year, prev_month, prev_day)

    return (cur_start, cur_end), (prev_start, prev_end)


# ── data fetching ─────────────────────────────────────────────────────────────

def _fetch_period(client: Client, start: date, end: date) -> list[dict]:
    """
    Fetch all store_daily_sales rows whose date falls within [start, end].
    Supabase REST paginates at 1 000 rows by default; loop until exhausted.
    """
    rows: list[dict] = []
    page_size = 1000
    offset = 0

    while True:
        resp = (
            client.table("store_daily_sales")
            .select("*")
            .gte("date", start.isoformat())
            .lte("date", end.isoformat())
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    return rows


# ── KPI computation ───────────────────────────────────────────────────────────

def _aggregate(rows: list[dict]) -> dict[str, dict]:
    """
    Aggregate daily rows into per-store MTD totals and derived KPIs.

    Returns a dict keyed by store_id:
      {
        store_id, store_name, region,
        mtd_sales, mtd_transactions, mtd_walkins,
        mtd_sales_target, mtd_walkin_target,
        days_elapsed,
        kra_ach_pct,   # gross_sales / sales_target × 100
        pdps,          # sales per day per store  (avg daily sales)
        abv,           # avg basket value  (sales / transactions)
        mtd_walkin,    # raw walkin total (used for MoM comparison)
      }
    """
    stores: dict[str, dict] = {}

    for row in rows:
        sid = row["store_id"]
        if sid not in stores:
            stores[sid] = {
                "store_id":           sid,
                "store_name":         row["store_name"],
                "region":             row["region"],
                "mtd_sales":          0.0,
                "mtd_transactions":   0,
                "mtd_walkins":        0,
                "mtd_sales_target":   0.0,
                "mtd_walkin_target":  0,
                "days_elapsed":       0,
            }
        s = stores[sid]
        s["mtd_sales"]         += float(row["gross_sales"])
        s["mtd_transactions"]  += int(row["transactions"])
        s["mtd_walkins"]       += int(row["walkin_count"])
        s["mtd_sales_target"]  += float(row["daily_sales_target"])
        s["mtd_walkin_target"] += int(row["daily_walkin_target"])
        s["days_elapsed"]      += 1

    for s in stores.values():
        tgt = s["mtd_sales_target"] or 1.0
        txn = s["mtd_transactions"] or 1
        days = s["days_elapsed"] or 1

        s["kra_ach_pct"] = round(s["mtd_sales"] / tgt * 100, 2)
        s["pdps"]        = round(s["mtd_sales"] / days, 2)
        s["abv"]         = round(s["mtd_sales"] / txn, 2)
        s["mtd_walkin"]  = s["mtd_walkins"]

    return stores


# ── anomaly detection ─────────────────────────────────────────────────────────

def _mom_drop_pct(current: float, previous: float) -> float:
    """Return the month-on-month percentage change (negative = drop)."""
    if previous == 0:
        return 0.0
    return round((current - previous) / previous * 100, 2)


def _detect_anomalies(
    cur: dict[str, dict],
    prev: dict[str, dict],
    ref_date: date,
) -> list[dict]:
    """
    Compare current-month KPIs against the same-days-elapsed period last month.
    Flags:
      - kra_ach_pct  < KRA_FLOOR  (absolute threshold)
      - pdps MoM drop > MOM_DROP_THRESHOLD
      - abv  MoM drop > MOM_DROP_THRESHOLD
      - walkin MoM drop > MOM_DROP_THRESHOLD

    Returns a list of anomaly dicts ready for the analyst agent.
    """
    anomalies: list[dict] = []

    for sid, cs in cur.items():
        ps = prev.get(sid, {})
        store_anomalies: list[dict] = []

        # 1. Low KRA
        if cs["kra_ach_pct"] < KRA_FLOOR:
            store_anomalies.append({
                "anomaly_type":   "low_kra",
                "metric_name":    "kra_ach_pct",
                "actual_value":   cs["kra_ach_pct"],
                "expected_value": KRA_FLOOR,
                "deviation_pct":  round(cs["kra_ach_pct"] - KRA_FLOOR, 2),
            })

        # 2. PDPS MoM drop
        if ps:
            pdps_chg = _mom_drop_pct(cs["pdps"], ps["pdps"])
            if pdps_chg < -MOM_DROP_THRESHOLD:
                store_anomalies.append({
                    "anomaly_type":   "abv_drop",        # PDPS ≈ revenue-side ABV signal
                    "metric_name":    "pdps",
                    "actual_value":   cs["pdps"],
                    "expected_value": ps["pdps"],
                    "deviation_pct":  pdps_chg,
                })

            # 3. ABV MoM drop
            abv_chg = _mom_drop_pct(cs["abv"], ps["abv"])
            if abv_chg < -MOM_DROP_THRESHOLD:
                store_anomalies.append({
                    "anomaly_type":   "abv_drop",
                    "metric_name":    "avg_basket_value",
                    "actual_value":   cs["abv"],
                    "expected_value": ps["abv"],
                    "deviation_pct":  abv_chg,
                })

            # 4. Walk-in MoM drop
            walkin_chg = _mom_drop_pct(cs["mtd_walkin"], ps.get("mtd_walkin", 0))
            if walkin_chg < -MOM_DROP_THRESHOLD:
                store_anomalies.append({
                    "anomaly_type":   "walkin_drop",
                    "metric_name":    "walkin_count",
                    "actual_value":   float(cs["mtd_walkin"]),
                    "expected_value": float(ps.get("mtd_walkin", 0)),
                    "deviation_pct":  walkin_chg,
                })

        for a in store_anomalies:
            anomalies.append({
                "store_id":     sid,
                "store_name":   cs["store_name"],
                "region":       cs["region"],
                "alert_date":   ref_date.isoformat(),
                "year":         ref_date.year,
                "month":        ref_date.month,
                "description":  (
                    f"{cs['store_name']} | {a['metric_name']} = {a['actual_value']} "
                    f"(expected ~{a['expected_value']}, {a['deviation_pct']:+.1f}%)"
                ),
                **a,
            })

    return anomalies


# ── Supabase writes ───────────────────────────────────────────────────────────

def _upsert_mtd_summary(
    client: Client,
    cur: dict[str, dict],
    ref: date,
) -> None:
    """Upsert one row per store into store_mtd_summary for the current month."""
    total_days = calendar.monthrange(ref.year, ref.month)[1]
    rows = []

    for s in cur.values():
        days = s["days_elapsed"] or 1
        rows.append({
            "store_id":                s["store_id"],
            "store_name":              s["store_name"],
            "region":                  s["region"],
            "year":                    ref.year,
            "month":                   ref.month,
            "days_elapsed":            days,
            "mtd_sales":               round(s["mtd_sales"], 2),
            "mtd_transactions":        s["mtd_transactions"],
            "mtd_walkins":             s["mtd_walkins"],
            "mtd_sales_target":        round(s["mtd_sales_target"], 2),
            "mtd_walkin_target":       s["mtd_walkin_target"],
            "sales_achievement_pct":   s["kra_ach_pct"],
            "walkin_achievement_pct":  round(
                s["mtd_walkins"] / max(s["mtd_walkin_target"], 1) * 100, 2
            ),
            "avg_basket_value":        s["abv"],
            "projected_monthly_sales": round(s["pdps"] * total_days, 2),
            "updated_at":              "now()",
        })

    # Batch upsert in chunks of 100
    chunk = 100
    for i in range(0, len(rows), chunk):
        client.table("store_mtd_summary").upsert(
            rows[i : i + chunk],
            on_conflict="store_id,year,month",
        ).execute()


def _log_run(
    client: Client,
    run_id: str,
    agent_name: str,
    run_date: date,
    status: str,
    **kwargs: Any,
) -> None:
    """
    Insert on first call (status='started'); update the same row on subsequent
    calls (status='completed' or 'failed'). Avoids requiring a unique constraint
    on run_id beyond the default primary-key behaviour.
    """
    if status == "started":
        payload: dict[str, Any] = {
            "run_id":     run_id,
            "agent_name": agent_name,
            "run_date":   run_date.isoformat(),
            "status":     status,
            **kwargs,
        }
        client.table("agent_run_log").insert(payload).execute()
    else:
        update_payload: dict[str, Any] = {"status": status, **kwargs}
        client.table("agent_run_log").update(update_payload).eq("run_id", run_id).execute()


# ── public entry point ────────────────────────────────────────────────────────

def run(ref_date: date | None = None) -> list[dict]:
    """
    Execute the monitor agent.

    Parameters
    ----------
    ref_date : date, optional
        The date to treat as "today". Defaults to date.today().
        Override in tests or backfills.

    Returns
    -------
    list[dict]
        Anomaly records ready for the analyst agent. Each dict contains:
        store_id, store_name, region, alert_date, year, month,
        anomaly_type, metric_name, actual_value, expected_value,
        deviation_pct, description.
    """
    ref = ref_date or date.today()
    run_id = str(uuid.uuid4())
    t0 = time.monotonic()

    client = _get_client()

    _log_run(client, run_id, "monitor_agent", ref, "started")
    print(f"[monitor_agent] run_id={run_id}  ref_date={ref}")

    try:
        (cur_start, cur_end), (prev_start, prev_end) = _period_bounds(ref)

        print(f"[monitor_agent] Fetching current period  : {cur_start} → {cur_end}")
        cur_rows = _fetch_period(client, cur_start, cur_end)
        print(f"[monitor_agent] Fetching previous period : {prev_start} → {prev_end}")
        prev_rows = _fetch_period(client, prev_start, prev_end)

        print(f"[monitor_agent] Rows — current: {len(cur_rows)}, previous: {len(prev_rows)}")

        cur_agg  = _aggregate(cur_rows)
        prev_agg = _aggregate(prev_rows)

        anomalies = _detect_anomalies(cur_agg, prev_agg, ref)

        print(f"[monitor_agent] Stores analysed : {len(cur_agg)}")
        print(f"[monitor_agent] Anomalies found : {len(anomalies)}")
        for a in anomalies:
            print(f"  ↳ {a['store_id']} | {a['anomaly_type']} | {a['metric_name']} "
                  f"= {a['actual_value']} ({a['deviation_pct']:+.1f}%)")

        print("[monitor_agent] Upserting MTD summary...")
        _upsert_mtd_summary(client, cur_agg, ref)

        duration = round(time.monotonic() - t0, 2)
        _log_run(
            client, run_id, "monitor_agent", ref, "completed",
            stores_analyzed=len(cur_agg),
            anomalies_found=len(anomalies),
            duration_seconds=duration,
            completed_at="now()",
        )
        print(f"[monitor_agent] Done in {duration}s")
        return anomalies

    except Exception as exc:
        duration = round(time.monotonic() - t0, 2)
        _log_run(
            client, run_id, "monitor_agent", ref, "failed",
            error_message=str(exc),
            duration_seconds=duration,
            completed_at="now()",
        )
        print(f"[monitor_agent] FAILED: {exc}")
        raise


if __name__ == "__main__":
    anomalies = run(ref_date=date(2025, 4, 17))
    print(f"\nReturned {len(anomalies)} anomaly record(s) to caller.")
