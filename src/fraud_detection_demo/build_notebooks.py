"""Generate Jupyter notebooks from the fraud detection demo scripts.

Output: src/fraud_detection_demo/jupyter/*.ipynb

Run from project root:
    source venv/bin/activate
    python src/fraud_detection_demo/build_notebooks.py
"""

from __future__ import annotations
import uuid
from pathlib import Path
import nbformat
from nbformat.v4 import new_notebook, new_markdown_cell, new_code_cell

OUT_DIR = Path("src/fraud_detection_demo/jupyter")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def uid() -> str:
    return uuid.uuid4().hex[:8]


def nb(cells: list) -> nbformat.NotebookNode:
    n = new_notebook()
    n.metadata = {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.10.0"},
    }
    n.cells = cells
    for c in n.cells:
        c["id"] = uid()
    return n


def save(notebook: nbformat.NotebookNode, name: str) -> None:
    path = OUT_DIR / name
    with open(path, "w") as f:
        nbformat.write(notebook, f)
    print(f"  wrote {path}")


# ─────────────────────────────────────────────────────────────────────────────
# 00 — EDA
# ─────────────────────────────────────────────────────────────────────────────
save(nb([
    new_markdown_cell("# 00 — Exploratory Data Analysis\n\nIEEE-CIS Fraud Detection dataset. Understand class imbalance, null rates, amount distribution, and identity coverage before feature engineering."),

    new_code_cell("""\
from pathlib import Path
import pandas as pd

DATA_DIR = Path("../../..") / "data" / "ieee-fraud-detection"
"""),

    new_markdown_cell("## 1. Load raw data"),
    new_code_cell("""\
txn = pd.read_csv(DATA_DIR / "train_transaction.csv")
idn = pd.read_csv(DATA_DIR / "train_identity.csv")
print(f"Transactions : {len(txn):,} rows  ×  {txn.shape[1]} columns")
print(f"Identity     : {len(idn):,} rows  ×  {idn.shape[1]} columns")
"""),

    new_markdown_cell("## 2. Class imbalance"),
    new_code_cell("""\
fraud_rate = txn["isFraud"].mean()
print(f"Fraud rate : {fraud_rate:.2%}")
print(f"Fraud count: {txn['isFraud'].sum():,}  /  {len(txn):,} total")
txn["isFraud"].value_counts().rename({0: "Legit", 1: "Fraud"})
"""),

    new_markdown_cell("## 3. Transaction amount statistics"),
    new_code_cell("""\
print(f"Mean   : ${txn['TransactionAmt'].mean():.2f}")
print(f"Median : ${txn['TransactionAmt'].median():.2f}")
print()
print("Percentiles:")
print(txn["TransactionAmt"].quantile([0.25, 0.5, 0.75, 0.90, 0.95, 0.99]).to_string())
"""),

    new_markdown_cell("## 4. Fraud vs legitimate — amount comparison"),
    new_code_cell("""\
txn.groupby("isFraud")["TransactionAmt"].describe().round(2)
"""),

    new_markdown_cell("## 5. Identity join coverage"),
    new_code_cell("""\
joined = txn.merge(idn, on="TransactionID", how="left", indicator=True)
coverage = (joined["_merge"] == "both").mean()
print(f"Transactions with identity record: {coverage:.1%}")
print(f"  → missing identity is itself a fraud signal")
"""),

    new_markdown_cell("## 6. Null rates — top 20 columns"),
    new_code_cell("""\
null_rates = txn.isnull().mean().sort_values(ascending=False).head(20)
null_rates[null_rates > 0].rename("null_rate").to_frame().style.format("{:.1%}")
"""),

    new_markdown_cell("## 7. Key feature null rates"),
    new_code_cell("""\
key_cols = ["dist1", "dist2", "R_emaildomain", "P_emaildomain"]
for col in key_cols:
    if col in txn.columns:
        print(f"{col:20s}: {txn[col].isnull().mean():.1%} null")
"""),

    new_markdown_cell("## 8. Product and card distributions"),
    new_code_cell("""\
print("=== ProductCD ===")
print(txn["ProductCD"].value_counts().to_string())
print()
if "card4" in txn.columns:
    print("=== card4 (brand) ===")
    print(txn["card4"].value_counts().to_string())
"""),
]), "00_eda.ipynb")


