# ADR 0007 — Image Moderation Backend

- **Status:** Accepted
- **Date:** 2026-06-20
- **Author:** Stuart Chen (Insider Threat SME)
- **Supersedes:** —
- **Related:** ADR 0003 (Pipeline), ADR 0002 (Evidence), ADR 0005 (Detection)

## Context

The content moderation pipeline must score images (BMP, JPEG, GIF) for violence
before or immediately after publication in Google Chat. The score drives a
three-tier action:

| Score | Action |
|---|---|
| 0–50 | Pass — no action |
| 51–70 | Human review — notify Stuart Chen, preserve evidence |
| 71–100 | Block — delete message, create case, preserve evidence |

Two viable approaches were evaluated: a **cloud API** (Google Cloud Vision
SafeSearch) and a **locally trained ML model** (fine-tuned EfficientNet).
Neither is universally superior; the right choice depends on data-privacy
posture, labelled-data availability, ops maturity, and time-to-production
constraints.

This ADR documents a **phased migration**: ship with Cloud Vision (low friction,
fast), accumulate labelled data from human-review decisions, then migrate to a
local model (higher privacy, full domain control) once the training set is
mature.

---

## Decision

### Phase 1 — Google Cloud Vision SafeSearch (current)

**Accepted. Production-ready immediately.**

Google Cloud Vision SafeSearch returns a five-point likelihood score for the
`violence` category: `VERY_UNLIKELY | UNLIKELY | POSSIBLE | LIKELY |
VERY_LIKELY`. This is mapped to the 0–100 integer scale as follows:

| SafeSearch likelihood | Numeric score | Action tier |
|---|---|---|
| `VERY_UNLIKELY` | 10 | Pass |
| `UNLIKELY` | 30 | Pass |
| `POSSIBLE` | 60 | Human review |
| `LIKELY` | 80 | Block |
| `VERY_LIKELY` | 95 | Block |

**Image format handling:**

- JPEG, BMP: passed directly to the Vision API as base64-encoded bytes.
- GIF (multi-frame): frames are sampled at 1 fps (via Pillow) up to a cap of
  30 frames; each frame is scored independently; the maximum score across all
  frames is the verdict score. A single violent frame triggers the tier.

**Fallback behaviour:** if the Vision API is unreachable or returns a non-2xx
response, the moderator assigns `score=None, action=REVIEW` and flags the item
for human review. Evidence is still preserved. This ensures the pipeline fails
safe rather than permissive.

**Authentication:** uses the existing GCP service account (least-privilege —
`roles/cloudvision.reader` only). Key path from env var
`GOOGLE_SERVICE_ACCOUNT_KEY_PATH`; no hardcoded credentials.

**Implementation location:** `src/moderation/image/violence_detector.py`
with a `VisionAPIBackend` class that implements the `ImageScorerBackend`
protocol defined in `src/moderation/image/protocol.py`. The protocol allows
Phase 2 to slot in without changing the orchestrator.

---

### Phase 2 — Local fine-tuned model (future, trigger-gated)

**Not yet started. Target: when ≥ 5,000 labelled human-review images have
accumulated in the evidence database.**

#### Rationale for migration

| Concern | Cloud Vision (Phase 1) | Local model (Phase 2) |
|---|---|---|
| Data privacy | Images leave the network | Images never leave infra |
| Customisability | Fixed SafeSearch taxonomy | Trained on your own content |
| Violence specificity | Google's definition | You define the label |
| Ongoing cost | Per-image API fee | Compute only |
| Setup cost | Low | High (labelling + training) |
| Maintenance burden | None | Quarterly re-evaluation |
| Time to production | Days | Weeks–months |

The primary driver for Phase 2 is **data privacy**: images sent for moderation
may contain sensitive content (HR disputes, evidence screenshots, personal
information). Long-term, images should not be transmitted to a third-party API.

#### Architecture

**Base model:** `EfficientNet-B3` pre-trained on ImageNet, fine-tuned via
transfer learning. Chosen for strong accuracy/throughput trade-off and broad
support in `timm`.

**Output head:** single sigmoid neuron → violence probability 0.0–1.0,
multiplied by 100 for the integer score scale.

**Training data sources:**

1. Accumulated human-review decisions from `evidence_items` (primary,
   domain-specific).
2. Public datasets to bootstrap before sufficient internal data exists:
   - RWF-2000 (real-world surveillance fights; most enterprise-relevant)
   - Movies Violence Dataset (clean binary labels)
   - Supplemented with negative samples from COCO (non-violent scenes)
3. Minimum viable set: 5,000 labelled images per class before fine-tuning
   produces reliable generalisation.

**Training infrastructure:**

- Vertex AI Training job (GCP-native, integrates with existing service
  accounts).
