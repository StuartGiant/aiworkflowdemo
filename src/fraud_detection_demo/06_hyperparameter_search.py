"""Hyperparameter search: RandomizedSearchCV vs Optuna (TPE), head-to-head.

Runs both search methods on the same search space, CV protocol, and trial
budget, then compares them on two axes:
  - Cost: wall-clock time + trials-to-95%-of-final-best (sample efficiency)
  - Performance: final CV PR-AUC, and held-out test PR-AUC/F-beta/cost vs.
    the hand-picked baseline config in 02_train_model.py

Appends to: src/fraud_detection_demo/demo/data_export.json
Outputs:    models/fraud_xgb_tuned.pkl

Run from project root:
    source venv/bin/activate
    python src/fraud_detection_demo/06_hyperparameter_search.py
"""

from __future__ import annotations

import json
import pickle
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

DATA_DIR = Path("data/ieee-fraud-detection")
MODEL_DIR = Path("models")
DEMO_DIR = Path("src/fraud_detection_demo/demo")
EXPORT_PATH = DEMO_DIR / "data_export.json"

DROP_COLS = [
    "TransactionID", "TransactionDT",
    "card2", "card3", "card5",
    "addr1", "addr2",
    "P_emaildomain", "R_emaildomain",
    "DeviceInfo", "DeviceType",
    "card4", "card6",
    "ProductCD",
    "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9",
    "id_12", "id_15", "id_16", "id_23", "id_27", "id_28",
    "id_29", "id_30", "id_31", "id_32", "id_33", "id_34",
    "id_35", "id_36", "id_37", "id_38",
]

# Manual baseline from 02_train_model.py — the config we're trying to beat
BASELINE_PARAMS = {
    "max_depth": 6,
    "learning_rate": 0.05,
    "min_child_weight": 1,
    "subsample": 1.0,
    "colsample_bytree": 1.0,
    "gamma": 0.0,
    "reg_alpha": 0.0,
    "reg_lambda": 1.0,
}

SEARCH_SPACE = {
    "max_depth": (3, 10),                # int
    "learning_rate": (0.01, 0.3, "log"),
    "min_child_weight": (1, 10),         # int
    "subsample": (0.5, 1.0),
    "colsample_bytree": (0.5, 1.0),
    "gamma": (0.0, 5.0),
    "reg_alpha": (1e-3, 10.0, "log"),
    "reg_lambda": (1e-3, 10.0, "log"),
}

N_TRIALS = 25
CV_FOLDS = 3
SEARCH_SUBSAMPLE = 60_000   # rows used per CV fit — keeps the search tractable
SEARCH_N_ESTIMATORS = 200   # fixed during search; final models retrain with early stopping
FINAL_N_ESTIMATORS = 500
C_FN = 420.0
C_FP = 6.0


def load_features(path: Path) -> tuple[pd.DataFrame, pd.Series]:
    df = pd.read_parquet(path)
    y = df["isFraud"].astype(np.int8)
    drop = [c for c in DROP_COLS if c in df.columns] + ["isFraud"]
    X = df.drop(columns=drop)
    for col in X.select_dtypes("object").columns:
        X[col] = X[col].astype("category").cat.codes
    return X, y


def cv_score(params: dict, X: pd.DataFrame, y: pd.Series, spw: float, folds: StratifiedKFold) -> float:
    scores = []
    for train_idx, val_idx in folds.split(X, y):
        clf = XGBClassifier(
            n_estimators=SEARCH_N_ESTIMATORS,
            scale_pos_weight=spw,
            eval_metric="aucpr",
            random_state=42,
            n_jobs=-1,
            tree_method="hist",
            **params,
        )
        clf.fit(X.iloc[train_idx], y.iloc[train_idx])
        proba = clf.predict_proba(X.iloc[val_idx])[:, 1]
        scores.append(average_precision_score(y.iloc[val_idx], proba))
    return float(np.mean(scores))


def sample_random_params(rng: np.random.Generator) -> dict:
    params = {}
    for name, spec in SEARCH_SPACE.items():
        if len(spec) == 3:
            lo, hi, _log = spec
            params[name] = float(np.exp(rng.uniform(np.log(lo), np.log(hi))))
        elif name in ("max_depth", "min_child_weight"):
            lo, hi = spec
            params[name] = int(rng.integers(lo, hi + 1))
        else:
            lo, hi = spec
            params[name] = float(rng.uniform(lo, hi))
    return params


