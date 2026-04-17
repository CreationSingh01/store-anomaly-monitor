# Store Anomaly Monitor

Automated multi-agent pipeline that detects retail store performance anomalies daily, generates AI-powered root-cause diagnoses, and delivers formatted email alerts to area managers.

---

## Business Problem

An area manager overseeing 20 stores cannot manually review every store's KPIs every morning. Underperforming stores slip through the cracks until a weekly review — by which point two or three lost trading days have compounded into a material revenue gap.

This system runs automatically at 09:00 IST every day. It compares each store's month-to-date performance against the same number of elapsed days in the prior month, flags stores where a metric has dropped by more than 15%, and sends the area manager a concise email per affected store with the likely root cause and the single most important corrective action — before the store opens for the day.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        GitHub Actions Cron                          │
│                 (daily_run.yml — 03:30 UTC / 09:00 IST)             │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ python main.py
                               ▼
                    ┌──────────────────────┐
                    │   LangGraph Graph    │
                    │   (graph.py)         │
                    └──────────┬───────────┘
                               │
               ┌───────────────▼───────────────┐
               │        Monitor Agent           │
               │  • Fetches store_daily_sales   │
               │    from Supabase               │
               │  • Computes MTD KPIs           │
               │  • Detects anomalies           │
               │  • Upserts store_mtd_summary   │
               └───────────────┬───────────────┘
                               │ anomaly list
                 (if empty → skip to END)
                               │
               ┌───────────────▼───────────────┐
               │        Analyst Agent           │
               │  • Calls Claude API            │
               │    (claude-sonnet-4-5)         │
               │  • tool_use → structured       │
               │    diagnosis per anomaly       │
               │  • Upserts anomaly_alerts      │
               └───────────────┬───────────────┘
                               │ diagnoses
                               │
               ┌───────────────▼───────────────┐
               │      Communicator Agent        │
               │  • Groups anomalies by store   │
               │  • Builds HTML email           │
               │  • Sends via Gmail SMTP        │
               │  • Updates alert_sent flags    │
               └───────────┬───────────┬────────┘
                           │           │
                    ┌──────▼──┐   ┌────▼──────┐
                    │  Gmail  │   │ Supabase  │
                    │ (SMTP)  │   │ (Postgres)│
                    └─────────┘   └───────────┘
```

---

## How It Works

- **Monitor Agent** connects to Supabase, pulls daily sales rows for the current month and the equivalent window last month (e.g. April 1–17 vs March 1–17), computes KRA achievement %, sales per day, average basket value, and walk-in count per store, then flags any metric that has dropped more than 15% or fallen below a KRA floor of 90%.
- **Analyst Agent** takes the list of flagged anomalies, fetches the full month-to-date context for each store, and sends each anomaly to Claude using forced `tool_use` — guaranteeing a structured JSON response with a root-cause diagnosis, severity rating, and a single recommended action.
- **Communicator Agent** groups all anomalies by store (so a store with three flagged metrics generates one email, not three), renders a professional HTML email with a metric summary table and per-anomaly diagnosis cards, and delivers it via Gmail SMTP.
- **GitHub Actions** runs the whole pipeline on a cron schedule every morning at 09:00 IST, passes secrets via environment variables, and logs every agent invocation to `agent_run_log` in Supabase so runs are auditable.

---

## Tech Stack

| Tool | Purpose | Why Chosen |
|---|---|---|
| **LangGraph** | Pipeline orchestration | Isolates each agent as a node; conditional edge skips analyst + communicator when no anomalies exist; easy to extend with retries or new stages |
| **Claude API** (`claude-sonnet-4-5`) | AI root-cause diagnosis | `tool_use` with `tool_choice=forced` returns validated structured JSON — no fragile free-text parsing |
| **Supabase** (Postgres) | All data storage and audit logging | Single credential pair, REST API sufficient for all access patterns, built-in dashboard for inspecting data |
| **GitHub Actions** | Scheduler and CI runner | Zero infrastructure to manage; secrets injection; `workflow_dispatch` for manual re-runs |
| **Gmail SMTP** | Alert email delivery | No additional service dependency; HTML emails render correctly across all major email clients |
| **python-dotenv** | Environment variable management | Consistent `.env` loading in both local dev and CI without code changes |
| **Pandas / NumPy** | Synthetic data generation | Realistic seasonality curves (December peak, weekend uplift, month-end spike) with deterministic seeded anomalies |

---

## Metrics Monitored

| Metric | Definition | Anomaly Trigger |
|---|---|---|
| **KRA Achievement %** | MTD gross sales ÷ MTD sales target × 100 | Falls below **90%** (absolute floor) |
| **PDPS** (Sales per Day per Store) | MTD gross sales ÷ days elapsed | Month-on-month drop **> 15%** vs same days last month |
| **ABV** (Average Basket Value) | MTD gross sales ÷ MTD transactions | Month-on-month drop **> 15%** vs same days last month |
| **Walk-in Count** | Total customer walk-ins month to date | Month-on-month drop **> 15%** vs same days last month |

---

## Anomaly Detection Logic

The monitor agent compares **like-for-like periods** — not current MTD against the full previous month.

```
Today = April 17