- GPU: NVIDIA T4 (≥ 8 GB VRAM); fine-tuning EfficientNet-B3 on 10k images
  ≈ 2–4 hours.
- Artefact: ONNX export (`model.onnx`) for portable, CPU-viable inference.
  Inference latency ≈ 50–150 ms/image on CPU (ONNX Runtime), which fits
  within the Pub/Sub near-real-time moderation window.

**Model versioning and reproducibility:**

- Checkpoint SHA-256 pinned in `config/content_moderation.yml` under
  `image_moderation.backend.local.model_sha256`.
- Training seed, dataset split hashes, and `timm` version recorded in Vertex
  AI run metadata (satisfies the project reproducibility rule).
- Model artefacts stored in GCS; path in env var
  `MODERATION_LOCAL_MODEL_GCS_URI`.

**GIF handling:** identical frame-sampling approach as Phase 1.

**Explainability:** Grad-CAM heatmap generated per scored image for REVIEW and
BLOCK verdicts. Heatmap stored as a PNG artefact in the evidence DB alongside
the source image, giving Stuart's review queue a spatial explanation of where
violence was detected.

**Fallback:** if the local model file is missing or raises an exception,
behaviour is identical to Phase 1 fallback (`score=None, action=REVIEW`).

**Implementation:** `LocalModelBackend` class in
`src/moderation/image/violence_detector.py` implementing the same
`ImageScorerBackend` protocol. Switching phases requires only a config change:

```yaml
# config/content_moderation.yml
image_moderation:
  backend: cloud_vision   # switch to "local_model" for Phase 2
```

No orchestrator or action code changes required.

#### Phase 2 trigger criteria

| Criterion | Target |
|---|---|
| Labelled images in evidence DB | ≥ 5,000 per class |
| Human-review disposition accuracy (analyst-validated) | ≥ 85 % |
| False-positive rate on held-out test set | ≤ 10 % |
| Grad-CAM review by Stuart Chen | Sign-off required before promotion |

---

## Consequences

### Positive

- **Phase 1** ships fast with no ML infrastructure cost; full format coverage
  (BMP/JPEG/GIF) on day one.
- **Pluggable backend protocol** means the Phase 2 migration is a config toggle,
  not a code refactor.
- **Evidence accumulation** during Phase 1 directly produces the training corpus
  for Phase 2 — the two phases are complementary, not competing.
- **Fail-safe fallback** (REVIEW on API error) means moderator never silently
  passes content due to infrastructure failure.
- **Grad-CAM** in Phase 2 makes model decisions auditable and defensible in
  evidence reports.

### Negative / risks

- Phase 1 images are transmitted to Google; mitigated by the existing GCP DPA
  and the fact that the same service account already has admin-scoped Chat
  access.
- Phase 2 training data quality depends on analyst disposition accuracy; noisy
  labels degrade model precision.
- Phase 2 introduces model drift risk; quarterly re-evaluation schedule must be
  maintained.
- ONNX CPU inference at 50–150 ms/image may introduce noticeable latency if
  message burst rate is high (> 10 images/second); GPU serving would be needed
  at that scale.

### Production gaps (Phase 1)

| Demo / Phase 1 | Production / Phase 2 |
|---|---|
| Cloud Vision API, images leave infra | Local model, images stay on-prem |
| Binary PASS/REVIEW/BLOCK | Sub-category labels (weapon, blood, fighting) |
| No spatial explanation | Grad-CAM heatmap stored with evidence |
| Fixed SafeSearch taxonomy | Custom taxonomy trained on your content |
| No model versioning needed | Checkpoint SHA-256 pinned; Vertex AI run metadata |

---

## Alternatives considered

| Option | Why rejected |
|---|---|
| **AWS Rekognition** | Equivalent capability to Cloud Vision but introduces a second cloud provider; GCP is already the project's primary cloud. |
| **NudeNet / open-source NSFW classifiers** | Focused on nudity, not violence. Wrong taxonomy for this use case. |
| **Train local model immediately (skip Phase 1)** | No labelled domain-specific training data exists yet. A model trained on public datasets only would have unknown false-positive rate on this environment's content. Phase 1 accumulates the right data first. |
| **Frame-level video analysis (Vertex AI Video Intelligence)** | GIFs are short, low-frame-rate sequences; per-frame image scoring is sufficient and avoids the video API cost and latency overhead. |
| **Threshold-free human review of all images** | Not scalable; defeats the purpose of automated moderation. Reserve human review for the ambiguous 51–70 band. |

---

## Confidence

**82 / 100** — Phase 1 design is well-understood and low-risk; the pluggable
backend protocol is a proven pattern. Gap to higher confidence is Phase 2:
training data accumulation timeline is uncertain, and the false-positive rate
on the local model won't be known until the held-out test set is evaluated.
Phase 2 trigger criteria are defined to ensure the migration only happens when
the model is demonstrably ready.