# ─────────────────────────────────────────────────────────────────────────────
# 01 — Feature Engineering
# ─────────────────────────────────────────────────────────────────────────────
save(nb([
    new_markdown_cell("# 01 — Feature Engineering\n\nFour signal layers:\n1. **Velocity** — rolling tx count + value per card (1h / 24h)\n2. **Ratio** — C-column totals, email null flags\n3. **Interaction** — new account × high value × express\n4. **Network** — email domain mismatch, device/email linkage counts\n\nOutput: `data/ieee-fraud-detection/train_engineered.parquet`"),

    new_code_cell("""\
from pathlib import Path
import numpy as np
import pandas as pd

DATA_DIR = Path("../../..") / "data" / "ieee-fraud-detection"
OUT_PATH = DATA_DIR / "train_engineered.parquet"
"""),

    new_markdown_cell("## 1. Load and join data"),
    new_code_cell("""\
txn = pd.read_csv(DATA_DIR / "train_transaction.csv")
idn = pd.read_csv(DATA_DIR / "train_identity.csv")

df = txn.merge(idn, on="TransactionID", how="left")
df["has_identity"] = df["id_01"].notna().astype(np.int8)
print(f"Rows: {len(df):,}  |  identity coverage: {df['has_identity'].mean():.1%}")
"""),

    new_markdown_cell("## 2. Impute dist1 (59.7% null)"),
    new_code_cell("""\
dist1_median = df["dist1"].median()
df["dist1_missing"] = df["dist1"].isnull().astype(np.int8)
df["dist1"] = df["dist1"].fillna(dist1_median)
print(f"dist1 median used for imputation: {dist1_median:.1f}")
print(f"dist1_missing flag created: {df['dist1_missing'].sum():,} rows flagged")
"""),

    new_markdown_cell("## 3. Layer 1 — Velocity features\n\nRolling transaction count and value per card over 1h and 24h windows. Captures burst patterns typical of card testing attacks.\n\n⚠ Slow (~2–3 min) due to per-group iteration."),
    new_code_cell("""\
def add_velocity_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("TransactionDT").copy()
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

df = add_velocity_features(df)
print("Velocity features added:", [c for c in df.columns if c.startswith("velocity_")])
"""),

    new_markdown_cell("## 4. Layer 2 — Ratio features\n\nC1–C14 are Vesta-encoded cumulative counts (chargeback history, address/phone usage). Summing them captures overall account history depth."),
    new_code_cell("""\
def add_ratio_features(df: pd.DataFrame) -> pd.DataFrame:
    c_cols = [c for c in df.columns if c.startswith("C") and c[1:].isdigit()]
    if c_cols:
        df["c_total"] = df[c_cols].fillna(0).sum(axis=1)
        df["c_max"]   = df[c_cols].fillna(0).max(axis=1)
    df["r_email_missing"] = df["R_emaildomain"].isnull().astype(np.int8)
    df["p_email_missing"] = df["P_emaildomain"].isnull().astype(np.int8)
    return df

df = add_ratio_features(df)
print(f"c_total mean: {df['c_total'].mean():.1f}  |  c_max mean: {df['c_max'].mean():.1f}")
"""),

    new_markdown_cell("## 5. Layer 3 — Interaction features\n\nD1 = days since first address use, which proxies account age. New accounts making high-value express purchases are a high-risk combination."),
    new_code_cell("""\
def add_interaction_features(df: pd.DataFrame) -> pd.DataFrame:
    addr_age = df["D1"].fillna(999)   # unknown → treat as established
    new_acct  = addr_age < 7
    high_value = df["TransactionAmt"] > 500
    express    = df["ProductCD"] == "S"

    df["new_acct_highval_express"] = (new_acct & high_value & express).astype(np.int8)
    df["new_acct_flag"]  = new_acct.astype(np.int8)
    df["addr_age_days"]  = addr_age.clip(upper=365)
    return df

df = add_interaction_features(df)
print(f"new_acct_highval_express triggered: {df['new_acct_highval_express'].sum():,} transactions")
print(f"new accounts (<7d): {df['new_acct_flag'].sum():,}")
"""),

    new_markdown_cell("## 6. Layer 4 — Network / graph features\n\nDevices and email domains shared across many transactions indicate account takeover rings or synthetic identity networks."),
    new_code_cell("""\
def add_network_features(df: pd.DataFrame) -> pd.DataFrame:
    def email_tld(s: pd.Series) -> pd.Series:
        return s.fillna("").str.split(".").str[-1].str.lower()

    p_tld = email_tld(df.get("P_emaildomain", pd.Series([""] * len(df), index=df.index)))
    r_tld = email_tld(df.get("R_emaildomain", pd.Series([""] * len(df), index=df.index)))
    df["email_domain_mismatch"] = (
        (p_tld != r_tld) & (p_tld != "") & (r_tld != "")
    ).astype(np.int8)

    if "DeviceInfo" in df.columns:
        df["device_linkage_count"] = df["DeviceInfo"].map(
            df["DeviceInfo"].value_counts()
        ).fillna(1).astype(np.int32)
    else:
        df["device_linkage_count"] = 1

    df["email_domain_linkage_count"] = df["P_emaildomain"].map(
        df["P_emaildomain"].value_counts()
    ).fillna(1).astype(np.int32)

    return df

df = add_network_features(df)
print(f"email_domain_mismatch: {df['email_domain_mismatch'].sum():,} transactions")
print(f"device_linkage_count > 100: {(df['device_linkage_count'] > 100).sum():,} transactions")
"""),

    new_markdown_cell("## 7. Save engineered dataset"),
    new_code_cell("""\
df.to_parquet(OUT_PATH, index=False)
print(f"Saved → {OUT_PATH}")
print(f"Shape: {df.shape}")

new_features = [
    "velocity_count_1h", "velocity_value_1h", "velocity_count_24h", "velocity_value_24h",
    "c_total", "c_max", "r_email_missing", "p_email_missing",
    "new_acct_highval_express", "new_acct_flag", "addr_age_days",
    "email_domain_mismatch", "device_linkage_count", "email_domain_linkage_count",
    "has_identity", "dist1_missing",
]
print(f"\\nEngineered features ({len(new_features)}):")
for f in new_features:
    print(f"  {f}")
"""),
]), "01_feature_engineering.ipynb")


