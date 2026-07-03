"""Feature engineering — four signal layers.

Outputs: data/ieee-fraud-detection/train_engineered.parquet

Run from project root:
    source venv/bin/activate
    python src/fraud_detection_demo/01_feature_engineering.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path("data/ieee-fraud-detection")
OUT_PATH = DATA_DIR / "train_engineered.parquet"


def add_velocity_features(df: pd.DataFrame) -> pd.DataFrame:
    """Layer 1: rolling tx count and value per card over 1h and 24h windows."""
    df = df.sort_values("TransactionDT").copy()
    # TransactionDT is seconds since a reference epoch
    for window_secs, label in [(3600, "1h"), (86400, "24h")]:
        counts, values = [], []
        for _, group in df.groupby("card1", sort=False):
            dt = group["TransactionDT"].values
            amt = group["TransactionAmt"].values
            cnt = np.zeros(len(group), dtype=np.int32)
            val = np.zeros(len(group), dtype=np.float32)
            for i in range(len(group)):
                mask = (dt >= dt[i] - window_secs) & (dt < dt[i])
                cnt[i] = mask.sum()
                val[i] = amt[mask].sum()
            counts.append(pd.Series(cnt, index=group.index))
            values.append(pd.Series(val, index=group.index))
        df[f"velocity_count_{label}"] = pd.concat(counts).reindex(df.index)
        df[f"velocity_value_{label}"] = pd.concat(values).reindex(df.index)
    return df


def add_ratio_features(df: pd.DataFrame) -> pd.DataFrame:
    """Layer 2: account-history ratio signals from C columns and email nulls."""
    # C1–C14 encode cumulative counts (chargeback history etc.)
    c_cols = [c for c in df.columns if c.startswith("C") and c[1:].isdigit()]
    if c_cols:
        df["c_total"] = df[c_cols].fillna(0).sum(axis=1)
        df["c_max"] = df[c_cols].fillna(0).max(axis=1)
    # Missing R_emaildomain as a risk signal (high null rate = unknown receiver domain)
    df["r_email_missing"] = df["R_emaildomain"].isnull().astype(np.int8)
    df["p_email_missing"] = df["P_emaildomain"].isnull().astype(np.int8)
    return df


def add_interaction_features(df: pd.DataFrame) -> pd.DataFrame:
    """Layer 3: high-risk combination flags."""
    # D1 = days since first address use — proxy for account age
    addr_age = df["D1"].fillna(999)  # unknown → treat as established
    new_acct = addr_age < 7
    high_value = df["TransactionAmt"] > 500
    express = df["ProductCD"] == "S"  # 'S' = service/subscription, closest to express
    df["new_acct_highval_express"] = (new_acct & high_value & express).astype(np.int8)
    df["new_acct_flag"] = new_acct.astype(np.int8)
    df["addr_age_days"] = addr_age.clip(upper=365)
    return df


def add_network_features(df: pd.DataFrame) -> pd.DataFrame:
    """Layer 4: network/address graph signals."""
    # Country mismatch: card issuer country (card4 brand) vs receiver email TLD
    def email_tld(series: pd.Series) -> pd.Series:
        return series.fillna("").str.split(".").str[-1].str.lower()

    p_tld = email_tld(df.get("P_emaildomain", pd.Series([""] * len(df), index=df.index)))
    r_tld = email_tld(df.get("R_emaildomain", pd.Series([""] * len(df), index=df.index)))
    df["email_domain_mismatch"] = (
        (p_tld != r_tld) & (p_tld != "") & (r_tld != "")
    ).astype(np.int8)

    # Shared device linkage: how many transactions share this DeviceInfo
    if "DeviceInfo" in df.columns:
        device_counts = df["DeviceInfo"].map(df["DeviceInfo"].value_counts())
        df["device_linkage_count"] = device_counts.fillna(1).astype(np.int32)
    else:
        df["device_linkage_count"] = 1

    # Shared P_emaildomain linkage count
    email_counts = df["P_emaildomain"].map(
        df["P_emaildomain"].value_counts()
    )
    df["email_domain_linkage_count"] = email_counts.fillna(1).astype(np.int32)

    return df


def main() -> None:
    print("Loading transaction + identity data...")
    txn = pd.read_csv(DATA_DIR / "train_transaction.csv")
    idn = pd.read_csv(DATA_DIR / "train_identity.csv")

    print(f"Transactions: {len(txn):,} rows")

    # Left-join identity; missing identity is itself a signal
    df = txn.merge(idn, on="TransactionID", how="left")
    df["has_identity"] = df["id_01"].notna().astype(np.int8)
    print(f"Joined: {len(df):,} rows  |  identity coverage: {df['has_identity'].mean():.1%}")

    # dist1: impute median + missing flag
    dist1_median = df["dist1"].median()
    df["dist1_missing"] = df["dist1"].isnull().astype(np.int8)
    df["dist1"] = df["dist1"].fillna(dist1_median)

    print("Adding velocity features (slow — ~2–3 min)...")
    df = add_velocity_features(df)

    print("Adding ratio features...")
    df = add_ratio_features(df)

    print("Adding interaction features...")
    df = add_interaction_features(df)

    print("Adding network features...")
    df = add_network_features(df)

    print(f"Saving to {OUT_PATH} ...")
    df.to_parquet(OUT_PATH, index=False)
    print(f"Saved. Shape: {df.shape}  |  New feature columns: velocity_count_1h, velocity_value_1h, velocity_count_24h, velocity_value_24h, c_total, c_max, r_email_missing, p_email_missing, new_acct_highval_express, new_acct_flag, addr_age_days, email_domain_mismatch, device_linkage_count, email_domain_linkage_count, has_identity, dist1_missing")
    print("Feature engineering complete.")


if __name__ == "__main__":
    main()