Current window    : April 1 → April 17   (17 days elapsed)
Comparison window : March 1 → March 17   (same 17 days last month)
```

This prevents a statistical artefact where mid-month stores always look worse than stores at month-end simply because fewer days have accumulated. The comparison window ceiling is clamped to the last valid day of the prior month so February short-month edge cases are handled correctly.

A store is flagged when:

```
deviation_pct = (current_value - prior_value) / prior_value × 100

Flag if:  deviation_pct < -15.0    (MoM drop threshold)
       OR kra_ach_pct   <  90.0    (absolute KRA floor)
```

Severity is assigned by Claude during the diagnosis step:

| Deviation | Severity |
|---|---|
| −15% to −20% | `medium` |
| −20% to −30% | `high` |
| Below −30% | `critical` |

---

## Project Structure

```
store-anomaly-monitor/
├── .github/
│   └── workflows/
│       └── daily_run.yml          # GitHub Actions cron — 03:30 UTC / 09:00 IST
├── agents/
│   ├── __init__.py
│   ├── analyst_agent.py           # Claude API forced tool_use → structured diagnosis per anomaly
│   ├── communicator_agent.py      # HTML email composition and Gmail SMTP delivery
│   ├── graph.py                   # LangGraph StateGraph wiring monitor → analyst → communicator
│   └── monitor_agent.py           # Supabase query → MTD KPI aggregation → anomaly detection
├── data/
│   └── store_daily_sales.csv      # Synthetic 6-month dataset (20 stores × ~180 days)
├── scripts/
│   ├── generate_data.py           # Generates synthetic data with deterministic seeded anomalies
│   └── seed_supabase.py           # Bulk-loads CSV into Supabase, creates tables if absent
├── .env.example                   # Template listing all required environment variables
├── .gitignore
├── DECISIONS.md                   # Seven architecture decision records
├── Dockerfile                     # Container image for local or CI execution
├── main.py                        # Pipeline entry point — runs the full LangGraph graph
└── requirements.txt               # Five runtime dependencies
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/CreationSingh01/store-anomaly-monitor.git
cd store-anomaly-monitor
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in all five values:

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (from console.anthropic.com) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase anon/service-role key |
| `GMAIL_SENDER` | Gmail address used to send alerts |
| `GMAIL_APP_PASSWORD` | Gmail App Password (not your account login password) |

> **Mock mode:** Set `ANTHROPIC_API_KEY=add_later` and `GMAIL_APP_PASSWORD=add_later` to run the full pipeline without live credentials. Claude diagnoses will use deterministic templates; emails will print to console instead of being sent.

### 3. Create Supabase tables

Run the following SQL in your Supabase SQL Editor:

