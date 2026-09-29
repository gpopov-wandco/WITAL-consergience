# Verified facts

Every statement about an external service that code depends on is listed here with the
source read in this build session and the date. Items that could not be verified
because their source was unreachable from the build environment (see D-005 in
`docs/DECISIONS.md`) are marked **VERIFY** with the expected source and the runtime
check that covers them.

Legend: `verified` = read in this session from the named source; `VERIFY` = still open.

## Anthropic

| Fact | Value | Source, date | Status |
|---|---|---|---|
| Default model id and price | `claude-sonnet-5-5`; USD 2 input, USD 10 output per million tokens; 1M context; 128K max output; adaptive thinking; default effort high; retirement not sooner than 2027-09-28 | platform.claude.com/docs/en/about-claude/models/overview and /model-deprecations, 2026-09-29 | verified |
| Failover model id and price | `claude-sonnet-5`; USD 2 / 10; active; retirement not sooner than 2027-06-30 | pricing and deprecations pages, 2026-09-29 | verified |
| Cheap model | `claude-haiku-4-5` (alias of `claude-haiku-4-5-20251001`); USD 1 / 5; 200K context; effort not supported; retirement not sooner than 2026-10-15 | models overview and deprecations, 2026-09-29 | verified; `doctor` warns |
| Fable | `claude-fable-5-1`; USD 10 / 50; not used by the product | models overview, 2026-09-29 | verified |
| Cache pricing | 5-minute cache write 1.25x input; cache read 0.1x input (USD 0.20 per million on Sonnet 5.5) | pricing page, 2026-09-29 | verified |
| Minimum cacheable prefix | 512 tokens on Sonnet 5.5, Opus 5.5, Fable 5.1; shorter prompts are silently not cached (both cache usage fields 0) | prompt-caching page "Cache limitations", 2026-09-29 | verified |
| `inference_geo` | values `global` (default) and `us` only; `us` costs 1.1x; supported on Claude 4.6 and later; Haiku 4.5 and earlier return 400 when the parameter is present; `usage.inference_geo` reports where inference ran | data-residency page, 2026-09-29 | verified |
| Effort | `output_config.effort` in `low`, `medium`, `high`, `xhigh`, `max`; default high on Sonnet 5.5; Haiku 4.5 does not support it | effort page and SDK `types/output_config_param.py`, 2026-09-29 | verified |
| Structured outputs | `output_config.format = {"type": "json_schema", "schema": ...}`; unsupported: numeric constraints, `minLength`/`maxLength`, `additionalProperties` other than `false`; at most 20 strict tools and 24 optional parameters per request; changing the format invalidates the prompt cache; `stop_reason: "refusal"` may ignore the schema | structured-outputs page, 2026-09-29 | verified |
| Forced tool choice | `tool_choice` `any` and `tool` return 400 on Sonnet 5.5, Opus 5.5, Fable 5.1; `auto` and `none` work | implement-tool-use page, 2026-09-29 | verified |
| Thinking parameters | `thinking: {"type": "disabled"}` returns 400 on Opus 5.5 at every effort; extended thinking with `budget_tokens` rejected on 4.7 and later; the adapter never sends a `thinking` block | extended-thinking and effort pages, 2026-09-29 | verified |
| Spend limits | organisation or workspace spend limit returns HTTP 400; usage-tier monthly spend cap returns HTTP 429 without `retry-after` and keeps failing | errors page, 2026-09-29 | verified |
| SDK retries | official SDK retries transient failures twice by default, honouring `retry-after`; `max_retries` configurable | errors page, 2026-09-29 | verified |
| Request id | `request-id` header; SDK exposes `_request_id` on responses | errors page, 2026-09-29 | verified |
| `stop_reason` values | `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `pause_turn`, `refusal`, `model_context_window_exceeded` | SDK 1.9.0 `types/stop_reason.py` | verified |
| Usage fields | `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `inference_geo`, `service_tier` | SDK 1.9.0 `types/usage.py` | verified |
| SDK 1.9.0 HTTP layer | depends on `httpx2>=2.0.0,<3`; `respx`/`pytest-httpx` do not see its traffic; custom clients must be `httpx2.AsyncClient`; `httpx2.MockTransport` exists | PyPI metadata and SDK `MIGRATION.md`, 2026-09-29 | verified |
| Token counting | `messages.count_tokens` is free, separate rate limit, accepts system, tools, messages | token-counting page, 2026-09-29 | verified |
| Models API | `GET /v1/models` lists ids with `max_input_tokens`, `max_tokens`, capabilities | models overview, 2026-09-29 | verified |
| Anthropic EU processing | none: `inference_geo` only `global` or `us` | data-residency page, 2026-09-29 | verified |

## Python packages (PyPI JSON API, 2026-09-29)

All versions below install from wheels on Python 3.13.12 (Linux) into a clean venv
(`uv pip install --only-binary :all:`), see D-003.

