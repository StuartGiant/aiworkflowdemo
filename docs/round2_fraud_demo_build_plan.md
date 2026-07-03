# Round 2 Fraud Detection Demo — Build Plan

Source spec: `revised_round2_demo.docx`. Target: self-contained, offline-capable HTML demo for the Xiaomi Singapore technical interview.

## Current Status (as of 2026-07-01)

Done, in the **Xiaomi Security Engineer** project folder:
- Kaggle IEEE-CIS Fraud Detection dataset downloaded → `data/ieee-fraud-detection/` (train_transaction.csv, train_identity.csv, + test files not needed)
- Python packages installed (xgboost, lightgbm, shap, scikit-learn, pandas, numpy, matplotlib)
- EDA run ad hoc (not yet saved as a script): 590,540 transactions, 3.50% fraud rate, only 24.4% of transactions have a matching identity record, `dist1` 60% null, `R_emaildomain` 77% null, avg amount $135

Not started: everything below. Note the `source codes/00_eda.py` … `04_feature_importance.py` files already in this project belong to a **different, earlier project** (Predictive Maintenance) — they are not fraud-demo code and should not be reused or overwritten.

Once the "AI Workflow Demo" folder is mounted, recreate the file layout below there and carry over just the two CSVs.

## Suggested File Layout (new project)

```
data/ieee-fraud-detection/train_transaction.csv
data/ieee-fraud-detection/train_identity.csv
scripts/00_eda.py
scripts/01_feature_engineering.py
scripts/02_train_model.py
scripts/03_threshold_and_cost.py
scripts/04_adversarial_drift.py
scripts/05_shap_reason_codes.py
demo/index.html          <- final self-contained demo
demo/data_export.json    <- pre-scored held-out set + PR curve + SHAP values baked in
```

## Phase-by-Phase Tasks

### Phase 1 — EDA (redo as a saved script)
1. Port the ad hoc EDA into `00_eda.py` so it's reproducible in the new project.
2. Confirm fraud rate, null rates, join coverage match the numbers above.

### Phase 2 — Feature Engineering (4 signal layers, per JD taxonomy)
3. **Velocity (transaction layer):** rolling tx count/value per card over 1hr and 24hr windows.
4. **Ratio (account history):** refund_rate, chargeback_rate.
5. **Interaction (high-risk combos):** new account (<7 days) + high value (>$500) + express shipping.
6. **Network/address (graph layer):** card/billing/device country mismatch flags; shared device, email-domain, phone-prefix linkage counts.
7. Left-join transaction + identity tables; treat missing identity as its own signal (75.6% of rows lack it).
8. Impute or flag `dist1` (60% null) rather than dropping it.