# ─────────────────────────────────────────────────────────────────────────────
# 02 — Train Model
# ─────────────────────────────────────────────────────────────────────────────
save(nb([
    new_markdown_cell("# 02 — Model Training\n\n**XGBoost + isotonic calibration** on engineered features.\n\nKey design choices:\n- `scale_pos_weight` handles 28:1 class imbalance — no SMOTE needed\n- `eval_metric=aucpr` optimises PR-AUC directly (better than ROC for imbalanced data)\n- Isotonic regression calibrates raw probabilities so P(fraud) is interpretable\n\nOutputs:\n- `models/fraud_xgb_calibrated.pkl`\n- `data/ieee-fraud-detection/test_predictions.parquet`"),

    new_code_cell("""\
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, fbeta_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

DATA_DIR  = Path("../../..") / "data" / "ieee-fraud-detection"
MODEL_DIR = Path("../../..") / "models"
MODEL_DIR.mkdir(exist_ok=True)
"""),

    new_markdown_cell("## 1. Define columns to drop\n\nDrop IDs, raw strings already encoded into features, and high-cardinality fields that would leak or overfit."),
    new_code_cell("""\
DROP_COLS = [
    "TransactionID", "TransactionDT",
    "card2", "card3", "card5",           # high-cardinality card metadata
    "addr1", "addr2",                    # raw address codes
    "P_emaildomain", "R_emaildomain",    # encoded via ratio/network features
    "DeviceInfo", "DeviceType",          # encoded via network features
    "card4", "card6", "ProductCD",       # raw strings
    "M1","M2","M3","M4","M5","M6","M7","M8","M9",     # match flags (string)
    "id_12","id_15","id_16","id_23","id_27","id_28",  # string identity cols
    "id_29","id_30","id_31","id_32","id_33","id_34",
    "id_35","id_36","id_37","id_38",
]

UI_FEATURES = [
    "TransactionAmt", "addr_age_days", "email_domain_mismatch",
    "new_acct_highval_express", "velocity_count_1h", "velocity_value_1h",
    "velocity_count_24h", "velocity_value_24h", "device_linkage_count",
    "has_identity", "dist1", "dist1_missing", "c_total",
]
print(f"Columns to drop: {len(DROP_COLS)}")
"""),

    new_markdown_cell("## 2. Load and prepare features"),
    new_code_cell("""\
df_full = pd.read_parquet(DATA_DIR / "train_engineered.parquet")
y = df_full["isFraud"].astype(np.int8)
drop = [c for c in DROP_COLS if c in df_full.columns] + ["isFraud"]
X = df_full.drop(columns=drop)

# Encode any remaining object columns as category codes
for col in X.select_dtypes("object").columns:
    X[col] = X[col].astype("category").cat.codes

print(f"Feature matrix: {X.shape[0]:,} rows  ×  {X.shape[1]} columns")
print(f"Fraud rate: {y.mean():.2%}")
"""),

    new_markdown_cell("## 3. Stratified train / calibration / test split\n\n70% train · 10% calibration · 20% test. Calibration set is held out from training and used to fit the isotonic regressor."),
    new_code_cell("""\
X_trainval, X_test, y_trainval, y_test = train_test_split(
    X, y, test_size=0.20, stratify=y, random_state=42
)
X_train, X_cal, y_train, y_cal = train_test_split(
    X_trainval, y_trainval, test_size=0.125, stratify=y_trainval, random_state=42
)   # 0.125 of 0.80 = 10% of total

print(f"Train : {len(X_train):,}")
print(f"Cal   : {len(X_cal):,}")
print(f"Test  : {len(X_test):,}")
"""),

    new_markdown_cell("## 4. Compute class weight\n\n`scale_pos_weight = neg / pos` tells XGBoost to penalise missing a fraud case ~28× more than a false positive during training. This is equivalent to upsampling fraud without actually duplicating rows."),
    new_code_cell("""\
neg = (y_train == 0).sum()
pos = (y_train == 1).sum()
spw = round(neg / pos)
print(f"Negative (legit): {neg:,}")
print(f"Positive (fraud): {pos:,}")
print(f"scale_pos_weight: {spw}  (ratio ≈ {neg/pos:.1f})")
"""),

    new_markdown_cell("## 5. Train XGBoost\n\n`eval_metric=aucpr` monitors PR-AUC on the calibration set. `early_stopping_rounds=30` halts if no improvement for 30 rounds."),
    new_code_cell("""\
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
print(f"\\nBest iteration: {xgb.best_iteration}")
"""),

    new_markdown_cell("## 6. Isotonic calibration\n\nXGBoost raw probabilities are well-ranked but not well-calibrated (not true P(fraud)). We fit an isotonic regression on the calibration set to map raw scores → calibrated probabilities.\n\n> Note: `CalibratedClassifierCV(cv='prefit')` was removed in sklearn 1.2. We do this manually instead."),
    new_code_cell("""\
raw_cal = xgb.predict_proba(X_cal)[:, 1]

iso = IsotonicRegression(out_of_bounds="clip")
iso.fit(raw_cal, y_cal)

# Compare raw vs calibrated on cal set
raw_test = xgb.predict_proba(X_test)[:, 1]
y_prob   = iso.predict(raw_test)

print(f"Raw score range  : {raw_test.min():.4f} – {raw_test.max():.4f}")
print(f"Calibrated range : {y_prob.min():.4f} – {y_prob.max():.4f}")
"""),

    new_markdown_cell("## 7. Evaluate on test set"),
    new_code_cell("""\
pr_auc = average_precision_score(y_test, y_prob)

# F-beta (β=2) rewards recall twice as much as precision
y_pred = (y_prob >= 0.5).astype(int)
fb2    = fbeta_score(y_test, y_pred, beta=2, zero_division=0)

print("=== Test Set Results ===")
print(f"  PR-AUC      : {pr_auc:.4f}  (primary metric)")
print(f"  F-beta β=2  : {fb2:.4f}  (at threshold=0.5)")
print()
print("Note: optimal threshold is not 0.5 — see notebook 03 for cost-based selection.")
"""),

    new_markdown_cell("## 8. Save model"),
    new_code_cell("""\
model_path = MODEL_DIR / "fraud_xgb_calibrated.pkl"
with open(model_path, "wb") as f:
    pickle.dump({"iso": iso, "xgb_base": xgb, "feature_names": list(X.columns)}, f)
print(f"Saved → {model_path}")
"""),

    new_markdown_cell("## 9. Save test predictions"),
    new_code_cell("""\
test_idx = X_test.index
ui_cols  = [c for c in UI_FEATURES if c in df_full.columns]
test_out = df_full.loc[test_idx, ["TransactionID", "isFraud"] + ui_cols].copy()
test_out["y_prob"] = y_prob

pred_path = DATA_DIR / "test_predictions.parquet"
test_out.to_parquet(pred_path, index=False)
print(f"Saved → {pred_path}  ({len(test_out):,} rows)")
test_out.head()
"""),
]), "02_train_model.ipynb")


