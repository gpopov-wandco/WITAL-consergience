# Architecture

One Python process serves three guest channels (WhatsApp Cloud API, the website
widget, a phone line), the hotel admin, the operator console and the job worker.
Every business table carries a `tenant_id`; one installation serves many hotels.

## Layout

| Package | Responsibility |
|---|---|
| `concierge.settings`, `context`, `runtime`, `app` | environment (Appendix A), the app context (settings, database, keyring, clock), the FastAPI app with health, metrics and security headers |
| `security/` | AES-256-GCM envelope encryption with per-tenant AAD, keyed blind indexes, webhook signatures (Meta, Retell, Twilio, Stripe, reservation push), SSRF-safe fetcher, moving-window limiter, Argon2id passwords, server sessions, signed links |
| `db/` | SQLAlchemy 2 async models, Alembic migrations, tenant isolation (`with_loader_criteria` plus a `before_flush` guard), append-only rows |
| `tenants/` | profile schema (Appendix C), tenant service, secrets store, client packs |
| `agent/` | the Brain (F01), tools (F02), guardrails (F03), the deterministic responder (F04), prompt assembly, validators, upsells, language detection |
| `kb/` | knowledge base entries with statuses, sensitivity, validity windows and snapshots |
| `llm/` | the model port, the Anthropic adapter, the scripted fake, prompts |
| `availability/` | availability port, chain with breaker and single flight, policy layer, adapters (Beds24, apaleo, Mews, iCal, manual, none), booking links, inquiries, the scripted PMS fake |
| `channels/whatsapp`, `channels/web`, `channels/voice` | webhook ingress and worker pipeline, outbox and statuses, the widget API and static files, the voice engine with Retell and Twilio adapters |
| `conversations/`, `handoff/`, `reservations/`, `journeys/`, `templates/`, `registration/` | guest and conversation records, staff handoffs with timers, reservation intake and the signed push API, the journey engine, the template registry, the § 29 BMG module |
| `notify/` | e-mail port (Resend, SMTP, fake), outbox, staff alerts, operator alerts |
| `metering/`, `gdpr/`, `reports/` | usage events and price tables, retention and data-subject tooling, analytics, QA queue, redaction, PDF and SVG helpers |
| `admin/` | hotel admin and operator console (Jinja2, htmx, CSP without inline scripts) |
| `jobs/` | the job table and the in-process worker (lease, retry, dead letter) |
| `eval/` | golden cases and the live evaluation |
| `demo/` | seed data, mock Graph server, WhatsApp and voice simulators, demo pages |
| `tools/` | `check` (static gates), `doctor`, `livechecks`, `setup`, `backup` |

Core logic depends on ports only (`LLMPort`, `WhatsAppPort`, `AvailabilityPort`,
`EmailPort`, `VoiceSocket`, `PaymentProofPort`, `Clock`). Only adapter modules import a
vendor SDK or `httpx2`; `tests/unit/test_repo_rules.py` enforces the boundary with an
`ast` walk. Every port ships a real adapter and a fake.

## Request flows

**Inbound WhatsApp.** `POST /wa/webhook` reads the raw body, verifies the Meta
signature against the cached app secrets, stores one `webhook_event` per message or
status together with its `wa_inbound` job in one transaction, and answers 200. The
worker leases the job, resolves the tenant, builds an `Inbound`, extends the 24-hour
window, runs the Brain under the conversation lock, writes outbox rows and sends
through the Graph client. Status webhooks update message state and write priced
usage events.

**The Brain.** Guardrails run before the model (emergencies, STOP, requests for a
person, other guests, card redaction, injection markers, complaint cues, special
categories). With a model configured and `ai_mode: full`, the prompt is assembled as
tools sorted by name, four system blocks (core rules, tenant block, public KB
snapshot, channel profile with the cache breakpoint) and text-only history plus a
context envelope; the tool loop runs with caps; the JSON reply is validated (facts,
forbidden binding words, human claims, canary, script, length) with one regeneration;
failures degrade to the responder or a handoff. Post-processing applies handoff
triggers (low confidence, complaint cue, same question, three unresolved turns,
off-topic redirects) and upsell rules.

**Widget.** `POST /w/api/session` opens a conversation and returns the disclosure;
`POST /w/api/message` returns the whole validated reply; `GET /w/api/updates` is
polled while staff handle the conversation.

**Voice.** Retell's custom-LLM WebSocket or Twilio ConversationRelay speak the
disclosure first, run the Brain with the voice profile (two sentences, no lists or
URLs), transfer during staffed hours or capture a callback, and end with a
transcript, a German summary, a callback task and a usage event.

**Scheduled work** runs through the `job` table: journey steps, SLA timers, ETA
silence, retention, metering reconciliation, the weekly digest, template sync,
backups reminders, voice reconciliation. Dedupe keys make every schedule idempotent.

## Tenancy and data

Shared schema; `tenant_id` on every business table; composite unique keys;
resolution never trusts the client (WhatsApp by `phone_number_id`, widget by public
key plus origin, voice by agent or number, admin by session row). Guest name, phone,
e-mail and Meldeschein fields are encrypted with the tenant's AAD and indexed by a
keyed HMAC; message text lives in its own retention-bound table. SQLite serves the
demo (WAL, `foreign_keys=ON`, `busy_timeout`), PostgreSQL 18 production, one migration
chain for both.

## Quality gates

`concierge check` runs ruff, format, mypy strict, bandit, pip check, the secret scan,
the import boundary, the C1 allow-list, the forbidden-marker scan, env parity, the
raw-SQL rule and pip-audit. `pytest` runs unit, integration, golden (104 cases),
property, e2e and failure-injection suites offline; `-m load` runs the load smoke;
`-m live` needs keys and is never a gate.