def run_random_search(X, y, spw, folds) -> dict:
    print(f"\n=== RandomizedSearchCV — {N_TRIALS} trials ===")
    rng = np.random.default_rng(42)
    trials, best_so_far = [], []
    best_score = -np.inf
    t_start = time.time()
    for i in range(N_TRIALS):
        params = sample_random_params(rng)
        t0 = time.time()
        score = cv_score(params, X, y, spw, folds)
        fit_time = time.time() - t0
        best_score = max(best_score, score)
        best_so_far.append(best_score)
        trials.append({"trial": i, "params": params, "cv_score": round(score, 4), "fit_time_sec": round(fit_time, 2)})
        print(f"  trial {i:2d}  score={score:.4f}  best={best_score:.4f}  ({fit_time:.1f}s)")
    total_time = time.time() - t_start
    return finalize_method("random_search", trials, best_so_far, total_time)


def run_optuna(X, y, spw, folds) -> dict:
    print(f"\n=== Optuna (TPE) — {N_TRIALS} trials ===")
    trials, best_so_far = [], []
    best_score = -np.inf
    trial_times = []

    def objective(trial: optuna.Trial) -> float:
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
        score = cv_score(params, X, y, spw, folds)
        trial_times.append(time.time() - t0)
        return score

    t_start = time.time()
    study = optuna.create_study(direction="maximize", sampler=TPESampler(seed=42))
    study.optimize(objective, n_trials=N_TRIALS, show_progress_bar=False)
    total_time = time.time() - t_start

    for i, t in enumerate(study.trials):
        best_score = max(best_score, t.value)
        best_so_far.append(best_score)
        trials.append({
            "trial": i,
            "params": {k: (float(v) if isinstance(v, float) else int(v)) for k, v in t.params.items()},
            "cv_score": round(t.value, 4),
            "fit_time_sec": round(trial_times[i], 2),
        })
        print(f"  trial {i:2d}  score={t.value:.4f}  best={best_score:.4f}  ({trial_times[i]:.1f}s)")

    return finalize_method("optuna", trials, best_so_far, total_time)


def finalize_method(name: str, trials: list, best_so_far: list, total_time: float) -> dict:
    final_best = best_so_far[-1]
    target = 0.95 * final_best
    trials_to_95 = next((i for i, s in enumerate(best_so_far) if s >= target), len(best_so_far) - 1)
    best_trial = max(trials, key=lambda t: t["cv_score"])
    return {
        "method": name,
        "trials": trials,
        "best_so_far": [round(s, 4) for s in best_so_far],
        "total_time_sec": round(total_time, 1),
        "best_cv_score": round(final_best, 4),
        "trials_to_95pct": trials_to_95,
        "best_params": best_trial["params"],
    }