```sql
-- Raw daily sales data
CREATE TABLE store_daily_sales (
    id                  bigserial PRIMARY KEY,
    date                date NOT NULL,
    store_id            text NOT NULL,
    store_name          text NOT NULL,
    region              text NOT NULL,
    gross_sales         numeric NOT NULL,
    transactions        integer NOT NULL,
    walkin_count        integer NOT NULL,
    daily_sales_target  numeric NOT NULL,
    daily_walkin_target integer NOT NULL
);

-- MTD summary (upserted daily)
CREATE TABLE store_mtd_summary (
    store_id                text NOT NULL,
    store_name              text NOT NULL,
    region                  text NOT NULL,
    year                    integer NOT NULL,
    month                   integer NOT NULL,
    days_elapsed            integer,
    mtd_sales               numeric,
    mtd_transactions        integer,
    mtd_walkins             integer,
    mtd_sales_target        numeric,
    mtd_walkin_target       integer,
    sales_achievement_pct   numeric,
    walkin_achievement_pct  numeric,
    avg_basket_value        numeric,
    projected_monthly_sales numeric,
    updated_at              timestamptz DEFAULT now(),
    PRIMARY KEY (store_id, year, month)
);

-- Anomaly alert ledger
CREATE TABLE anomaly_alerts (
    id             bigserial PRIMARY KEY,
    store_id       text NOT NULL,
    store_name     text,
    region         text,
    alert_date     date NOT NULL,
    year           integer,
    month          integer,
    anomaly_type   text,
    metric_name    text,
    actual_value   numeric,
    expected_value numeric,
    deviation_pct  numeric,
    severity       text,
    description    text,
    status         text DEFAULT 'open',
    alert_sent     boolean NOT NULL DEFAULT false,
    alert_sent_at  timestamptz,
    UNIQUE (store_id, alert_date, anomaly_type, metric_name)
);

-- Agent execution audit log
CREATE TABLE agent_run_log (
    run_id           text PRIMARY KEY,
    agent_name       text NOT NULL,
    run_date         date NOT NULL,
    status           text NOT NULL,
    stores_analyzed  integer,
    anomalies_found  integer,
    alerts_created   integer,
    emails_sent      integer,
    error_message    text,
    duration_seconds numeric,
    completed_at     timestamptz
);
```

### 4. Seed data

```bash
python scripts/generate_data.py   # writes data/store_daily_sales.csv
python scripts/seed_supabase.py   # loads CSV into Supabase
```

### 5. Run the pipeline

```bash
python main.py
```

---

## Sample Alert Output