| Package | Version | Note |
|---|---|---|
| anthropic | 1.9.0 (uploaded 2026-09-28) | requires `httpx2` |
| httpx2 | 2.13.1 | `MockTransport`, `ASGITransport` present |
| fastapi / starlette / uvicorn | 0.141.1 / 1.7.0 / 0.54.0 | |
| sqlalchemy / alembic / aiosqlite / asyncpg | 2.1.1 / 1.20.0 / 0.22.1 / 0.31.0 | |
| pydantic / pydantic-settings | 2.13.5 / 2.15.0 | |
| reportlab / pillow / qrcode / pypdf / pypdfium2 | 5.0.1 / 12.3.0 / 8.2 / 6.19.0 / 5.13.0 | |
| cryptography / argon2-cffi / itsdangerous | 50.0.1 / 25.1.0 / 2.2.0 | |
| limits / structlog / prometheus-client / typer / tenacity | 5.8.0 / 26.1.0 / 0.26.0 / 0.27.2 / 9.1.4 | |
| pyyaml / tzdata / icalendar / phonenumbers / lingua-language-detector | 6.0.3 / 2026.4 / 7.3.0 / 9.0.40 / 2.2.0 | lingua requires Python >= 3.12 |
| jinja2 / python-multipart | 3.1.6 / 0.0.32 | |
| pytest / pytest-asyncio / pytest-cov / hypothesis / time-machine / playwright | 9.1.1 / 1.4.0 / 7.1.0 / 6.168.3 / 3.5.1 / 1.63.0 | |
| ruff / mypy / bandit / pip-audit | 0.16.9 / 2.3.1 / 1.9.4 / 2.10.1 | |

## Vendored files

| File | Value | Source, date | Status |
|---|---|---|---|
| `static/vendor/htmx.min.js` | htmx 2.0.11, 0BSD, SHA-256 `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`, 52,182 bytes | npm registry tarball `htmx.org-2.0.11.tgz` and `raw.githubusercontent.com/bigskysoftware/htmx/v2.0.11/dist/htmx.min.js`, identical digests, 2026-09-29 | verified |
| Inter | 4.1 static TTFs (Regular, Medium, SemiBold, Bold), OFL 1.1, no Reserved Font Name; character map contains Turkish (İ ı ş ğ ç ö ü), Ukrainian (і ї є ґ) and Russian glyphs | github.com/rsms/inter release v4.1 `Inter-4.1.zip`, `LICENSE.txt`; fontTools cmap check, 2026-09-29 | verified |
| Syne | static instances Regular (400), Bold (700), ExtraBold (800) generated with fontTools from `Syne[wght].ttf`; OFL 1.1, no Reserved Font Name; Turkish glyphs present, Cyrillic absent (headings in RU/UK use Inter Bold) | raw.githubusercontent.com/google/fonts/main/ofl/syne, fontTools cmap check, 2026-09-29 | verified |

## Retell

| Fact | Value | Source, date | Status |
|---|---|---|---|
| Webhook signature | header `X-Retell-Signature` = `v={timestamp_ms},d={hex64}`; digest = HMAC-SHA256 keyed with the API key over `body + str(timestamp_ms)`; 5-minute tolerance on the timestamp; the demo server verifies against the compact JSON serialisation (`json.dumps(payload, separators=(",", ":"), ensure_ascii=False)`) of the parsed body | `retell-sdk` 6.0.1 wheel, `retell/lib/webhook_auth.py`; RetellAI/retell-custom-llm-python-demo `app/server.py`, 2026-09-29 | verified; the verifier accepts the raw body and the compact re-serialisation |
| Custom LLM websocket protocol | route `/llm-websocket/{call_id}`; inbound `interaction_type` in `ping_pong` (`timestamp`), `call_details` (`call`), `update_only` (`transcript`), `response_required` and `reminder_required` (`response_id`, `transcript` of `{role: agent|user|system, content}`); outbound `response_type` in `config` (`auto_reconnect`, `call_details`), `ping_pong` (`timestamp`), `response` (`response_id`, `content`, `content_complete`, `end_call`, `transfer_number`); a newer `response_id` abandons the running generation | demo `app/custom_types.py`, `app/server.py`, 2026-09-29 | verified |
| EU processing | not offered per the prompt (section 2.6); docs.retellai.com unreachable here | expected source docs.retellai.com, trust.retellai.com | VERIFY; gated by `voice.eu_only` and `voice.us_transfer_acknowledged` |
| Web call endpoint, agent and number APIs, billing | `POST /v3/create-web-call` per the prompt | expected source docs.retellai.com | VERIFY; `doctor --live` calls the API |

## Twilio

| Fact | Value | Source, date | Status |
|---|---|---|---|
| Request signature | `X-Twilio-Signature` = base64(HMAC-SHA1(auth token, URL without port + concatenation of sorted POST `name+value` pairs)); also computed with the explicit port; JSON bodies carry `bodySHA256` in the query string compared with SHA-256 of the body; constant-time compare | `twilio` 9.11.2 wheel, `twilio/request_validator.py`, 2026-09-29 | verified |
| ConversationRelay TwiML | `<Connect><ConversationRelay url=... language=... ttsLanguage=... transcriptionLanguage=... ttsProvider=... voice=... transcriptionProvider=... speechModel=.../></Connect>` | `twilio` 9.11.2 `twiml/voice_response.py` `conversation_relay()`, 2026-09-29 | verified |
| ConversationRelay websocket message schema | `setup`, `prompt` (`voicePrompt`, `lang`, `last`), `dtmf`, `interrupt`, `error` inbound; `text` (`token`, `last`, `lang`), `end`, `language` outbound, per the prompt and the public Twilio docs | expected source twilio.com/docs/voice/twiml/connect/conversationrelay | VERIFY; simulator-tested |
| German number regulatory bundle approval time | unknown | Twilio console | VERIFY (handout) |