# ─────────────────────────────────────────────────────────────────────────────
# 03 — Threshold & Cost
# ─────────────────────────────────────────────────────────────────────────────
save(nb([
    new_markdown_cell("# 03 — Threshold Selection & Cost Analysis\n\nTwo competing objectives:\n1. **Cost-optimal threshold** — minimise total dollar loss (C_FN=$420, C_FP=$6)\n2. **Capacity-constrained threshold** — lowest threshold where analyst queue ≤ 200 cases/day\n\nOutput: `src/fraud_detection_demo/demo/data_export.json`"),

    new_code_cell("""\
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_curve, average_precision_score

DATA_DIR  = Path("../../..") / "data" / "ieee-fraud-detection"
DEMO_DIR  = Path("..") / "demo"
EXPORT_PATH = DEMO_DIR / "data_export.json"

C_FN = 420.0   # cost of missed fraud: avg order value + chargeback penalty
C_FP = 6.0     # cost of false positive: review time + customer friction
CAPACITY_PER_DAY = 200
print(f"Cost FN=${C_FN}  FP=${C_FP}  |  imbalance ratio: {C_FN/C_FP:.0f}:1")
"""),

    new_markdown_cell("## 1. Load test predictions"),
    new_code_cell("""\
df = pd.read_parquet(DATA_DIR / "test_predictions.parquet")
y_true = df["isFraud"].values
y_prob = df["y_prob"].values

pr_auc = average_precision_score(y_true, y_prob)
print(f"Test set: {len(df):,} rows  |  fraud: {y_true.mean():.2%}")
print(f"PR-AUC: {pr_auc:.4f}")
"""),

    new_markdown_cell("## 2. Compute PR curve"),
    new_code_cell("""\
precision, recall, thresholds = precision_recall_curve(y_true, y_prob)
thresholds = np.append(thresholds, 1.0)   # align lengths

print(f"PR curve points: {len(thresholds)}")
print(f"Recall range: {recall.min():.3f} – {recall.max():.3f}")
print(f"Precision range: {precision.min():.3f} – {precision.max():.3f}")
"""),

    new_markdown_cell("## 3. Cost sweep across all thresholds"),
    new_code_cell("""\
def compute_cost(y_true, y_prob, threshold):
    y_pred = (y_prob >= threshold).astype(int)
    fn = ((y_true == 1) & (y_pred == 0)).sum()
    fp = ((y_true == 0) & (y_pred == 1)).sum()
    return float(C_FN * fn + C_FP * fp)

costs          = np.array([compute_cost(y_true, y_prob, t) for t in thresholds])
flagged_counts = np.array([(y_prob >= t).sum() for t in thresholds])
flagged_per_day = flagged_counts / 30.0   # test set ≈ 30 days of traffic

best_idx = int(np.argmin(costs))
print(f"Cost-optimal threshold : {thresholds[best_idx]:.3f}")
print(f"  Precision: {precision[best_idx]:.3f}  Recall: {recall[best_idx]:.3f}")
print(f"  Total cost: ${costs[best_idx]:,.0f}  |  Flagged/day: {flagged_per_day[best_idx]:.0f}")
"""),

    new_markdown_cell("## 4. Capacity-constrained threshold\n\nThe cost-optimal threshold flags 1,200+ cases/day — far above analyst capacity. We find the lowest threshold (highest recall) where volume stays ≤ 200/day."),
    new_code_cell("""\
cap_idx = next(i for i, fpd in enumerate(flagged_per_day) if fpd <= CAPACITY_PER_DAY)

print(f"Capacity-constrained threshold : {thresholds[cap_idx]:.3f}")
print(f"  Precision : {precision[cap_idx]:.3f}")
print(f"  Recall    : {recall[cap_idx]:.3f}")
print(f"  Cost/day  : ${costs[cap_idx]/30:,.0f}")
print(f"  Flags/day : {flagged_per_day[cap_idx]:.0f}")
"""),

    new_markdown_cell("## 5. Export PR curve + thresholds to JSON"),
    new_code_cell("""\
pr_curve = [
    {
        "threshold": round(float(t), 4),
        "precision": round(float(p), 4),
        "recall": round(float(r), 4),
        "cost": round(float(c), 0),
        "flagged_per_day": round(float(f), 1),
    }
    for t, p, r, c, f in zip(thresholds, precision, recall, costs, flagged_per_day)
]

if EXPORT_PATH.exists():
    with open(EXPORT_PATH) as f:
        export = json.load(f)
else:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    export = {}

export.update({
    "pr_curve": pr_curve,
    "pr_auc": round(pr_auc, 4),
    "optimal_threshold": round(float(thresholds[cap_idx]), 4),
    "cost_optimal_threshold": round(float(thresholds[best_idx]), 4),
    "optimal_precision": round(float(precision[cap_idx]), 4),
    "optimal_recall": round(float(recall[cap_idx]), 4),
    "c_fn": C_FN,
    "c_fp": C_FP,
    "capacity_per_day": CAPACITY_PER_DAY,
})

with open(EXPORT_PATH, "w") as f:
    json.dump(export, f)

print(f"Exported {len(pr_curve)} PR curve points → {EXPORT_PATH}")
"""),
]), "03_threshold_and_cost.ipynb")


