# Content Moderation System — Implementation Plan

- **Date:** 2026-06-20
- **Author:** Stuart Chen (Insider Threat SME)
- **Status:** Implemented — security-hardened 2026-06-22
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

## Reviewer Interaction Server

A lightweight Flask server handles reviewer disposition decisions (True Positive / False Positive / Inconclusive) when a reviewer clicks a button in the Chat DM card.

**Flow:**

```
Reviewer clicks button in Chat DM card
        ↓
openLink → https://<ngrok-url>/disposition?case_id=...&disposition=...&token=...
        ↓
Flask server (localhost:8080, exposed via ngrok)
        ↓
HMAC token verified → disposition written to DB → message tombstoned if true_positive
        ↓
HTML confirmation page (auto-closes after 3 seconds)
```

Google Chat also POSTs `CARD_CLICKED` events directly to `/chat/interactions` for in-card button handling.

**Components:**

| File | Purpose |
|------|---------|
| `src/moderation/actions/interaction_handler.py` | Flask app: `/disposition` GET + `/chat/interactions` POST |
| `src/moderation/actions/card_builder.py` | Builds cardsV2 alert cards with signed disposition button URLs |

**Deployment:** the Flask server starts in a daemon thread alongside the Pub/Sub listener when `run_content_moderation.py` is launched. Expose port 8080 publicly with ngrok (`ngrok http 8080`) and set `INTERACTION_BASE_URL` to the resulting https URL.

---

## Workspace Events Subscription Renewal / Recreation

Google Chat Workspace Events subscriptions expire after a maximum 24-hour TTL, and can also be silently suspended or deleted server-side. `ChatListener._reactivate_subscription()` runs on a periodic timer and handles all three cases so the pipeline stays live without manual intervention:

| Observed state | Action taken |
|---|---|
| `ACTIVE` | PATCH the TTL back to 24 h (`_renew_subscription_ttl()`) |
| `SUSPENDED` | POST `:reactivate` |
| Gone (`403`/`404`) | Recreate the subscription from scratch (`_recreate_subscription()`) |

Recreation POSTs a new subscription for `WORKSPACE_EVENTS_TARGET_RESOURCE` (the Chat space) publishing to the `PUBSUB_TOPIC_ID` topic, then swaps the listener's in-memory subscription name to the newly created one so subsequent renewal checks target the right resource.

**Files changed:** `src/moderation/chat_listener.py` — replaced the old reactivate-only check with the state-based renew/reactivate/recreate logic above.

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
    ├── card_builder.py            # builds cardsV2 alert cards with signed button URLs
    ├── chat_responder.py          # deletes message; DMs reviewer
    ├── email_notifier.py          # sends review notification email
    ├── case_writer.py             # creates case + evidence records
    └── interaction_handler.py    # Flask server for reviewer disposition callbacks
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
INTERACTION_BASE_URL=          # public ngrok https URL, e.g. https://abc123.ngrok-free.app
MODERATION_HMAC_SECRET=        # 64-char hex string; generate with: python -c "import secrets; print(secrets.token_hex(32))"
PUBSUB_TOPIC_ID=               # topic name (not full path), used to recreate the Workspace Events subscription if it expires
WORKSPACE_EVENTS_TARGET_RESOURCE=  # Chat space the subscription monitors, e.g. //chat.googleapis.com/spaces/AAQADyrUsoI
```

Note: `GOOGLE_SERVICE_ACCOUNT_KEY_PATH` already exists and is reused for Cloud Vision and Chat API calls. The service account requires additional roles: `roles/cloudvision.reader`, `roles/pubsub.subscriber`.

`INTERACTION_BASE_URL` and `MODERATION_HMAC_SECRET` are **required** — the pipeline will refuse to start if either is missing.

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

## Security Hardening (2026-06-22)

A post-implementation security review identified two HIGH-severity vulnerabilities in the reviewer interaction server. Both were fixed before any production traffic was processed.

---

### Vuln 1 — Unauthenticated `/disposition` endpoint (IDOR / auth bypass)

**Affected file:** `src/moderation/actions/interaction_handler.py`

**Issue:** The `/disposition` GET endpoint accepted `case_id` and `disposition` query parameters and wrote directly to the database with no authentication. Any unauthenticated party who reached the ngrok URL could silently close any open case (`false_positive`) or trigger an admin-credentialed Chat message tombstone (`true_positive`).

**Fix:** Every disposition button URL now includes a per-link HMAC-SHA256 token, computed as `HMAC-SHA256(MODERATION_HMAC_SECRET, "{case_id}:{disposition}")` at card-build time in `card_builder.py`. The `/disposition` handler verifies the token using `hmac.compare_digest()` (timing-safe) before executing any DB write or tombstone action. Requests with a missing or invalid token return HTTP 403.

**Files changed:**
- `src/moderation/actions/card_builder.py` — added `set_hmac_secret()` and `_sign_disposition()`
- `src/moderation/actions/interaction_handler.py` — added `_verify_disposition_token()`, token check in `disposition_link()`
- `scripts/run_content_moderation.py` — reads and passes `MODERATION_HMAC_SECRET`

---

### Vuln 2 — Missing webhook signature verification on `/chat/interactions`

**Affected file:** `src/moderation/actions/interaction_handler.py`

**Issue:** The `/chat/interactions` POST endpoint processed any inbound JSON payload without verifying it originated from Google Chat. An attacker who could reach the ngrok URL could POST a synthetic `CARD_CLICKED` payload to trigger DB writes and admin-credentialed message tombstoning.

**Fix:** The handler now verifies Google Chat's Bearer JWT on every POST using `google.oauth2.id_token.verify_token()` against Chat's public key endpoint (`googleapis.com/service_accounts/v1/jwk/chat@system.gserviceaccount.com`). The issuer claim is checked against `chat@system.gserviceaccount.com` and the audience claim against the configured `INTERACTION_BASE_URL/chat/interactions`. Requests that fail verification return HTTP 401 before any payload is processed.

**Files changed:**
- `src/moderation/actions/interaction_handler.py` — added `_verify_chat_jwt()`, JWT check at top of `interactions()`
- `scripts/run_content_moderation.py` — derives and passes `chat_audience` to `create_app()`

---

### Post-fix rescan result

A second security scan after applying the fixes found no remaining HIGH-severity vulnerabilities. The two issues above are the only confirmed security findings on this branch.

---

## Open Questions / Decisions Made

| Question | Decision |
|---|---|
| Google Chat interception model | Accepted: near-real-time post-publish delete via Pub/Sub |
| LLM for semantic screening | Anthropic Claude API (`claude-sonnet-4-6`) |
| Image moderation backend | Phase 1: Google Cloud Vision SafeSearch; Phase 2: local EfficientNet-B3 (see ADR 0007) |
| Phase 2 trigger | ≥ 5,000 labelled images in evidence DB + analyst sign-off |