## Stripe

| Fact | Value | Source, date | Status |
|---|---|---|---|
| Webhook signature | `Stripe-Signature` = `t={unix_seconds},v1={hex}`; digest = HMAC-SHA256(secret, `f"{t}.{payload}"`); default tolerance 300 s | `stripe` 15.6.1 wheel, `stripe/_webhook.py`, 2026-09-29 | verified |
| Checkout Session, PaymentIntent, SCA, "Zuordnungsnummer" mapping, pre-authorisation | per the prompt (F15) | expected source stripe.com docs, PSP and lawyer | VERIFY |

## Meta WhatsApp Cloud API

| Fact | Value | Source, date | Status |
|---|---|---|---|
| Graph API version | `v26.0` is the version the current official Python SDK targets, so it is a currently supported version | `facebook-business` 26.0.2 wheel, `facebook_business/apiconfig.py` (`API_VERSION: v26.0`), 2026-09-29 | verified for existence; default of `GC_WA_GRAPH_VERSION`; re-check on developers.facebook.com before go-live |
| Webhook signature | `X-Hub-Signature-256` = `sha256=` + HMAC-SHA256(app secret, raw body) | prompt 6.6 (developers.facebook.com unreachable) | VERIFY; `doctor --live` sends a signed test webhook |
| Message pricing Germany | utility EUR 0.0456, marketing EUR 0.1131; service messages free up to 1,000 per business number and month from 2026-10-01 | prompt 2.6 and 2.7 | VERIFY; per-tenant dated `price_table` rows, never constants |
| Error codes | 0, 190, 4, 80007, 130429, 131047, 131048, 368, 131049, 131050, 131026, 131056, 132000, 132001, 132015, 132016 as in prompt 6.6 | prompt 6.6 | VERIFY; behaviour table in configuration |
| Send idempotency | Meta documents no send idempotency key | prompt 6.6 | VERIFY; timeouts are `unknown`, reconciled by status webhooks |
| Template limits | 100 creations per hour, 250 per unverified portfolio; body 1,024, header and footer 60, button 25, quick replies at most 3 | prompt F14 | VERIFY |

## PMS vendors

| Fact | Value | Source, date | Status |
|---|---|---|---|
| Beds24 | invite code exchanged at `GET /authentication/setup` for a refresh token; `token` header lasts 24 h; `/inventory/rooms/calendar` empty without `include*` flags; 100 credits per 5 min per account (`X-FiveMinCreditLimit-Remaining`, `X-RequestCost`); inquiry `POST /bookings` with `status: inquiry` | prompt F08 (beds24.com unreachable) | VERIFY; contract tests use synthetic fixtures; `doctor --live` calls `GET /authentication/details` |
| apaleo | OAuth2 client credentials; `/booking/v1/offers`; `POST /booking/v1/bookings` creates real reservations (never called); 3,500 req/min live, 200 dev | prompt F08 (apaleo.dev unreachable) | VERIFY |
| Mews | Distributor API keyless read (`HotelId`, `ConfigurationId`); Connector needs certification; time units UTC midnight of the local day; 200 requests per token per 30 s; demo host `api.mews-demo.com`; deep-link parameters `mewsStart`, `mewsEnd`, `mewsAdultCount`, `mewsChildCount`, `language`, `currency` | prompt F08 and F10 (api.mews.com unreachable) | VERIFY |
| Smoobu, ibelsa | HMAC-signed requests (legacy `Api-Key` ends 2026-10-31); ibelsa 1,000 req/min per IP | prompt F08 | VERIFY; (S) items |

## Law and commercial

| Fact | Value | Source, date | Status |
|---|---|---|---|
| AI Act Art. 50(1) | applies from 2026-08-02 | prompt 2.6 | VERIFY regulation number; behaviour implemented regardless |
| BMG §§ 29, 30, 54; UWG § 7; TDDDG § 25; BFSG | as used in the prompt | prompt 2.6 (gesetze-im-internet.de unreachable) | VERIFY texts with counsel; behaviour implemented as specified |
| Meldeschein data retention | one year after departure, deletion within three months after | prompt F15 | VERIFY with counsel; configurable 365 to 455 days, default 380 |
| Hetzner smallest plan, Storage Box pricing | about EUR 5.99 per month | prompt 2.8 | VERIFY (handout) |
| Resend region, Retell erasure API, HubSpot scopes, Stripe account owner, DPO need | unknown | vendor pages, counsel | VERIFY (compliance pack lists them) |
