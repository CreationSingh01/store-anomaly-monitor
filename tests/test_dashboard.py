"""Tests for the Store Anomaly Monitor dashboard API."""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from faker import Faker
from fastapi.testclient import TestClient

from app import app

fake = Faker()
client = TestClient(app)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_alert(**overrides: object) -> dict:
    """Build a fake alert dict matching the AlertRecord schema."""
    base: dict = {
        "store_id": fake.bothify("STR_###"),
        "store_name": fake.company(),
        "region": fake.city(),
        "alert_date": (date.today() - timedelta(days=1)).isoformat(),
        "year": date.today().year,
        "month": date.today().month,
        "anomaly_type": "low_kra",
        "metric_name": "kra_ach_pct",
        "actual_value": round(fake.pyfloat(min_value=50, max_value=89), 2),
        "expected_value": 90.0,
        "deviation_pct": round(fake.pyfloat(min_value=-40, max_value=-1), 2),
        "severity": "high",
        "description": fake.sentence(),
        "status": "open",
    }
    base.update(overrides)
    return base


def _make_kra(**overrides: object) -> dict:
    """Build a fake KRA summary dict matching the KraRecord schema."""
    base: dict = {
        "store_id": fake.bothify("STR_###"),
        "store_name": fake.company(),
        "region": fake.city(),
        "year": date.today().year,
        "month": date.today().month,
        "days_elapsed": fake.random_int(min=1, max=28),
        "mtd_sales": round(fake.pyfloat(min_value=100000, max_value=500000), 2),
        "mtd_sales_target": round(fake.pyfloat(min_value=300000, max_value=600000), 2),
        "sales_achievement_pct": round(fake.pyfloat(min_value=60, max_value=120), 2),
        "walkin_achievement_pct": round(fake.pyfloat(min_value=60, max_value=110), 2),
        "avg_basket_value": round(fake.pyfloat(min_value=500, max_value=3000), 2),
        "projected_monthly_sales": round(fake.pyfloat(min_value=200000, max_value=700000), 2),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_returns_200() -> None:
    """GET /health returns HTTP 200."""
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_body() -> None:
    """GET /health returns the correct JSON payload."""
    resp = client.get("/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "store-anomaly-api"
    assert body["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# GET /api/alerts
# ---------------------------------------------------------------------------

@patch("routers.dashboard.get_supabase")
def test_alerts_default_days_returns_list(mock_get_sb: MagicMock) -> None:
    """GET /api/alerts with default days=7 returns a list of alerts."""
    alerts = [_make_alert() for _ in range(3)]
    mock_chain = MagicMock()
    mock_chain.execute.return_value.data = alerts
    mock_get_sb.return_value.table.return_value.select.return_value.gte.return_value.order.return_value = mock_chain

    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 3


@patch("routers.dashboard.get_supabase")
def test_alerts_custom_days_param(mock_get_sb: MagicMock) -> None:
    """GET /api/alerts?days=30 passes correct cutoff to Supabase query."""
    alerts = [_make_alert()]
    mock_chain = MagicMock()
    mock_chain.execute.return_value.data = alerts
    mock_get_sb.return_value.table.return_value.select.return_value.gte.return_value.order.return_value = mock_chain

    resp = client.get("/api/alerts?days=30")
    assert resp.status_code == 200
    # Verify gte was called with a date string 30 days back
    gte_call_args = mock_get_sb.return_value.table.return_value.select.return_value.gte.call_args
    assert gte_call_args is not None
    cutoff_arg = gte_call_args[0][1]
    expected_cutoff = (date.today() - timedelta(days=30)).isoformat()
    assert cutoff_arg == expected_cutoff


@patch("routers.dashboard.get_supabase")
def test_alerts_empty_result(mock_get_sb: MagicMock) -> None:
    """GET /api/alerts returns empty list when no alerts exist."""
    mock_chain = MagicMock()
    mock_chain.execute.return_value.data = []
    mock_get_sb.return_value.table.return_value.select.return_value.gte.return_value.order.return_value = mock_chain

    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    assert resp.json() == []


@patch("routers.dashboard.get_supabase")
def test_alerts_supabase_error_returns_500(mock_get_sb: MagicMock) -> None:
    """GET /api/alerts returns 500 when Supabase raises an exception."""
    mock_get_sb.return_value.table.side_effect = RuntimeError("DB connection failed")

    resp = client.get("/api/alerts")
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/stores/kra
# ---------------------------------------------------------------------------

@patch("routers.dashboard.get_supabase")
def test_kra_returns_list(mock_get_sb: MagicMock) -> None:
    """GET /api/stores/kra returns a list of KRA records."""
    records = [_make_kra() for _ in range(5)]
    mock_chain = MagicMock()
    mock_chain.execute.return_value.data = records
    (
        mock_get_sb.return_value.table.return_value
        .select.return_value.eq.return_value.eq.return_value.order.return_value
    ) = mock_chain

    resp = client.get("/api/stores/kra")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 5


@patch("routers.dashboard.get_supabase")
def test_kra_filters_current_month(mock_get_sb: MagicMock) -> None:
    """GET /api/stores/kra queries the current year and month."""
    mock_chain = MagicMock()
    mock_chain.execute.return_value.data = []
    table_mock = mock_get_sb.return_value.table.return_value
    table_mock.select.return_value.eq.return_value.eq.return_value.order.return_value = mock_chain

    client.get("/api/stores/kra")

    today = date.today()
    first_eq = table_mock.select.return_value.eq
    first_eq.assert_called_once_with("year", today.year)
    second_eq = first_eq.return_value.eq
    second_eq.assert_called_once_with("month", today.month)


@patch("routers.dashboard.get_supabase")
def test_kra_supabase_error_returns_500(mock_get_sb: MagicMock) -> None:
    """GET /api/stores/kra returns 500 when Supabase raises an exception."""
    mock_get_sb.return_value.table.side_effect = RuntimeError("DB connection failed")

    resp = client.get("/api/stores/kra")
    assert resp.status_code == 500
