# Content Moderation System — Implementation Plan

- **Date:** 2026-06-20
- **Author:** Stuart Chen (Insider Threat SME)
- **Status:** Approved — pending implementation
- **Related ADRs:** ADR 0002 (Evidence), ADR 0003 (Pipeline), ADR 0007 (Image Moderation Backend)

---

## Scope

Automated content moderation for Google Chat messages, covering:

- **Text:** keyword first-layer + Claude LLM semantic second-layer
- **Images:** Google Cloud Vision SafeSearch violence detection (BMP, JPEG, GIF)
- **Actions:** pass / human review / block, with evidence preservation and case creation
- **Reviewer notification:** Google Chat DM + email to `stuart.chen@zeroinsiderai.com`

---

## Constraint: Google Chat Interception Model

Google Chat has no native pre-publish webhook. Messages appear immediately upon sending. The architecture subscribes to **Google Chat Events via Pub/Sub**, receives the message event within ~1–2 seconds, scans it, and **deletes flagged messages** via the Chat REST API (near-real-time post-publish delete).

This is accepted as the production approach. An alternative — using a Chat App bot as the posting intermediary — would provide true pre-publish control but changes the user workflow significantly and is out of scope.

---

## Architecture Overview

```
Google Chat  →  Pub/Sub subscription  →  content_guard service
                                               │
                              ┌────────────────┼────────────────┐
                              ▼                ▼                ▼
                         TextModerator   ImageModerator   Orchestrator
                          (keyword→LLM)  (Vision API)    (routes verdict)
                                                               │
                          ┌────────────────────────────────────┤
                          ▼                ▼                   ▼
                    Delete message    Human review       Pass (no action)
                    + case/evidence   notification
                    record            (Chat + email)
```

---

## Detection Logic

### Text — two-layer pipeline

**Layer 1: Keyword filter**

- Keyword source: [LDNOOBW](https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words) (~1,400 terms, MIT-licensed) as the base list
- Extended with a curated `tech_ecommerce_extension.txt` covering: fraud, scam, carding, counterfeit goods, IP theft, phishing lures, account-takeover terminology specific to tech/e-comm context
- Both lists stored as plain `.txt` files under `src/moderation/text/keywords/`; loaded at startup, never hardcoded
- Returns: `(matched: bool, matched_terms: list[str])`

**Layer 2: LLM semantic screener (Anthropic Claude API)**

- Invoked only when Layer 1 returns `matched=True`
- Determines whether the flagged content is a true positive (e.g. distinguishes "I got scammed" as a victim report vs. an active fraud offer)
- Returns: `TRUE_POSITIVE | FALSE_POSITIVE` with a brief rationale
- **Degradation:** if the Anthropic API is unreachable, the Layer 1 keyword result is used as the final verdict (fail-flagged, not fail-open)

**Text verdict decision table:**

| Layer 1 | Layer 2 | Final verdict |
|---|---|---|
| No match | (not called) | PASS |
| Match | TRUE_POSITIVE | FLAGGED |
| Match | FALSE_POSITIVE | PASS |
| Match | API unavailable | FLAGGED (fallback) |

### Image — Google Cloud Vision SafeSearch

Supported formats: BMP, JPEG, GIF (multi-frame).

**GIF handling:** frames sampled at 1 fps up to a cap of 30 frames via Pillow; each frame scored independently; maximum score across all frames is the verdict. A single violent frame triggers the tier.

**Score mapping:**

| SafeSearch likelihood | Numeric score | Action tier |
|---|---|---|
| `VERY_UNLIKELY` | 10 | Pass |
| `UNLIKELY` | 30 | Pass |
| `POSSIBLE` | 60 | Human review |
| `LIKELY` | 80 | Block |
| `VERY_LIKELY` | 95 | Block |

**Fallback:** if the Vision API is unreachable or errors, `score=None, action=REVIEW` — evidence is still preserved. Fails safe, not permissive.

See **ADR 0007** for the two-phase image moderation strategy (Phase 1: Cloud Vision; Phase 2: local fine-tuned EfficientNet-B3).

---

## Action Thresholds

| Condition | Action |
|---|---|
| Text PASS **and** image 0–50 | No action |
| Text FLAGGED **or** image 51–70 | Human review |
| Text FLAGGED **or** image 71–100 | Block |
| Either layer is BLOCK | Block (overrides REVIEW) |

**Block actions:**
1. Delete message via Chat REST API
2. Create case in `cases` table
3. Preserve content in `evidence_items` (text as JSON, image as stored artefact)
4. Notify reviewer via Chat DM + email

**Review actions:**
1. Message is NOT deleted (reviewer must see it)
2. Create case in `cases` table
3. Preserve content in `evidence_items`
4. Notify reviewer via Chat DM + email

---

## Reviewer Notification

- **Google Chat DM:** direct message to Stuart Chen (`MODERATION_REVIEWER_CHAT_USER_ID`) with: sender, space, timestamp, verdict, matched terms or score, link to message
- **Email:** to `stuart.chen@zeroinsiderai.com` with the same fields
- Triggered for both REVIEW and BLOCK verdicts

---

## New Package: `src/moderation/`

