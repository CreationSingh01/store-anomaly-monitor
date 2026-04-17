import pandas as pd
import numpy as np
from datetime import date, timedelta
import os

STORES = [
    ("STR_001", "Hyderabad - Banjara Hills", "South"),
    ("STR_002", "Hyderabad - Jubilee Hills", "South"),
    ("STR_003", "Chennai - T. Nagar", "South"),
    ("STR_004", "Chennai - Anna Nagar", "South"),
    ("STR_005", "Bengaluru - Koramangala", "South"),
    ("STR_006", "Bengaluru - Indiranagar", "South"),
    ("STR_007", "Delhi - Connaught Place", "North"),
    ("STR_008", "Delhi - Lajpat Nagar", "North"),
    ("STR_009", "Noida - Sector 18", "North"),
    ("STR_010", "Gurgaon - DLF Phase 4", "North"),
    ("STR_011", "Jaipur - MI Road", "North"),
    ("STR_012", "Kolkata - Park Street", "East"),
    ("STR_013", "Kolkata - New Market", "East"),
    ("STR_014", "Bhubaneswar - Saheed Nagar", "East"),
    ("STR_015", "Patna - Fraser Road", "East"),
    ("STR_016", "Mumbai - Andheri West", "West"),
    ("STR_017", "Mumbai - Bandra", "West"),
    ("STR_018", "Pune - FC Road", "West"),
    ("STR_019", "Ahmedabad - CG Road", "West"),
    ("STR_020", "Surat - Ring Road", "West"),
]

BASE_SALES = {
    "STR_001": 420000, "STR_002": 390000, "STR_003": 350000, "STR_004": 330000,
    "STR_005": 460000, "STR_006": 440000, "STR_007": 500000, "STR_008": 370000,
    "STR_009": 410000, "STR_010": 480000, "STR_011": 310000, "STR_012": 360000,
    "STR_013": 340000, "STR_014": 290000, "STR_015": 270000, "STR_016": 520000,
    "STR_017": 490000, "STR_018": 430000, "STR_019": 400000, "STR_020": 380000,
}

BASE_TRANSACTIONS = {sid: int(base / 850) for sid, base in BASE_SALES.items()}
BASE_WALKINS = {sid: int(txns * 1.35) for sid, txns in BASE_TRANSACTIONS.items()}

TARGET_MARGIN = 1.05


def month_seasonality(m: int) -> float:
    factors = {11: 1.10, 12: 1.35, 1: 0.82, 2: 0.90, 3: 0.95, 4: 1.00}
    return factors.get(m, 1.0)


def generate_rows(rng: np.random.Generator) -> list[dict]:
    start = date(2024, 11, 1)
    end = date(2025, 4, 17)
    rows = []

    current = start
    while current <= end:
        dow = current.weekday()
        is_weekend = dow >= 5
        weekend_mult = 1.35 if is_weekend else 1.0
        dom = current.day
        month_days = (date(current.year, current.month % 12 + 1, 1) - timedelta(days=1)).day if current.month < 12 else 31
        month_end_mult = 1.12 if dom >= month_days - 2 else 1.0
        month_mult = month_seasonality(current.month)

        for store_id, store_name, region in STORES:
            base_s = BASE_SALES[store_id]
            base_t = BASE_TRANSACTIONS[store_id]
            base_w = BASE_WALKINS[store_id]

            sales_mult = month_mult * weekend_mult * month_end_mult
            txn_mult = month_mult * weekend_mult * month_end_mult
            walkin_mult = month_mult * weekend_mult * month_end_mult

            # April 2025 anomaly seeding
            if current >= date(2025, 4, 1):
                if store_id == "STR_003":
                    # Low KRA: 72-84% achievement vs target
                    sales_mult *= rng.uniform(0.72, 0.84)
                    txn_mult *= rng.uniform(0.75, 0.86)
                elif store_id == "STR_009":
                    # ABV drop: avg basket value 68-78% of base
                    txn_mult *= rng.uniform(1.0, 1.05)       # normal footfall
                    sales_mult *= rng.uniform(0.68, 0.78)    # low ABV reflected in sales
                elif store_id == "STR_014":
                    walkin_mult *= rng.uniform(0.62, 0.74)   # walkin drop
                    txn_mult *= rng.uniform(0.65, 0.76)
                    sales_mult *= rng.uniform(0.68, 0.78)
                elif store_id == "STR_017":
                    sales_mult *= rng.uniform(0.65, 0.78)
                    txn_mult *= rng.uniform(0.67, 0.80)
                    walkin_mult *= rng.uniform(0.63, 0.76)

            noise_s = rng.normal(1.0, 0.04)
            noise_t = rng.normal(1.0, 0.03)
            noise_w = rng.normal(1.0, 0.03)

            gross_sales = round(base_s * sales_mult * noise_s, 2)
            transactions = max(1, int(base_t * txn_mult * noise_t))
            walkin_count = max(transactions, int(base_w * walkin_mult * noise_w))

            daily_sales_target = round(base_s * month_mult * TARGET_MARGIN, 2)
            daily_walkin_target = int(base_w * month_mult * TARGET_MARGIN)

            rows.append({
                "store_id": store_id,
                "store_name": store_name,
                "region": region,
                "date": current.isoformat(),
                "gross_sales": gross_sales,
                "transactions": transactions,
                "walkin_count": walkin_count,
                "daily_sales_target": daily_sales_target,
                "daily_walkin_target": daily_walkin_target,
            })

        current += timedelta(days=1)

    return rows


def main():
    rng = np.random.default_rng(seed=42)
    rows = generate_rows(rng)
    df = pd.DataFrame(rows)

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "store_daily_sales.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    df.to_csv(out_path, index=False)

    print(f"Rows generated : {len(df):,}")
    print(f"Date range     : {df['date'].min()} -> {df['date'].max()}")
    print(f"Stores         : {df['store_id'].nunique()}")
    print("\nAnomaly stores (April 2025 behaviour):")
    anomalies = {
        "STR_003": "Low KRA -- 72-84% sales achievement",
        "STR_009": "ABV drop -- basket value 68-78% of baseline",
        "STR_014": "Walk-in drop -- footfall 62-74% of baseline",
        "STR_017": "All metrics weak -- sales/txn/walkin suppressed",
    }
    for sid, desc in anomalies.items():
        print(f"  {sid}: {desc}")
    print(f"\nSaved to: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