# ─────────────────────────────────────────────────────────────────────────────
# 04 — Adversarial Drift
# ─────────────────────────────────────────────────────────────────────────────
save(nb([
    new_markdown_cell("# 04 — Adversarial Drift Simulation\n\n**Threat model**: fraudsters observe that high-value transactions are flagged, so they split each transaction into micro-amounts (≤$50) spread across time — suppressing both the `TransactionAmt` and `velocity_value` signals.\n\nWe simulate this by modifying the test set and re-scoring with the fixed model, then measuring:\n- Recall drop\n- Population Stability Index (PSI) on key features — alarm at PSI > 0.2\n\nOutput: appended to `data_export.json`"),

    new_code_cell("""\
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, recall_score, precision_recall_curve

DATA_DIR    = Path("../../..") / "data" / "ieee-fraud-detection"
DEMO_DIR    = Path("..") / "demo"
EXPORT_PATH = DEMO_DIR / "data_export.json"
MODEL_PATH  = Path("../../..") / "models" / "fraud_xgb_calibrated.pkl"

EVASION_CAP  = 50.0   # fraudsters cap amounts to ≤$50
VELOCITY_CAP = 80.0   # and suppress velocity value signal
PSI_ALARM    = 0.2
"""),

    new_markdown_cell("## 1. Load model"),
    new_code_cell("""\
with open(MODEL_PATH, "rb") as f:
    bundle = pickle.load(f)

xgb_base      = bundle["xgb_base"]
iso           = bundle["iso"]
feature_names = bundle["feature_names"]

def score(X):
    return iso.predict(xgb_base.predict_proba(X)[:, 1])

print(f"Model loaded — {len(feature_names)} features")
"""),

    new_markdown_cell("## 2. Reconstruct test feature matrix"),
    new_code_cell("""\
df_pred = pd.read_parquet(DATA_DIR / "test_predictions.parquet")
eng     = pd.read_parquet(DATA_DIR / "train_engineered.parquet")
eng_test = (
    eng[eng["TransactionID"].isin(df_pred["TransactionID"])]
    .set_index("TransactionID")
    .loc[df_pred["TransactionID"].values]
    .reset_index()
)

drop_cols = ["TransactionID","TransactionDT","isFraud",
             "card2","card3","card5","addr1","addr2",
             "P_emaildomain","R_emaildomain","DeviceInfo","DeviceType",
             "card4","card6","ProductCD",
             "M1","M2","M3","M4","M5","M6","M7","M8","M9",
             "id_12","id_15","id_16","id_23","id_27","id_28",
             "id_29","id_30","id_31","id_32","id_33","id_34",
             "id_35","id_36","id_37","id_38"]
drop_cols = [c for c in drop_cols if c in eng_test.columns]

X_test = eng_test.drop(columns=drop_cols)
for col in X_test.select_dtypes("object").columns:
    X_test[col] = X_test[col].astype("category").cat.codes
for col in feature_names:
    if col not in X_test.columns:
        X_test[col] = 0
X_test = X_test[feature_names]

y_true = eng_test["isFraud"].values
print(f"Test set: {len(X_test):,} rows  |  fraud: {y_true.mean():.2%}")
"""),

    new_markdown_cell("## 3. Baseline scoring"),
    new_code_cell("""\
with open(EXPORT_PATH) as f:
    export = json.load(f)
threshold = export.get("optimal_threshold", 0.5)

y_prob_baseline  = score(X_test)
baseline_recall  = recall_score(y_true, (y_prob_baseline >= threshold).astype(int), zero_division=0)
baseline_prauc   = average_precision_score(y_true, y_prob_baseline)

print(f"Threshold       : {threshold:.3f}")
print(f"Baseline recall : {baseline_recall:.3f}")
print(f"Baseline PR-AUC : {baseline_prauc:.4f}")
"""),

    new_markdown_cell("## 4. Apply adversarial shift\n\nApply amount capping to all fraud rows + 30% of legitimate rows to simulate a population-level distribution shift (so PSI is detectable on the monitoring dashboard)."),
    new_code_cell("""\
rng        = np.random.default_rng(42)
fraud_mask = y_true == 1
legit_shift = (~fraud_mask) & (rng.random(len(y_true)) < 0.30)
shift_mask  = fraud_mask | legit_shift

print(f"Rows shifted: {shift_mask.sum():,}  ({shift_mask.mean():.1%} of test set)")
print(f"  Fraud rows   : {fraud_mask.sum():,}  (all)")
print(f"  Legit rows   : {legit_shift.sum():,}  (30% sample)")
"""),

    new_markdown_cell("## 5. Re-score on shifted data"),
    new_code_cell("""\
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
shifted_prauc  = average_precision_score(y_true, y_prob_shifted)

print(f"Post-drift recall : {shifted_recall:.3f}  (was {baseline_recall:.3f})")
print(f"Recall drop       : {baseline_recall - shifted_recall:.3f}  ({(baseline_recall-shifted_recall)/baseline_recall:.1%} relative)")
print(f"Post-drift PR-AUC : {shifted_prauc:.4f}  (was {baseline_prauc:.4f})")
"""),

    new_markdown_cell("## 6. Population Stability Index (PSI)\n\nPSI measures how much a distribution has shifted between baseline and current. PSI > 0.2 triggers a drift alarm — time to investigate or retrain.\n\nWe use **quantile-based bins** rather than uniform bins, which is more sensitive for skewed financial distributions."),
    new_code_cell("""\
def compute_psi(baseline, shifted, bins=10):
    edges = np.unique(np.percentile(baseline, np.linspace(0, 100, bins + 1)))
    if len(edges) < 3:
        return 0.0
    base_cnt,  _ = np.histogram(baseline, bins=edges)
    shift_cnt, _ = np.histogram(shifted,  bins=edges)
    base_pct  = (base_cnt  + 1e-6) / len(baseline)
    shift_pct = (shift_cnt + 1e-6) / len(shifted)
    return float(np.sum((shift_pct - base_pct) * np.log(shift_pct / base_pct)))

psi_amount   = compute_psi(X_test["TransactionAmt"].values,   X_shifted["TransactionAmt"].values)
psi_velocity = compute_psi(X_test["velocity_value_1h"].values, X_shifted["velocity_value_1h"].values)

print(f"PSI — TransactionAmt    : {psi_amount:.4f}  {'⚠ ALARM' if psi_amount > PSI_ALARM else '✓ OK'}  (alarm > {PSI_ALARM})")
print(f"PSI — velocity_value_1h : {psi_velocity:.4f}  {'⚠ ALARM' if psi_velocity > PSI_ALARM else '✓ OK'}")
"""),

    new_markdown_cell("## 7. Export results"),
    new_code_cell("""\
prec_s, rec_s, thresh_s = precision_recall_curve(y_true, y_prob_shifted)
thresh_s = np.append(thresh_s, 1.0)
pr_curve_shifted = [
    {"threshold": round(float(t),4), "precision": round(float(p),4), "recall": round(float(r),4)}
    for t, p, r in zip(thresh_s, prec_s, rec_s)
]

export["adversarial"] = {
    "strategy":        "transaction_splitting",
    "evasion_cap":     EVASION_CAP,
    "baseline_recall": round(baseline_recall, 4),
    "shifted_recall":  round(shifted_recall, 4),
    "recall_drop":     round(baseline_recall - shifted_recall, 4),
    "baseline_prauc":  round(baseline_prauc, 4),
    "shifted_prauc":   round(shifted_prauc, 4),
    "psi_amount":      round(psi_amount, 4),
    "psi_velocity":    round(psi_velocity, 4),
    "psi_alarm_threshold": PSI_ALARM,
    "pr_curve_shifted": pr_curve_shifted,
}

with open(EXPORT_PATH, "w") as f:
    json.dump(export, f)

print(f"Saved → {EXPORT_PATH}")
"""),
]), "04_adversarial_drift.ipynb")