### Phase 3 — Model
9. Train XGBoost with `scale_pos_weight≈30` — **not** SMOTE (don't stack both; it double-corrects and wrecks calibration).
10. Eval metric: `aucpr` (PR-AUC), not accuracy. `n_estimators=500, learning_rate=0.05, max_depth=6`.
11. Wrap with `CalibratedClassifierCV(method='isotonic', cv='prefit')` for honest probabilities.
12. Report PR-AUC and F-beta (β=2, recall-weighted).

### Phase 4 — Cost-Based Operating Point (Round 1 fix: no arbitrary recall floor)
13. Define business inputs: `C_FN ≈ $420` (avg order value + chargeback penalty), `C_FP ≈ $6` (review time + friction).
14. Compute `cost = C_FN*(fraud_n*(1-recall)) + C_FP*(flagged_n - tp_at_threshold)` across the PR curve; pick threshold minimizing cost.
15. Add analyst capacity ceiling: cap = precision @ N reviews/day; show flagged volume against that line.
16. Build the precision/recall/cost curve with a live slider (data feeds the HTML UI in Phase 8).

### Phase 5 — Adversarial / Drift Mode (the centerpiece — biggest Round 1 gap)
17. Pick one evasion strategy to simulate: amounts kept just under the velocity threshold, or device-fingerprint rotation.
18. Shift the test distribution to mimic the adapted population; show recall drop at the fixed threshold.
19. Compute PSI (Population Stability Index) on key features; define an alarm band it crosses.
20. Wire up a "retrain / add rule" trigger + a one-line champion-challenger note (new model shadow-tested before promotion).

### Phase 6 — Post-Detection Response Loop
21. Map score → tier: ALLOW / STEP-UP (OTP/liveness) / MANUAL REVIEW / AUTO-BLOCK + device ban.
22. Build a mock review queue: sort by `order_value × fraud_probability`, boosted by time-to-ship SLA.
23. Render as a ranked table (5 rows is enough) in the UI.

### Phase 7 — Ground Truth & Label Pipeline (Round 1 fix — be specific)
24. Document label sources in priority order: confirmed chargebacks/disputes > manual-review outcomes > law-enforcement/account freezes > heuristic linkage labels.
25. State QC bar: two independent reviewers, Cohen's κ > 0.8 before data enters training; disagreements escalate, never silently dropped.
26. Add a demo banner: "Labels for the last 60 days are still maturing" (chargebacks land 60–90 days post-transaction — retrain cadence must account for the lag).

### Phase 8 — SHAP Explainability as Compliance Reason Codes
27. `shap.TreeExplainer(model)`; compute per-transaction SHAP values.
28. Render top-5 contributors as a horizontal bar/waterfall — framed as adverse-action "reason codes," not just feature importance.

### Phase 9 — Demo UI (self-contained HTML, offline)
29. Layout: input panel (left) — amount, account age, card vs billing country, shipping type, last-hour tx count, device type.
30. Output/decision pipeline (right): fraud probability (color-coded), decision tier badge, SHAP reason-code chart, cost-threshold slider, PR curve with operating point marker, adversarial toggle + PSI gauge + retrain trigger, mini review-queue table.
31. Bake pre-scored held-out data + PR curve + SHAP values into a JSON blob so the slider recomputes metrics client-side with no server/model dependency.

### Phase 10 — Operational Readiness Panel
32. Model health: PR-AUC/recall over time, alert on floor breach.
33. Data health: PSI per feature (ties to Phase 5).
34. Ops health: queue depth vs analyst capacity, step-up completion rate, time-to-decision SLA.
35. Note who gets paged when recall drops or the queue backs up.

### Phase 11 — Polish & Rehearsal
36. Pre-load all data/models so the demo runs fully offline.
37. Record a 60-second screen-recording backup.
38. Rehearse the 90-second verbal walkthrough (frame → signals → model → threshold → adversarial → after-the-flag → governance — full script in §16 of the source doc).
39. Prep answers for: label pipeline specifics, adversarial adaptation, precision/recall trade-off, role calibration (vendor-augmentation framing, not build-from-scratch).

## Key Positioning Line (say this up front)
"The Singapore team runs mostly on commercial vendor tooling under data-localisation constraints. So I built this not to replace a vendor engine, but to show how I'd evaluate one, tune its operating point to our cost structure, and add the custom risk layer and analyst tooling on top."

## Explicit Don'ts (from the source doc)
- Don't chase AUC alone with no decision pipeline.
- Don't use UCI/ULB creditcard.csv (too simple, overused).
- Don't demo a Jupyter notebook — a live UI is more memorable.
- Don't default to 0.5 threshold or justify with a bare "85% recall."
- Don't stack SMOTE + scale_pos_weight.
- Don't pitch as replacing a vendor engine.

## Estimated Timeline (~4 days)
| Task | Estimate |
|---|---|
| Dataset + EDA + feature engineering | 1 day |
| Model train + calibration + cost-threshold logic | 0.5 day |
| Adversarial/drift simulation + PSI | 0.5 day |
| SHAP reason codes | 2 hours |
| Self-contained HTML UI | 1 day |
| Post-detection + monitoring panels | 0.5 day |
| Polish, pre-load, backup recording | 2 hours |
