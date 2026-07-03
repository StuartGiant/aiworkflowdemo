"""SHAP reason codes — per-transaction explainability export.

Appends to: src/fraud_detection_demo/demo/data_export.json

Run from project root:
    source venv/bin/activate
    python src/fraud_detection_demo/05_shap_export.py
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import shap

DATA_DIR = Path("data/ieee-fraud-detection")
DEMO_DIR = Path("src/fraud_detection_demo/demo")
EXPORT_PATH = DEMO_DIR / "data_export.json"
MODEL_PATH = Path("models/fraud_xgb_calibrated.pkl")

N_EXPLAIN = 1000   # number of transactions to compute SHAP for
TOP_K = 5          # top-k features per transaction


def main() -> None:
    print("Loading model...")
    with open(MODEL_PATH, "rb") as f:
        bundle = pickle.load(f)
    xgb_base = bundle["xgb_base"]
    iso = bundle["iso"]
    feature_names = bundle["feature_names"]

    print("Loading test set...")
    df_pred = pd.read_parquet(DATA_DIR / "test_predictions.parquet")
    eng = pd.read_parquet(DATA_DIR / "train_engineered.parquet")
    eng_test = eng[eng["TransactionID"].isin(df_pred["TransactionID"])].copy()
    eng_test = eng_test.set_index("TransactionID").loc[df_pred["TransactionID"].values].reset_index()

    drop_cols = ["TransactionID", "TransactionDT", "isFraud",
                 "card2", "card3", "card5", "addr1", "addr2",
                 "P_emaildomain", "R_emaildomain", "DeviceInfo", "DeviceType",
                 "card4", "card6", "ProductCD",
                 "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9",
                 "id_12", "id_15", "id_16", "id_23", "id_27", "id_28",
                 "id_29", "id_30", "id_31", "id_32", "id_33", "id_34",
                 "id_35", "id_36", "id_37", "id_38"]
    drop_cols = [c for c in drop_cols if c in eng_test.columns]
    X_test = eng_test.drop(columns=drop_cols)
    for col in X_test.select_dtypes("object").columns:
        X_test[col] = X_test[col].astype("category").cat.codes
    for col in feature_names:
        if col not in X_test.columns:
            X_test[col] = 0
    X_test = X_test[feature_names]

    # Sample N_EXPLAIN transactions — stratified: half fraud, half legit
    fraud_idx = eng_test[eng_test["isFraud"] == 1].index[:N_EXPLAIN // 2]
    legit_idx = eng_test[eng_test["isFraud"] == 0].index[:N_EXPLAIN // 2]
    sample_idx = fraud_idx.tolist() + legit_idx.tolist()

    # Map to positional indices within eng_test
    pos_map = {orig: pos for pos, orig in enumerate(eng_test.index)}
    sample_pos = [pos_map[i] for i in sample_idx if i in pos_map]

    X_sample = X_test.iloc[sample_pos]
    y_sample = eng_test["isFraud"].iloc[sample_pos].values
    tid_sample = eng_test["TransactionID"].iloc[sample_pos].values
    y_prob_sample = df_pred.set_index("TransactionID").loc[tid_sample, "y_prob"].values

    print(f"Computing SHAP values for {len(X_sample)} transactions...")
    explainer = shap.TreeExplainer(xgb_base)
    shap_values = explainer.shap_values(X_sample)

    # Build per-transaction records
    records = []
    ui_feature_keys = [
        "TransactionAmt", "addr_age_days", "email_domain_mismatch",
        "new_acct_highval_express", "velocity_count_1h", "velocity_value_1h",
        "velocity_count_24h", "velocity_value_24h", "device_linkage_count",
        "has_identity", "dist1", "c_total",
    ]
    for i, (tid, y_true, y_prob) in enumerate(zip(tid_sample, y_sample, y_prob_sample)):
        sv = shap_values[i]
        # Top-K by absolute SHAP value
        top_idx = np.argsort(np.abs(sv))[::-1][:TOP_K]
        top5 = [
            {
                "feature": feature_names[j],
                "value": round(float(X_sample.iloc[i, j]), 4),
                "shap": round(float(sv[j]), 4),
            }
            for j in top_idx
        ]
        key_features = {
            k: round(float(X_sample.iloc[i][k]), 4)
            for k in ui_feature_keys
            if k in X_sample.columns
        }
        records.append({
            "transaction_id": int(tid),
            "y_true": int(y_true),
            "fraud_prob": round(float(y_prob), 4),
            "shap_top5": top5,
            "key_features": key_features,
        })

    print(f"Built {len(records)} SHAP records")

    # Review queue: top 5 by expected loss (amount × fraud_prob), fraud only
    queue_candidates = [r for r in records if r["fraud_prob"] > 0.3]
    queue_candidates.sort(
        key=lambda r: r["key_features"].get("TransactionAmt", 0) * r["fraud_prob"],
        reverse=True,
    )
    review_queue = queue_candidates[:5]

    # Load and update export
    with open(EXPORT_PATH) as f:
        export = json.load(f)

    export["transactions"] = records
    export["review_queue"] = review_queue

    with open(EXPORT_PATH, "w") as f:
        json.dump(export, f)

    print(f"SHAP export saved → {EXPORT_PATH}")
    print("SHAP reason codes complete.")


if __name__ == "__main__":
    main()