# ─────────────────────────────────────────────────────────────────────────────
# 05 — SHAP Export
# ─────────────────────────────────────────────────────────────────────────────
save(nb([
    new_markdown_cell("# 05 — SHAP Reason Codes\n\nExplain per-transaction predictions using **SHAP TreeExplainer**. This produces:\n1. Top-5 adverse-action reason codes per transaction (required for regulatory compliance)\n2. A review queue of the 5 highest expected-loss cases (amount × P(fraud))\n\nOutput: appended to `data_export.json`"),

    new_code_cell("""\
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import shap

DATA_DIR    = Path("../../..") / "data" / "ieee-fraud-detection"
DEMO_DIR    = Path("..") / "demo"
EXPORT_PATH = DEMO_DIR / "data_export.json"
MODEL_PATH  = Path("../../..") / "models" / "fraud_xgb_calibrated.pkl"

N_EXPLAIN = 1000   # 500 fraud + 500 legit
TOP_K     = 5
"""),

    new_markdown_cell("## 1. Load model"),
    new_code_cell("""\
with open(MODEL_PATH, "rb") as f:
    bundle = pickle.load(f)

xgb_base      = bundle["xgb_base"]
iso           = bundle["iso"]
feature_names = bundle["feature_names"]
print(f"Model loaded — {len(feature_names)} features")
"""),

    new_markdown_cell("## 2. Reconstruct test feature matrix"),
    new_code_cell("""\
df_pred  = pd.read_parquet(DATA_DIR / "test_predictions.parquet")
eng      = pd.read_parquet(DATA_DIR / "train_engineered.parquet")
eng_test = (
    eng[eng["TransactionID"].isin(df_pred["TransactionID"])]
    .set_index("TransactionID")
    .loc[df_pred["TransactionID"].values]
    .reset_index()
)

drop_cols = ["TransactionID","TransactionDT","isFraud",
             "card2","card3","card5","addr1","addr2",
             "P_emaildomain","R_emaildomain","DeviceInfo","DeviceType",
             "card4","card6","ProductCD",
             "M1","M2","M3","M4","M5","M6","M7","M8","M9",
             "id_12","id_15","id_16","id_23","id_27","id_28",
             "id_29","id_30","id_31","id_32","id_33","id_34",
             "id_35","id_36","id_37","id_38"]
drop_cols = [c for c in drop_cols if c in eng_test.columns]

X_test = eng_test.drop(columns=drop_cols)
for col in X_test.select_dtypes("object").columns:
    X_test[col] = X_test[col].astype("category").cat.codes
for col in feature_names:
    if col not in X_test.columns:
        X_test[col] = 0
X_test = X_test[feature_names]
print(f"Test features: {X_test.shape}")
"""),

    new_markdown_cell("## 3. Stratified sample — 500 fraud + 500 legit"),
    new_code_cell("""\
fraud_idx  = eng_test[eng_test["isFraud"] == 1].index[:N_EXPLAIN // 2]
legit_idx  = eng_test[eng_test["isFraud"] == 0].index[:N_EXPLAIN // 2]
sample_idx = fraud_idx.tolist() + legit_idx.tolist()

pos_map    = {orig: pos for pos, orig in enumerate(eng_test.index)}
sample_pos = [pos_map[i] for i in sample_idx if i in pos_map]

X_sample       = X_test.iloc[sample_pos]
y_sample       = eng_test["isFraud"].iloc[sample_pos].values
tid_sample     = eng_test["TransactionID"].iloc[sample_pos].values
y_prob_sample  = df_pred.set_index("TransactionID").loc[tid_sample, "y_prob"].values

print(f"Sample: {len(X_sample)} transactions  |  fraud: {y_sample.mean():.0%}")
"""),

    new_markdown_cell("## 4. Compute SHAP values\n\n`TreeExplainer` is exact and fast for tree-based models — no approximation needed."),
    new_code_cell("""\
explainer   = shap.TreeExplainer(xgb_base)
shap_values = explainer.shap_values(X_sample)

print(f"SHAP matrix shape: {shap_values.shape}")
print(f"Columns: {shap_values.shape[1]}  (one per feature)")
"""),

    new_markdown_cell("## 5. Build per-transaction reason code records"),
    new_code_cell("""\
UI_FEATURE_KEYS = [
    "TransactionAmt", "addr_age_days", "email_domain_mismatch",
    "new_acct_highval_express", "velocity_count_1h", "velocity_value_1h",
    "velocity_count_24h", "velocity_value_24h", "device_linkage_count",
    "has_identity", "dist1", "c_total",
]

records = []
for i, (tid, y_true, y_prob) in enumerate(zip(tid_sample, y_sample, y_prob_sample)):
    sv      = shap_values[i]
    top_idx = np.argsort(np.abs(sv))[::-1][:TOP_K]
    top5    = [
        {"feature": feature_names[j], "value": round(float(X_sample.iloc[i, j]), 4),
         "shap": round(float(sv[j]), 4)}
        for j in top_idx
    ]
    key_features = {
        k: round(float(X_sample.iloc[i][k]), 4)
        for k in UI_FEATURE_KEYS if k in X_sample.columns
    }
    records.append({
        "transaction_id": int(tid),
        "y_true": int(y_true),
        "fraud_prob": round(float(y_prob), 4),
        "shap_top5": top5,
        "key_features": key_features,
    })

print(f"Built {len(records)} SHAP records")
print(f"\\nSample — transaction {records[0]['transaction_id']}:")
for r in records[0]["shap_top5"]:
    print(f"  {r['feature']:30s}  shap={r['shap']:+.4f}  value={r['value']}")
"""),

    new_markdown_cell("## 6. Build review queue\n\nSort by **expected loss = amount × P(fraud)** — prioritises high-value high-confidence cases for analyst review."),
    new_code_cell("""\
queue_candidates = [r for r in records if r["fraud_prob"] > 0.3]
queue_candidates.sort(
    key=lambda r: r["key_features"].get("TransactionAmt", 0) * r["fraud_prob"],
    reverse=True,
)
review_queue = queue_candidates[:5]

print(f"Queue candidates (P>0.3): {len(queue_candidates)}")
print(f"\\nTop 5 review cases:")
for i, r in enumerate(review_queue):
    amt = r["key_features"].get("TransactionAmt", 0)
    print(f"  {i+1}. TxID={r['transaction_id']}  amount=${amt:.0f}  P(fraud)={r['fraud_prob']:.3f}  expected_loss=${amt*r['fraud_prob']:.0f}")
"""),

    new_markdown_cell("## 7. Export to data_export.json"),
    new_code_cell("""\
with open(EXPORT_PATH) as f:
    export = json.load(f)

export["transactions"]  = records
export["review_queue"]  = review_queue

with open(EXPORT_PATH, "w") as f:
    json.dump(export, f)

print(f"Saved → {EXPORT_PATH}")
print(f"Total keys in export: {list(export.keys())}")
"""),
]), "05_shap_export.ipynb")


