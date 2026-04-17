import os
import sys
import math
import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "store_daily_sales.csv")
BATCH_SIZE = 500

CREATE_TABLE_SQL = """
create table if not exists store_daily_sales (
    id              bigserial primary key,
    store_id        text        not null,
    store_name      text        not null,
    region          text        not null,
    date            date        not null,
    gross_sales     numeric     not null,
    transactions    integer     not null,
    walkin_count    integer     not null,
    daily_sales_target  numeric not null,
    daily_walkin_target integer not null,
    unique (store_id, date)
);
"""


def get_client() -> Client:
    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in .env", file=sys.stderr)
        sys.exit(1)
    return create_client(url, key)


def ensure_table(client: Client) -> None:
    # Use rpc to execute raw DDL via a Postgres function if available,
    # otherwise fall back to a direct REST check (table will be created
    # in Supabase dashboard or via migration before running this script).
    try:
        client.rpc("exec_sql", {"query": CREATE_TABLE_SQL}).execute()
        print("Table check/create via exec_sql RPC succeeded.")
    except Exception:
        # exec_sql RPC likely doesn't exist — attempt raw query via postgrest
        try:
            client.postgrest.schema("public")  # no-op, just validates connection
            print(
                "NOTE: Could not auto-create table via RPC.\n"
                "Please run the following SQL in your Supabase SQL editor once:\n"
                f"\n{CREATE_TABLE_SQL}"
            )
        except Exception as e:
            print(f"Connection error: {e}", file=sys.stderr)
            sys.exit(1)


def load_csv() -> list[dict]:
    df = pd.read_csv(CSV_PATH)
    df["date"] = df["date"].astype(str)
    df["gross_sales"] = df["gross_sales"].round(2)
    df["daily_sales_target"] = df["daily_sales_target"].round(2)
    for col in ("transactions", "walkin_count", "daily_walkin_target"):
        df[col] = df[col].astype(int)
    return df.to_dict(orient="records")


def insert_batches(client: Client, rows: list[dict]) -> int:
    total = len(rows)
    n_batches = math.ceil(total / BATCH_SIZE)
    inserted = 0

    for i in range(n_batches):
        batch = rows[i * BATCH_SIZE : (i + 1) * BATCH_SIZE]
        client.table("store_daily_sales").upsert(batch, on_conflict="store_id,date").execute()
        inserted += len(batch)
        pct = inserted / total * 100
        print(f"  Batch {i + 1}/{n_batches} — {inserted:,}/{total:,} rows ({pct:.1f}%)")

    return inserted


def main():
    print("Loading CSV...")
    rows = load_csv()
    print(f"  {len(rows):,} rows read from {os.path.abspath(CSV_PATH)}")

    print("\nConnecting to Supabase...")
    client = get_client()
    print("  Connected.")

    print("\nEnsuring table exists...")
    ensure_table(client)

    print(f"\nInserting in batches of {BATCH_SIZE}...")
    total_inserted = insert_batches(client, rows)

    print(f"\nDone. Total rows inserted/upserted: {total_inserted:,}")


if __name__ == "__main__":
    main()