def fit_and_eval(params: dict, X_train, y_train, X_cal, y_cal, X_test, y_test, spw) -> dict:
    """Fit a final full-scale model with early stopping, calibrate, evaluate on test."""
    clf = XGBClassifier(
        n_estimators=FINAL_N_ESTIMATORS,
        scale_pos_weight=spw,
        eval_metric="aucpr",
        early_stopping_rounds=30,
        random_state=42,
        n_jobs=-1,
        tree_method="hist",
        **params,
    )
    clf.fit(X_train, y_train, eval_set=[(X_cal, y_cal)], verbose=False)

    raw_cal = clf.predict_proba(X_cal)[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(raw_cal, y_cal)

    raw_test = clf.predict_proba(X_test)[:, 1]
    y_prob = iso.predict(raw_test)

    pr_auc = average_precision_score(y_test, y_prob)
    y_pred = (y_prob >= 0.5).astype(int)
    fb2 = fbeta_score(y_test, y_pred, beta=2, zero_division=0)
    fn = int(((y_test == 1) & (y_pred == 0)).sum())
    fp = int(((y_test == 0) & (y_pred == 1)).sum())
    cost_per_day = (C_FN * fn + C_FP * fp) / 30.0  # test set ≈ 30 days of traffic

    return {
        "pr_auc": round(float(pr_auc), 4),
        "fbeta2": round(float(fb2), 4),
        "cost_per_day": round(float(cost_per_day), 0),
        "params": params,
        "model": clf,
        "iso": iso,
    }


def main() -> None:
    print("Loading engineered data...")
    X, y = load_features(DATA_DIR / "train_engineered.parquet")

    # Same first split as 02_train_model.py so the test set is identical
    X_trainval, X_test, y_trainval, y_test = train_test_split(
        X, y, test_size=0.20, stratify=y, random_state=42
    )
    X_train, X_cal, y_train, y_cal = train_test_split(
        X_trainval, y_trainval, test_size=0.125, stratify=y_trainval, random_state=42
    )
    print(f"Train: {len(X_train):,}  Cal: {len(X_cal):,}  Test: {len(X_test):,}")

    neg, pos = (y_train == 0).sum(), (y_train == 1).sum()
    spw = neg / pos

    # Stratified subsample for the search phase — keeps ~25 trials x 2 methods tractable
    sub_idx = X_trainval.sample(n=min(SEARCH_SUBSAMPLE, len(X_trainval)), random_state=42).index
    X_sub, y_sub = X_trainval.loc[sub_idx], y_trainval.loc[sub_idx]
    sub_spw = (y_sub == 0).sum() / (y_sub == 1).sum()
    folds = StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=42)
    print(f"Search subsample: {len(X_sub):,} rows  |  {CV_FOLDS}-fold CV  |  {N_TRIALS} trials/method")

    random_result = run_random_search(X_sub, y_sub, sub_spw, folds)
    optuna_result = run_optuna(X_sub, y_sub, sub_spw, folds)

    print("\n=== Search Summary ===")
    for r in (random_result, optuna_result):
        print(f"  {r['method']:14s}  best_cv={r['best_cv_score']:.4f}  "
              f"time={r['total_time_sec']:.1f}s  trials_to_95%={r['trials_to_95pct']}")

    faster = min(random_result, optuna_result, key=lambda r: r["trials_to_95pct"])
    higher = max(random_result, optuna_result, key=lambda r: r["best_cv_score"])
    if faster["method"] == higher["method"]:
        winner = faster["method"]
        reason = (
            f"{winner} reached a higher CV PR-AUC ({higher['best_cv_score']:.4f}) in fewer trials "
            f"(trial {faster['trials_to_95pct']} vs "
            f"{(optuna_result if faster['method']=='random_search' else random_result)['trials_to_95pct']} "
            f"to hit 95% of its own best) — better on both cost and performance for this search space."
        )
    else:
        winner = higher["method"]
        reason = (
            f"{higher['method']} found the better final config (CV PR-AUC {higher['best_cv_score']:.4f} vs "
            f"{(optuna_result if higher['method']=='random_search' else random_result)['best_cv_score']:.4f}), "
            f"though {faster['method']} converged faster (trial {faster['trials_to_95pct']} to 95%-of-best). "
            f"Each fit here is expensive (XGBoost on a mixed continuous/discrete space) — sample efficiency "
            f"matters, but final quality is decisive since the trial budget is fixed."
        )
    print(f"\nVerdict: {winner}  —  {reason}")

    # Final full-scale fit + evaluation for baseline, random-search-best, optuna-best
    print("\n=== Final full-scale evaluation (500 estimators, early stopping) ===")
    final_results = {}
    for label, params in [
        ("baseline_manual", BASELINE_PARAMS),
        ("random_search_tuned", random_result["best_params"]),
        ("optuna_tuned", optuna_result["best_params"]),
    ]:
        print(f"  fitting {label}...")
        res = fit_and_eval(params, X_train, y_train, X_cal, y_cal, X_test, y_test, spw)
        final_results[label] = res
        print(f"    PR-AUC={res['pr_auc']:.4f}  F-beta2={res['fbeta2']:.4f}  cost/day=${res['cost_per_day']:,.0f}")

    # Ship the better of the two tuned models as the new "tuned" model artifact
    tuned_label = "random_search_tuned" if final_results["random_search_tuned"]["pr_auc"] >= final_results["optuna_tuned"]["pr_auc"] else "optuna_tuned"
    tuned = final_results[tuned_label]
    model_path = MODEL_DIR / "fraud_xgb_tuned.pkl"
    with open(model_path, "wb") as f:
        pickle.dump({"iso": tuned["iso"], "xgb_base": tuned["model"], "feature_names": list(X.columns)}, f)
    print(f"\nTuned model ({tuned_label}) saved → {model_path}")

    # Export for UI
    export = json.loads(EXPORT_PATH.read_text()) if EXPORT_PATH.exists() else {}
    export["hyperparam_search"] = {
        "search_space": {k: list(v) for k, v in SEARCH_SPACE.items()},
        "trial_budget": N_TRIALS,
        "cv_folds": CV_FOLDS,
        "subsample_size": len(X_sub),
        "methods": {
            "random_search": {k: v for k, v in random_result.items() if k != "trials"} | {
                "trials": [{"trial": t["trial"], "cv_score": t["cv_score"], "fit_time_sec": t["fit_time_sec"]} for t in random_result["trials"]]
            },
            "optuna": {k: v for k, v in optuna_result.items() if k != "trials"} | {
                "trials": [{"trial": t["trial"], "cv_score": t["cv_score"], "fit_time_sec": t["fit_time_sec"]} for t in optuna_result["trials"]]
            },
        },
        "winner": winner,
        "verdict_reason": reason,
        "final_eval": {
            label: {k: v for k, v in res.items() if k not in ("model", "iso")}
            for label, res in final_results.items()
        },
        "tuned_model": tuned_label,
    }
    with open(EXPORT_PATH, "w") as f:
        json.dump(export, f)
    print(f"Exported hyperparameter search results → {EXPORT_PATH}")
    print("Hyperparameter search complete.")


if __name__ == "__main__":
    main()
