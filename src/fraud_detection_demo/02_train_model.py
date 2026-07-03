"""Train XGBoost + isotonic calibration on engineered features.

Outputs:
  models/fraud_xgb_calibrated.pkl
  data/ieee-fraud-detection/test_predictions.parquet

Run from project root:
    source venv/bin/activate
    python src/fraud_detection_demo/02_train_model.py
"""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, fbeta_score, precision_recall_curve
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

DATA_DIR = Path("data/ieee-fraud-detection")
MODEL_DIR = Path("models")
MODEL_DIR.mkdir(exist_ok=True)

# Columns to keep for training (drop IDs, raw strings, leaky cols)
DROP_COLS = [
    "TransactionID", "TransactionDT",
    "card2", "card3", "card5",          # high-cardinality card metadata
    "addr1", "addr2",                   # raw address codes
    "P_emaildomain", "R_emaildomain",   # encoded via ratio/network features
    "DeviceInfo", "DeviceType",         # encoded via network features
    "card4", "card6",                   # string card brand/type
    "ProductCD",                        # string, limited signal beyond interaction flag
    "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9",  # match flags (string)
    "id_12", "id_15", "id_16", "id_23", "id_27", "id_28",   # string identity cols
    "id_29", "id_30", "id_31", "id_32", "id_33", "id_34",
    "id_35", "id_36", "id_37", "id_38",
]

UI_FEATURES = [
    "TransactionAmt", "addr_age_days", "email_domain_mismatch",
    "new_acct_highval_express", "velocity_count_1h", "velocity_value_1h",
    "velocity_count_24h", "velocity_value_24h", "device_linkage_count",
    "has_identity", "dist1", "dist1_missing", "c_total",
]


def load_features(path: Path) -> tuple[pd.DataFrame, pd.Series]:
    df = pd.read_parquet(path)
    y = df["isFraud"].astype(np.int8)
    drop = [c for c in DROP_COLS if c in df.columns] + ["isFraud"]
    X = df.drop(columns=drop)
    # Encode any remaining object columns as category codes
    for col in X.select_dtypes("object").columns:
        X[col] = X[col].astype("category").cat.codes
    return X, y


def main() -> None:
    print("Loading engineered data...")
    X, y = load_features(DATA_DIR / "train_engineered.parquet")
    print(f"Features: {X.shape[1]}  |  Fraud: {y.mean():.2%}")

    # 70% train, 10% calibration, 20% test — stratified
    X_trainval, X_test, y_trainval, y_test = train_test_split(
        X, y, test_size=0.20, stratify=y, random_state=42
    )
    X_train, X_cal, y_train, y_cal = train_test_split(
        X_trainval, y_trainval, test_size=0.125, stratify=y_trainval, random_state=42
    )  # 0.125 of 0.80 = 10% of total

    neg = (y_train == 0).sum()
    pos = (y_train == 1).sum()
    spw = round(neg / pos)
    print(f"Train: {len(X_train):,}  Cal: {len(X_cal):,}  Test: {len(X_test):,}")
    print(f"scale_pos_weight = {spw}  (neg={neg:,} / pos={pos:,})")

    print("Training XGBoost...")
    xgb = XGBClassifier(
        n_estimators=500,
        learning_rate=0.05,
        max_depth=6,
        scale_pos_weight=spw,
        eval_metric="aucpr",
        early_stopping_rounds=30,
        use_label_encoder=False,
        random_state=42,
        n_jobs=-1,
        tree_method="hist",
    )
    xgb.fit(
        X_train, y_train,
        eval_set=[(X_cal, y_cal)],
        verbose=50,
    )
    print(f"Best iteration: {xgb.best_iteration}")

    print("Calibrating with isotonic regression...")
    # CalibratedClassifierCV dropped cv='prefit' in sklearn 1.2+; do it manually:
    # fit an isotonic regressor mapping raw XGB proba → calibrated proba
    raw_cal = xgb.predict_proba(X_cal)[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(raw_cal, y_cal)

    # Evaluate on test set
    raw_test = xgb.predict_proba(X_test)[:, 1]
    y_prob = iso.predict(raw_test)
    pr_auc = average_precision_score(y_test, y_prob)

    # F-beta (β=2, recall-weighted) at cost-neutral threshold 0.5 as baseline
    y_pred = (y_prob >= 0.5).astype(int)
    fb2 = fbeta_score(y_test, y_pred, beta=2, zero_division=0)

    print(f"\n=== Test Set Results ===")
    print(f"  PR-AUC:          {pr_auc:.4f}")
    print(f"  F-beta (β=2):    {fb2:.4f}  (at threshold=0.5)")

    # Save model
    model_path = MODEL_DIR / "fraud_xgb_calibrated.pkl"
    with open(model_path, "wb") as f:
        pickle.dump({"iso": iso, "xgb_base": xgb, "feature_names": list(X.columns)}, f)
    print(f"\nModel saved → {model_path}")

    # Save test predictions with UI feature columns
    df_full = pd.read_parquet(DATA_DIR / "train_engineered.parquet")
    test_idx = X_test.index
    ui_cols = [c for c in UI_FEATURES if c in df_full.columns]
    test_out = df_full.loc[test_idx, ["TransactionID", "isFraud"] + ui_cols].copy()
    test_out["y_prob"] = y_prob
    pred_path = DATA_DIR / "test_predictions.parquet"
    test_out.to_parquet(pred_path, index=False)
    print(f"Test predictions saved → {pred_path}  ({len(test_out):,} rows)")
    print("Model training complete.")


if __name__ == "__main__":
    main()
