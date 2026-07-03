"""Adversarial / drift simulation — amount-capping evasion strategy.

Appends to: src/fraud_detection_demo/demo/data_export.json

Strategy: fraudsters cap transaction amounts just under the velocity threshold
($200) to evade amount-based signals. Simulates by clipping fraud amounts in
the test set to ≤$200 and re-scoring with the fixed model.

Run from project root:
    source venv/bin/activate
    python src/fraud_detection_demo/04_adversarial_drift.py
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, recall_score

DATA_DIR = Path("data/ieee-fraud-detection")
DEMO_DIR = Path("src/fraud_detection_demo/demo")
EXPORT_PATH = DEMO_DIR / "data_export.json"
MODEL_PATH = Path("models/fraud_xgb_calibrated.pkl")

EVASION_CAP = 50.0    # fraudsters split transactions into small amounts (≤$50)
VELOCITY_CAP = 80.0   # and spread across time, suppressing velocity value signal
PSI_ALARM = 0.2       # PSI threshold that triggers a drift alarm


def compute_psi(baseline: np.ndarray, shifted: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index between two distributions.

    Uses quantile-based bin edges from the baseline so each bucket has equal
    population — far more sensitive than uniform bins for skewed financial data.
    """
    edges = np.unique(np.percentile(baseline, np.linspace(0, 100, bins + 1)))
    if len(edges) < 3:
        return 0.0

    base_counts, _ = np.histogram(baseline, bins=edges)
    shift_counts, _ = np.histogram(shifted, bins=edges)

    base_pct = (base_counts + 1e-6) / len(baseline)
    shift_pct = (shift_counts + 1e-6) / len(shifted)

    psi = np.sum((shift_pct - base_pct) * np.log(shift_pct / base_pct))
    return float(psi)


def main() -> None:
    print("Loading model and test predictions...")
    with open(MODEL_PATH, "rb") as f:
        bundle = pickle.load(f)
    xgb_base = bundle["xgb_base"]
    iso = bundle["iso"]
    feature_names = bundle["feature_names"]

    def score(X: pd.DataFrame) -> np.ndarray:
        return iso.predict(xgb_base.predict_proba(X)[:, 1])


    df = pd.read_parquet(DATA_DIR / "test_predictions.parquet")
    # Load full feature matrix for test rows
    eng = pd.read_parquet(DATA_DIR / "train_engineered.parquet")
    test_idx = df.index  # aligned by position after re-parquet save

    # Re-derive test features from the engineered parquet using TransactionID
    eng_test = eng[eng["TransactionID"].isin(df["TransactionID"])].copy()
    eng_test = eng_test.set_index("TransactionID").loc[df["TransactionID"].values].reset_index()

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
    # Align columns
    for col in feature_names:
        if col not in X_test.columns:
            X_test[col] = 0
    X_test = X_test[feature_names]

    y_true = eng_test["isFraud"].values
    y_prob_baseline = score(X_test)

    # Load the optimal threshold from data_export.json
    with open(EXPORT_PATH) as f:
        export = json.load(f)
    threshold = export.get("optimal_threshold", 0.5)

    baseline_recall = recall_score(y_true, (y_prob_baseline >= threshold).astype(int), zero_division=0)
    baseline_prauc = average_precision_score(y_true, y_prob_baseline)
    print(f"Baseline  — recall: {baseline_recall:.3f}  PR-AUC: {baseline_prauc:.4f}  (threshold={threshold:.3f})")

    # Adversarial shift: fraudsters split transactions into micro-amounts (≤$50)
    # and spread across time to suppress velocity signals.
    # Apply to all fraud rows + 30% of legit rows (population-level drift effect)
    # so PSI on the overall distribution is detectable.
    rng = np.random.default_rng(42)
    fraud_mask = y_true == 1
    legit_mask = ~fraud_mask
    legit_shift = legit_mask & (rng.random(len(y_true)) < 0.30)
    shift_mask = fraud_mask | legit_shift

    X_shifted = X_test.copy()
    X_shifted.loc[shift_mask, "TransactionAmt"] = (
        X_shifted.loc[shift_mask, "TransactionAmt"].clip(upper=EVASION_CAP)
    )
    if "velocity_value_1h" in X_shifted.columns:
        X_shifted.loc[shift_mask, "velocity_value_1h"] = (
            X_shifted.loc[shift_mask, "velocity_value_1h"].clip(upper=VELOCITY_CAP)
        )
    if "velocity_value_24h" in X_shifted.columns:
        X_shifted.loc[shift_mask, "velocity_value_24h"] = (
            X_shifted.loc[shift_mask, "velocity_value_24h"].clip(upper=VELOCITY_CAP * 5)
        )

    y_prob_shifted = score(X_shifted)
    shifted_recall = recall_score(y_true, (y_prob_shifted >= threshold).astype(int), zero_division=0)
    shifted_prauc = average_precision_score(y_true, y_prob_shifted)
    print(f"Post-drift — recall: {shifted_recall:.3f}  PR-AUC: {shifted_prauc:.4f}")
    print(f"Recall drop: {baseline_recall - shifted_recall:.3f}  ({(baseline_recall - shifted_recall)/baseline_recall:.1%} relative)")

    # PR curve for shifted distribution
    from sklearn.metrics import precision_recall_curve
    prec_s, rec_s, thresh_s = precision_recall_curve(y_true, y_prob_shifted)
    thresh_s = np.append(thresh_s, 1.0)
    pr_curve_shifted = [
        {"threshold": round(float(t), 4), "precision": round(float(p), 4), "recall": round(float(r), 4)}
        for t, p, r in zip(thresh_s, prec_s, rec_s)
    ]

    # PSI for key features
    amt_baseline = X_test["TransactionAmt"].values
    amt_shifted = X_shifted["TransactionAmt"].values
    psi_amount = compute_psi(amt_baseline, amt_shifted)

    psi_velocity = 0.0
    if "velocity_value_1h" in X_test.columns:
        psi_velocity = compute_psi(
            X_test["velocity_value_1h"].values,
            X_shifted["velocity_value_1h"].values,
        )

    print(f"PSI — TransactionAmt: {psi_amount:.3f}  velocity_value_1h: {psi_velocity:.3f}  (alarm > {PSI_ALARM})")

    export["adversarial"] = {
        "strategy": "transaction_splitting",
        "evasion_cap": EVASION_CAP,
        "baseline_recall": round(baseline_recall, 4),
        "shifted_recall": round(shifted_recall, 4),
        "recall_drop": round(baseline_recall - shifted_recall, 4),
        "baseline_prauc": round(baseline_prauc, 4),
        "shifted_prauc": round(shifted_prauc, 4),
        "psi_amount": round(psi_amount, 4),
        "psi_velocity": round(psi_velocity, 4),
        "psi_alarm_threshold": PSI_ALARM,
        "pr_curve_shifted": pr_curve_shifted,
    }

    with open(EXPORT_PATH, "w") as f:
        json.dump(export, f)
    print(f"Adversarial data saved → {EXPORT_PATH}")
    print("Adversarial drift simulation complete.")


if __name__ == "__main__":
    main()