```
src/moderation/
├── __init__.py
├── config.py                      # typed config; secrets from .env
├── models.py                      # TextVerdict, ImageVerdict, ModerationDecision, ContentItem
├── orchestrator.py                # combines verdicts, triggers actions
├── chat_listener.py               # Pub/Sub subscriber for Google Chat Events
├── text/
│   ├── __init__.py
│   ├── keyword_filter.py          # Layer 1
│   ├── llm_screener.py            # Layer 2 (Anthropic API)
│   ├── moderator.py               # orchestrates layers 1+2
│   └── keywords/
│       ├── ldnoobw.txt            # base list (MIT-licensed)
│       └── tech_ecommerce_extension.txt
├── image/
│   ├── __init__.py
│   ├── protocol.py                # ImageScorerBackend protocol (pluggable)
│   ├── violence_detector.py       # VisionAPIBackend (Phase 1) + LocalModelBackend stub (Phase 2)
│   └── moderator.py               # maps score → PASS/REVIEW/BLOCK
└── actions/
    ├── __init__.py
    ├── chat_responder.py          # deletes message; DMs reviewer
    ├── email_notifier.py          # sends review notification email
    └── case_writer.py             # creates case + evidence records
```

---

## New DB Migrations

| File | Purpose |
|---|---|
| `db/0010_content_moderation.sql` | `moderation_decisions` table: `message_id`, `sender`, `space`, `text_verdict`, `image_verdict`, `action`, `score`, `created_at_utc` |
| `db/0011_moderation_source_system.sql` | Add `google_workspace.chat_moderation` to `source_system` enum |

Cases and evidence records reuse existing `cases` + `evidence_items` + `evidence_custody` tables. For REVIEW and BLOCK verdicts, a case is opened with `source_system = google_workspace.chat_moderation`.

---

## New Configuration Files

**`config/content_moderation.yml`**

```yaml
text_moderation:
  keyword_lists:
    - src/moderation/text/keywords/ldnoobw.txt
    - src/moderation/text/keywords/tech_ecommerce_extension.txt
  llm:
    model: claude-sonnet-4-6
    max_tokens: 256
    timeout_seconds: 10

image_moderation:
  backend: cloud_vision          # switch to "local_model" for Phase 2
  gif_frame_sample_fps: 1
  gif_max_frames: 30
  fallback_on_api_error: review  # fail-safe

actions:
  reviewer_email: stuart.chen@zeroinsiderai.com
  # reviewer_chat_user_id: set via env MODERATION_REVIEWER_CHAT_USER_ID

pubsub:
  # project_id and subscription_id set via env
```

---

## New `.env` Keys

```
ANTHROPIC_API_KEY=
MODERATION_REVIEWER_EMAIL=stuart.chen@zeroinsiderai.com
MODERATION_REVIEWER_CHAT_USER_ID=
PUBSUB_PROJECT_ID=
PUBSUB_SUBSCRIPTION_ID=
```

Note: `GOOGLE_SERVICE_ACCOUNT_KEY_PATH` already exists and is reused for Cloud Vision and Chat API calls. The service account requires additional roles: `roles/cloudvision.reader`, `roles/pubsub.subscriber`.

---

## New Dependencies (`requirements.txt`)

```
anthropic==X.Y.Z
google-cloud-vision==X.Y.Z
google-cloud-pubsub==X.Y.Z
Pillow==X.Y.Z                  # GIF frame sampling; may already be transitive
```

All pinned to exact versions on addition.

---

## New Script

**`scripts/run_content_moderation.py`** — entrypoint that loads config, initialises the Pub/Sub listener, and runs the moderation loop. Activated via `source venv/bin/activate && python scripts/run_content_moderation.py`.

---

## Tests

```
tests/moderation/
├── test_keyword_filter.py
├── test_llm_screener.py        # mocked Anthropic API
├── test_violence_detector.py   # mocked Vision API
├── test_orchestrator.py        # end-to-end verdict routing
└── fixtures/
    ├── sample_images/          # BMP, JPEG, GIF test images
    └── sample_messages.json
```

Coverage target: ≥ 80% on `src/moderation/` core logic, consistent with project rules.

---

## Documentation Updates

| File | Change |
|---|---|
| `docs/adr/0007-image-moderation-backend.md` | New — two-phase image moderation strategy ✅ |
| `README.md` | Add Content Moderation section (setup, env vars, run command) |
| `CLAUDE.md` | Update active projects table |

---

## Delivery Order

| # | Step | Depends on |
|---|---|---|
| 1 | DB migrations (0010, 0011) | — |
| 2 | `models.py` + `config.py` | 1 |
| 3 | Text layer (keyword filter → LLM screener → moderator) + unit tests | 2 |
| 4 | Image layer (Vision API → moderator) + unit tests | 2 |
| 5 | Actions layer (chat_responder, email_notifier, case_writer) | 1, 2 |
| 6 | Orchestrator + integration tests | 3, 4, 5 |
| 7 | Chat listener (Pub/Sub) | 6 |
| 8 | `run_content_moderation.py` + config YAML | 7 |
| 9 | README + CLAUDE.md updates | 8 |

---

## Open Questions / Decisions Made

| Question | Decision |
|---|---|
| Google Chat interception model | Accepted: near-real-time post-publish delete via Pub/Sub |
| LLM for semantic screening | Anthropic Claude API (`claude-sonnet-4-6`) |
| Image moderation backend | Phase 1: Google Cloud Vision SafeSearch; Phase 2: local EfficientNet-B3 (see ADR 0007) |
| Phase 2 trigger | ≥ 5,000 labelled images in evidence DB + analyst sign-off |
