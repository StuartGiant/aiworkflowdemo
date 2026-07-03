"""Cost-based operating point + PR curve export.

Appends to: src/fraud_detection_demo/demo/data_export.json

Run from project root:
    source venv/bin/activate
    python src/fraud_detection_demo/03_threshold_and_cost.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_curve, average_precision_score

DATA_DIR = Path("data/ieee-fraud-detection")
DEMO_DIR = Path("src/fraud_detection_demo/demo")
EXPORT_PATH = DEMO_DIR / "data_export.json"

C_FN = 420.0   # cost of missed fraud (avg order value + chargeback penalty)
C_FP = 6.0     # cost of false positive (review time + customer friction)
CAPACITY_PER_DAY = 200  # analyst review capacity


def compute_cost(y_true: np.ndarray, y_prob: np.ndarray, threshold: float) -> float:
    y_pred = (y_prob >= threshold).astype(int)
    fn = ((y_true == 1) & (y_pred == 0)).sum()
    fp = ((y_true == 0) & (y_pred == 1)).sum()
    return float(C_FN * fn + C_FP * fp)


def main() -> None:
    print("Loading test predictions...")
    df = pd.read_parquet(DATA_DIR / "test_predictions.parquet")
    y_true = df["isFraud"].values
    y_prob = df["y_prob"].values

    pr_auc = average_precision_score(y_true, y_prob)
    print(f"PR-AUC: {pr_auc:.4f}")

    # PR curve
    precision, recall, thresholds = precision_recall_curve(y_true, y_prob)
    # precision_recall_curve returns one more point than thresholds
    thresholds = np.append(thresholds, 1.0)

    # Cost sweep
    costs, flagged_counts = [], []
    test_size = len(df)
    for t in thresholds:
        costs.append(compute_cost(y_true, y_prob, t))
        flagged_counts.append(int((y_prob >= t).sum()))

    costs = np.array(costs)
    flagged_counts = np.array(flagged_counts)

    # Scale flagged to per-day (test set is 20% of ~590k ≈ 118k; daily volume varies)
    # For demo: assume test set represents ~30 days of traffic
    flagged_per_day = flagged_counts / 30.0

    best_idx = int(np.argmin(costs))
    optimal_threshold = float(thresholds[best_idx])
    print(f"Optimal threshold: {optimal_threshold:.3f}  (min cost at that point)")
    print(f"  Precision: {precision[best_idx]:.3f}  Recall: {recall[best_idx]:.3f}")
    print(f"  Total cost: ${costs[best_idx]:,.0f}  |  Flagged/day: {flagged_per_day[best_idx]:.0f}")

    # Analyst capacity line — precision needed so flagged_per_day ≤ 200
    # Find thresholds where volume is manageable
    capacity_threshold_idx = None
    for i, fpd in enumerate(flagged_per_day):
        if fpd <= CAPACITY_PER_DAY:
            capacity_threshold_idx = i
            break

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

    # Load or init export JSON
    if EXPORT_PATH.exists():
        with open(EXPORT_PATH) as f:
            export = json.load(f)
    else:
        DEMO_DIR.mkdir(parents=True, exist_ok=True)
        export = {}

    # Capacity-constrained threshold: lowest cost where volume ≤ CAPACITY_PER_DAY
    cap_idx = None
    for i, fpd in enumerate(flagged_per_day):
        if fpd <= CAPACITY_PER_DAY:
            cap_idx = i
            break
    cap_threshold = float(thresholds[cap_idx]) if cap_idx is not None else optimal_threshold
    cap_precision = float(precision[cap_idx]) if cap_idx is not None else float(precision[best_idx])
    cap_recall = float(recall[cap_idx]) if cap_idx is not None else float(recall[best_idx])
    print(f"Capacity-constrained threshold: {cap_threshold:.3f}  (≤{CAPACITY_PER_DAY} flags/day)")
    print(f"  Precision: {cap_precision:.3f}  Recall: {cap_recall:.3f}")

    export["pr_curve"] = pr_curve
    export["pr_auc"] = round(pr_auc, 4)
    export["optimal_threshold"] = round(cap_threshold, 4)   # use capacity-constrained as default
    export["optimal_precision"] = round(cap_precision, 4)
    export["optimal_recall"] = round(cap_recall, 4)
    export["cost_optimal_threshold"] = round(optimal_threshold, 4)
    export["c_fn"] = C_FN
    export["c_fp"] = C_FP
    export["capacity_per_day"] = CAPACITY_PER_DAY

    with open(EXPORT_PATH, "w") as f:
        json.dump(export, f)

    print(f"Saved PR curve ({len(pr_curve)} points) → {EXPORT_PATH}")
    print("Threshold/cost analysis complete.")


if __name__ == "__main__":
    main()
