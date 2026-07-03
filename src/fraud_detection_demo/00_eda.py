"""EDA — IEEE-CIS Fraud Detection dataset.

Run from project root:
    source venv/bin/activate
    python src/fraud_detection_demo/00_eda.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

DATA_DIR = Path("data/ieee-fraud-detection")


def main() -> None:
    print("Loading data...")
    txn = pd.read_csv(DATA_DIR / "train_transaction.csv")
    idn = pd.read_csv(DATA_DIR / "train_identity.csv")

    print(f"\n=== Transactions ===")
    print(f"  Rows: {len(txn):,}  |  Columns: {txn.shape[1]}")
    fraud_rate = txn["isFraud"].mean()
    print(f"  Fraud rate: {fraud_rate:.2%}  ({txn['isFraud'].sum():,} fraud / {len(txn):,} total)")
    print(f"  Avg amount: ${txn['TransactionAmt'].mean():.2f}")
    print(f"  Median amount: ${txn['TransactionAmt'].median():.2f}")

    print(f"\n=== Identity join coverage ===")
    joined = txn.merge(idn, on="TransactionID", how="left", indicator=True)
    coverage = (joined["_merge"] == "both").mean()
    print(f"  Transactions with identity record: {coverage:.1%}")

    print(f"\n=== Top 20 columns by null rate ===")
    null_rates = txn.isnull().mean().sort_values(ascending=False).head(20)
    for col, rate in null_rates.items():
        if rate > 0:
            print(f"  {col:30s}  {rate:.1%} null")

    print(f"\n=== Key null rates ===")
    for col in ["dist1", "dist2", "R_emaildomain", "P_emaildomain"]:
        if col in txn.columns:
            print(f"  {col}: {txn[col].isnull().mean():.1%} null")

    print(f"\n=== ProductCD distribution ===")
    print(txn["ProductCD"].value_counts().to_string())

    print(f"\n=== Card type distribution ===")
    if "card4" in txn.columns:
        print(txn["card4"].value_counts().to_string())

    print(f"\n=== TransactionAmt percentiles ===")
    pcts = txn["TransactionAmt"].quantile([0.25, 0.5, 0.75, 0.90, 0.95, 0.99])
    print(pcts.to_string())

    print(f"\n=== Fraud vs non-fraud amount stats ===")
    print(txn.groupby("isFraud")["TransactionAmt"].describe().round(2).to_string())

    print("\nEDA complete.")


if __name__ == "__main__":
    main()