```
====================================================================
  Store Anomaly Monitor — Pipeline Run
  Reference date : 2025-04-17
====================================================================

── [graph] monitor_node ─────────────────────────────────────────────
[monitor_agent] run_id=a3f8c1d2-...  ref_date=2025-04-17
[monitor_agent] Fetching current period  : 2025-04-01 → 2025-04-17
[monitor_agent] Fetching previous period : 2025-03-01 → 2025-03-17
[monitor_agent] Rows — current: 340, previous: 340
[monitor_agent] Stores analysed : 20
[monitor_agent] Anomalies found : 11
  ↳ STR_003 | low_kra      | kra_ach_pct      = 82.4  (-7.6%)
  ↳ STR_003 | abv_drop     | pdps             = 41250.0 (-18.3%)
  ↳ STR_009 | abv_drop     | avg_basket_value = 1840.0  (-22.7%)
  ↳ STR_009 | abv_drop     | pdps             = 53100.0 (-19.1%)
  ↳ STR_014 | walkin_drop  | walkin_count     = 312.0   (-34.6%)
  ↳ STR_014 | low_kra      | kra_ach_pct      = 77.1    (-12.9%)
  ↳ STR_017 | low_kra      | kra_ach_pct      = 68.3    (-21.7%)
  ↳ STR_017 | abv_drop     | avg_basket_value = 1620.0  (-31.4%)
  ↳ STR_017 | abv_drop     | pdps             = 38400.0 (-28.9%)
  ↳ STR_017 | walkin_drop  | walkin_count     = 284.0   (-38.2%)
[monitor_agent] Upserting MTD summary...
[monitor_agent] Done in 2.41s

── [graph] analyst_node ─────────────────────────────────────────────
[analyst_agent] run_id=b7e2a4f9-...  ref_date=2025-04-17  mock=False
[analyst_agent] MTD context loaded for 4 store(s)
[analyst_agent] [1/10] STR_003 | low_kra | kra_ach_pct (-7.6%)
  → severity=medium    action=Run an immediate floor audit and compare last 7 days...
[analyst_agent] [2/10] STR_003 | abv_drop | pdps (-18.3%)
  → severity=medium    action=Audit availability of the top-20 revenue SKUs and bri...
[analyst_agent] [3/10] STR_009 | abv_drop | avg_basket_value (-22.7%)
  → severity=high      action=Brief the floor team on upselling and bundling scripts...
[analyst_agent] [4/10] STR_009 | abv_drop | pdps (-19.1%)
  → severity=medium    action=Audit availability of the top-20 revenue SKUs and bri...
[analyst_agent] [5/10] STR_014 | walkin_drop | walkin_count (-34.6%)
  → severity=critical  action=Review local competitor activity and activate a short-...
[analyst_agent] [6/10] STR_014 | low_kra | kra_ach_pct (-12.9%)
  → severity=medium    action=Run an immediate floor audit focusing on conversion rat...
[analyst_agent] [7/10] STR_017 | low_kra | kra_ach_pct (-21.7%)
  → severity=high      action=Escalate to the regional manager for an on-site diagno...
[analyst_agent] [8/10] STR_017 | abv_drop | avg_basket_value (-31.4%)
  → severity=critical  action=Escalate to the regional manager for an on-site diagno...
[analyst_agent] [9/10] STR_017 | abv_drop | pdps (-28.9%)
  → severity=high      action=Escalate to the regional manager for an on-site diagno...
[analyst_agent] [10/10] STR_017 | walkin_drop | walkin_count (-38.2%)
  → severity=critical  action=Escalate to the regional manager for an on-site diagno...
[analyst_agent] Done in 18.74s — 10 alert(s) inserted.

── [graph] communicator_node ────────────────────────────────────────
[communicator_agent] run_id=c9d1b3e7-...  ref_date=2025-04-17  mock=False
[communicator_agent] 10 anomalies across 4 store(s) → 4 email(s)
[communicator_agent] STR_003 (2 flags) → Store Alert: Koramangala Central — 2 anomalies detected (2025-04-17)
  ✓ Sent to area.manager@retailco.in
[communicator_agent] STR_009 (2 flags) → Store Alert: Whitefield East — 2 anomalies detected (2025-04-17)
  ✓ Sent to area.manager@retailco.in
[communicator_agent] STR_014 (2 flags) → Store Alert: Indiranagar North — 2 anomalies detected (2025-04-17)
  ✓ Sent to area.manager@retailco.in
[communicator_agent] STR_017 (4 flags) → Store Alert: HSR Layout South — 4 anomalies detected (2025-04-17)
  ✓ Sent to area.manager@retailco.in
[communicator_agent] Done in 4.12s — 4 email(s) sent.

====================================================================
  PIPELINE SUMMARY
====================================================================
  Stores checked       : 20
  Stores with flags    : 4
  Anomalies detected   : 10
  Diagnoses generated  : 10
  Emails sent/printed  : 4
  Errors               : none
====================================================================
```

---

## Architecture Decisions

Seven documented decisions covering pipeline design, comparison logic, AI output structure, email grouping strategy, synthetic data design, mock fallback mode, and Supabase as the unified data layer:

[DECISIONS.md](./DECISIONS.md)

---

*Built by **Data Transformer AI Solutions***
