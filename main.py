"""
main.py
-------
Entry point for the Store Anomaly Monitor pipeline.
Runs the full LangGraph pipeline (monitor → analyst → communicator) and
prints a final summary to stdout.
"""

from datetime import date

from agents.graph import run_pipeline


def main() -> None:
    ref = date(2025, 4, 17)

    print("=" * 68)
    print("  Store Anomaly Monitor — Pipeline Run")
    print(f"  Reference date : {ref}")
    print("=" * 68)

    state = run_pipeline(ref_date=ref)

    # ── final summary ─────────────────────────────────────────────────────────
    unique_stores  = len({a["store_id"] for a in state["anomalies"]})
    total_anomalies = len(state["anomalies"])
    total_diagnoses = len(state["diagnoses"])
    emails_sent     = state["emails_sent"]
    errors          = state["errors"]

    print("\n" + "=" * 68)
    print("  PIPELINE SUMMARY")
    print("=" * 68)
    print(f"  Stores checked       : 20")
    print(f"  Stores with flags    : {unique_stores}")
    print(f"  Anomalies detected   : {total_anomalies}")
    print(f"  Diagnoses generated  : {total_diagnoses}")
    print(f"  Emails sent/printed  : {emails_sent}")

    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for e in errors:
            print(f"    ✗ {e}")
    else:
        print("  Errors               : none")

    print("=" * 68)


if __name__ == "__main__":
    main()