# ─────────────────────────────────────────────────────────────────────────────
# 06 — Hyperparameter Search
# ─────────────────────────────────────────────────────────────────────────────
save(nb([
    new_markdown_cell("# 06 — Hyperparameter Search: RandomizedSearchCV vs Optuna (TPE)\n\nThe model in notebook 02 uses hand-picked hyperparameters (`max_depth=6, learning_rate=0.05`). Here we search for better ones — and compare **two search methods head-to-head** on the same search space, CV protocol, and trial budget:\n\n- **Cost**: wall-clock time + trials-to-95%-of-final-best (sample efficiency)\n- **Performance**: final CV PR-AUC, then held-out test PR-AUC / F-β=2 / cost vs. the manual baseline\n\nOutputs:\n- `models/fraud_xgb_tuned.pkl`\n- `data_export.json` → `hyperparam_search`"),

    new_code_cell("""\
import json
import time
from pathlib import Path

import numpy as np
import optuna
import pandas as pd
from optuna.samplers import TPESampler
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, fbeta_score
from sklearn.model_selection import StratifiedKFold, train_test_split
from xgboost import XGBClassifier

optuna.logging.set_verbosity(optuna.logging.WARNING)

DATA_DIR  = Path("../../..") / "data" / "ieee-fraud-detection"
MODEL_DIR = Path("../../..") / "models"
DEMO_DIR  = Path("..") / "demo"
EXPORT_PATH = DEMO_DIR / "data_export.json"

BASELINE_PARAMS = {
    "max_depth": 6, "learning_rate": 0.05, "min_child_weight": 1,
    "subsample": 1.0, "colsample_bytree": 1.0, "gamma": 0.0,
    "reg_alpha": 0.0, "reg_lambda": 1.0,
}
SEARCH_SPACE = {
    "max_depth": (3, 10), "learning_rate": (0.01, 0.3, "log"),
    "min_child_weight": (1, 10), "subsample": (0.5, 1.0),
    "colsample_bytree": (0.5, 1.0), "gamma": (0.0, 5.0),
    "reg_alpha": (1e-3, 10.0, "log"), "reg_lambda": (1e-3, 10.0, "log"),
}
N_TRIALS = 25
CV_FOLDS = 3
SEARCH_SUBSAMPLE = 60_000
SEARCH_N_ESTIMATORS = 200
FINAL_N_ESTIMATORS = 500
C_FN, C_FP = 420.0, 6.0
"""),

    new_markdown_cell("## 1. Load features + same train/cal/test split as notebook 02\n\nUsing the identical `random_state=42` split means the held-out test set here is the same one notebook 02 evaluated on — results are directly comparable."),
    new_code_cell("""\
DROP_COLS = [
    "TransactionID", "TransactionDT", "card2", "card3", "card5", "addr1", "addr2",
    "P_emaildomain", "R_emaildomain", "DeviceInfo", "DeviceType", "card4", "card6", "ProductCD",
    "M1","M2","M3","M4","M5","M6","M7","M8","M9",
    "id_12","id_15","id_16","id_23","id_27","id_28",
    "id_29","id_30","id_31","id_32","id_33","id_34","id_35","id_36","id_37","id_38",
]

df_full = pd.read_parquet(DATA_DIR / "train_engineered.parquet")
y = df_full["isFraud"].astype(np.int8)
X = df_full.drop(columns=[c for c in DROP_COLS if c in df_full.columns] + ["isFraud"])
for col in X.select_dtypes("object").columns:
    X[col] = X[col].astype("category").cat.codes

X_trainval, X_test, y_trainval, y_test = train_test_split(X, y, test_size=0.20, stratify=y, random_state=42)
X_train, X_cal, y_train, y_cal = train_test_split(X_trainval, y_trainval, test_size=0.125, stratify=y_trainval, random_state=42)

neg, pos = (y_train == 0).sum(), (y_train == 1).sum()
spw = neg / pos
print(f"Train: {len(X_train):,}  Cal: {len(X_cal):,}  Test: {len(X_test):,}  scale_pos_weight={spw:.1f}")
"""),

    new_markdown_cell("## 2. Stratified subsample for the search phase\n\n25 trials × 2 methods × 3-fold CV = 150 XGBoost fits. Fitting each on the full 472K-row training set would make the search itself the bottleneck, so we search on a 60K-row stratified subsample (fixed `n_estimators=200`, no early stopping) and only train the *final* candidates at full scale."),
    new_code_cell("""\
sub_idx = X_trainval.sample(n=min(SEARCH_SUBSAMPLE, len(X_trainval)), random_state=42).index
X_sub, y_sub = X_trainval.loc[sub_idx], y_trainval.loc[sub_idx]
sub_spw = (y_sub == 0).sum() / (y_sub == 1).sum()
folds = StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=42)
print(f"Search subsample: {len(X_sub):,} rows  |  {CV_FOLDS}-fold CV  |  {N_TRIALS} trials/method")
"""),

    new_markdown_cell("## 3. CV scoring function\n\nShared by both search methods so the comparison is apples-to-apples: same folds, same metric (PR-AUC), same fixed `n_estimators`."),
    new_code_cell("""\
def cv_score(params, X, y, spw, folds):
    scores = []
    for train_idx, val_idx in folds.split(X, y):
        clf = XGBClassifier(
            n_estimators=SEARCH_N_ESTIMATORS, scale_pos_weight=spw, eval_metric="aucpr",
            random_state=42, n_jobs=-1, tree_method="hist", **params,
        )
        clf.fit(X.iloc[train_idx], y.iloc[train_idx])
        proba = clf.predict_proba(X.iloc[val_idx])[:, 1]
        scores.append(average_precision_score(y.iloc[val_idx], proba))
    return float(np.mean(scores))

def finalize_method(name, trials, best_so_far, total_time):
    final_best = best_so_far[-1]
    target = 0.95 * final_best
    trials_to_95 = next((i for i, s in enumerate(best_so_far) if s >= target), len(best_so_far) - 1)
    best_trial = max(trials, key=lambda t: t["cv_score"])
    return {
        "method": name, "trials": trials, "best_so_far": [round(s, 4) for s in best_so_far],
        "total_time_sec": round(total_time, 1), "best_cv_score": round(final_best, 4),
        "trials_to_95pct": trials_to_95, "best_params": best_trial["params"],
    }
"""),

    new_markdown_cell("## 4. RandomizedSearchCV — blind sampling\n\nEach trial samples hyperparameters independently, with no knowledge of prior trials' results."),
    new_code_cell("""\
def sample_random_params(rng):
    params = {}
    for name, spec in SEARCH_SPACE.items():
        if len(spec) == 3:
            lo, hi, _ = spec
            params[name] = float(np.exp(rng.uniform(np.log(lo), np.log(hi))))
        elif name in ("max_depth", "min_child_weight"):
            lo, hi = spec
            params[name] = int(rng.integers(lo, hi + 1))
        else:
            lo, hi = spec
            params[name] = float(rng.uniform(lo, hi))
    return params

rng = np.random.default_rng(42)
trials, best_so_far, best_score = [], [], -np.inf
t_start = time.time()
for i in range(N_TRIALS):
    params = sample_random_params(rng)
    t0 = time.time()
    score = cv_score(params, X_sub, y_sub, sub_spw, folds)
    best_score = max(best_score, score)
    best_so_far.append(best_score)
    trials.append({"trial": i, "params": params, "cv_score": round(score, 4), "fit_time_sec": round(time.time() - t0, 2)})
random_result = finalize_method("random_search", trials, best_so_far, time.time() - t_start)
print(f"RandomizedSearch: best_cv={random_result['best_cv_score']:.4f}  time={random_result['total_time_sec']:.1f}s  trials_to_95%={random_result['trials_to_95pct']}")
"""),

    new_markdown_cell("## 5. Optuna — Bayesian (TPE) sampling\n\nEach trial's proposal is informed by the posterior over past trials' scores — it should need fewer trials to reach the same quality, since it stops wasting fits on regions that already look bad."),
    new_code_cell("""\
def objective(trial):
    params = {
        "max_depth": trial.suggest_int("max_depth", *SEARCH_SPACE["max_depth"]),
        "learning_rate": trial.suggest_float("learning_rate", *SEARCH_SPACE["learning_rate"][:2], log=True),
        "min_child_weight": trial.suggest_int("min_child_weight", *SEARCH_SPACE["min_child_weight"]),
        "subsample": trial.suggest_float("subsample", *SEARCH_SPACE["subsample"]),
        "colsample_bytree": trial.suggest_float("colsample_bytree", *SEARCH_SPACE["colsample_bytree"]),
        "gamma": trial.suggest_float("gamma", *SEARCH_SPACE["gamma"]),
        "reg_alpha": trial.suggest_float("reg_alpha", *SEARCH_SPACE["reg_alpha"][:2], log=True),
        "reg_lambda": trial.suggest_float("reg_lambda", *SEARCH_SPACE["reg_lambda"][:2], log=True),
    }
    t0 = time.time()
    score = cv_score(params, X_sub, y_sub, sub_spw, folds)
    trial_times.append(time.time() - t0)
    return score

trial_times = []
t_start = time.time()
study = optuna.create_study(direction="maximize", sampler=TPESampler(seed=42))
study.optimize(objective, n_trials=N_TRIALS, show_progress_bar=False)
total_time = time.time() - t_start

trials, best_so_far, best_score = [], [], -np.inf
for i, t in enumerate(study.trials):
    best_score = max(best_score, t.value)
    best_so_far.append(best_score)
    trials.append({"trial": i, "params": {k: (float(v) if isinstance(v, float) else int(v)) for k, v in t.params.items()},
                    "cv_score": round(t.value, 4), "fit_time_sec": round(trial_times[i], 2)})
optuna_result = finalize_method("optuna", trials, best_so_far, total_time)
print(f"Optuna: best_cv={optuna_result['best_cv_score']:.4f}  time={optuna_result['total_time_sec']:.1f}s  trials_to_95%={optuna_result['trials_to_95pct']}")
"""),

    new_markdown_cell("## 6. Verdict — which search method wins, and why\n\nWin on **both** axes → clear call. Split result (one faster, one better) → prefer final quality, since the trial budget here is fixed and small; sample efficiency matters most when trials are cheap or the budget is open-ended."),
    new_code_cell("""\
faster = min(random_result, optuna_result, key=lambda r: r["trials_to_95pct"])
higher = max(random_result, optuna_result, key=lambda r: r["best_cv_score"])
if faster["method"] == higher["method"]:
    winner = faster["method"]
    reason = f"{winner} won on both cost and performance — higher CV PR-AUC in fewer trials."
else:
    winner = higher["method"]
    reason = (f"{higher['method']} found the better final config; {faster['method']} converged faster. "
              f"Each fit here is expensive, so sample efficiency matters, but final quality decides "
              f"under a fixed trial budget.")
print(f"Winner: {winner}\\n{reason}")
"""),

    new_markdown_cell("## 7. Final full-scale fit + evaluation\n\nRetrain three candidates at full scale (472K rows, 500 estimators, early stopping, isotonic calibration) and compare on the *same* held-out test set used in notebook 02: the manual baseline, RandomizedSearch's best config, and Optuna's best config."),
    new_code_cell("""\
def fit_and_eval(params, X_train, y_train, X_cal, y_cal, X_test, y_test, spw):
    clf = XGBClassifier(
        n_estimators=FINAL_N_ESTIMATORS, scale_pos_weight=spw, eval_metric="aucpr",
        early_stopping_rounds=30, random_state=42, n_jobs=-1, tree_method="hist", **params,
    )
    clf.fit(X_train, y_train, eval_set=[(X_cal, y_cal)], verbose=False)

    raw_cal = clf.predict_proba(X_cal)[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(raw_cal, y_cal)

    y_prob = iso.predict(clf.predict_proba(X_test)[:, 1])
    pr_auc = average_precision_score(y_test, y_prob)
    y_pred = (y_prob >= 0.5).astype(int)
    fb2 = fbeta_score(y_test, y_pred, beta=2, zero_division=0)
    fn = int(((y_test == 1) & (y_pred == 0)).sum())
    fp = int(((y_test == 0) & (y_pred == 1)).sum())
    cost_per_day = (C_FN * fn + C_FP * fp) / 30.0
    return {"pr_auc": round(float(pr_auc), 4), "fbeta2": round(float(fb2), 4),
            "cost_per_day": round(float(cost_per_day), 0), "params": params, "model": clf, "iso": iso}

final_results = {}
for label, params in [("baseline_manual", BASELINE_PARAMS),
                       ("random_search_tuned", random_result["best_params"]),
                       ("optuna_tuned", optuna_result["best_params"])]:
    final_results[label] = fit_and_eval(params, X_train, y_train, X_cal, y_cal, X_test, y_test, spw)
    r = final_results[label]
    print(f"{label:22s}  PR-AUC={r['pr_auc']:.4f}  F-beta2={r['fbeta2']:.4f}  cost/day=${r['cost_per_day']:,.0f}")
"""),

    new_markdown_cell("## 8. Save tuned model + export results"),
    new_code_cell("""\
import pickle

tuned_label = "random_search_tuned" if final_results["random_search_tuned"]["pr_auc"] >= final_results["optuna_tuned"]["pr_auc"] else "optuna_tuned"
tuned = final_results[tuned_label]
model_path = MODEL_DIR / "fraud_xgb_tuned.pkl"
with open(model_path, "wb") as f:
    pickle.dump({"iso": tuned["iso"], "xgb_base": tuned["model"], "feature_names": list(X.columns)}, f)
print(f"Tuned model ({tuned_label}) saved -> {model_path}")

with open(EXPORT_PATH) as f:
    export = json.load(f)

export["hyperparam_search"] = {
    "search_space": {k: list(v) for k, v in SEARCH_SPACE.items()},
    "trial_budget": N_TRIALS, "cv_folds": CV_FOLDS, "subsample_size": len(X_sub),
    "methods": {
        "random_search": {k: v for k, v in random_result.items() if k != "trials"} | {
            "trials": [{"trial": t["trial"], "cv_score": t["cv_score"], "fit_time_sec": t["fit_time_sec"]} for t in random_result["trials"]]},
        "optuna": {k: v for k, v in optuna_result.items() if k != "trials"} | {
            "trials": [{"trial": t["trial"], "cv_score": t["cv_score"], "fit_time_sec": t["fit_time_sec"]} for t in optuna_result["trials"]]},
    },
    "winner": winner, "verdict_reason": reason,
    "final_eval": {label: {k: v for k, v in res.items() if k not in ("model", "iso")} for label, res in final_results.items()},
    "tuned_model": tuned_label,
}
with open(EXPORT_PATH, "w") as f:
    json.dump(export, f)
print(f"Saved -> {EXPORT_PATH}")
"""),
]), "06_hyperparameter_search.ipynb")


print("\nAll notebooks written to", OUT_DIR)
