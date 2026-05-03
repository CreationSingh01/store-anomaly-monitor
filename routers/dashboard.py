"""Dashboard API router for the Store Anomaly Monitor."""

import logging
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter()


class AlertRecord(BaseModel):
    """Schema for a single alert row returned by GET /api/alerts."""

    store_id: str
    store_name: str
    region: str
    alert_date: str
    year: int
    month: int
    anomaly_type: str
    metric_name: str
    actual_value: float
    expected_value: float
    deviation_pct: float
    severity: str
    description: str
    status: str


class KraRecord(BaseModel):
    """Schema for a single KRA summary row returned by GET /api/stores/kra."""

    store_id: str
    store_name: str
    region: str
    year: int
    month: int
    days_elapsed: int
    mtd_sales: float
    mtd_sales_target: float
    sales_achievement_pct: float
    walkin_achievement_pct: float
    avg_basket_value: float
    projected_monthly_sales: float


@router.get("/api/alerts", response_model=list[AlertRecord])
def get_alerts(days: int = Query(default=7, ge=1, le=365)) -> list[AlertRecord]:
    """Return anomaly alerts from the last *days* days.

    Parameters
    ----------
    days:
        Number of calendar days to look back from today. Defaults to 7.

    Returns
    -------
    list[AlertRecord]
        All alert rows whose alert_date >= today - days.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    try:
        sb = get_supabase()
        resp = (
            sb.table("anomaly_alerts")
            .select("*")
            .gte("alert_date", cutoff)
            .order("alert_date", desc=True)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.error("Failed to fetch alerts: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch alerts") from exc


@router.get("/api/stores/kra", response_model=list[KraRecord])
def get_stores_kra() -> list[KraRecord]:
    """Return the latest month-to-date KRA summary for every store.

    Queries store_mtd_summary for the current year/month so callers get
    fresh performance data without needing any filter parameters.

    Returns
    -------
    list[KraRecord]
        One row per store containing MTD sales, targets, and achievement %.
    """
    today = date.today()
    try:
        sb = get_supabase()
        resp = (
            sb.table("store_mtd_summary")
            .select(
                "store_id,store_name,region,year,month,days_elapsed,"
                "mtd_sales,mtd_sales_target,sales_achievement_pct,"
                "walkin_achievement_pct,avg_basket_value,projected_monthly_sales"
            )
            .eq("year", today.year)
            .eq("month", today.month)
            .order("sales_achievement_pct", desc=False)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.error("Failed to fetch KRA data: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch KRA data") from exc
