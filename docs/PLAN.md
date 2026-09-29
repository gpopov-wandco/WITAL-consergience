# Plan

One self-contained Python service, `concierge`, that answers hotel guests on WhatsApp,
a website widget and a phone line, in six languages, from approved hotel facts and a
real availability layer, with staff handoff, guest journeys, metering and the tooling
to onboard and hand over a hotel. Everything runs and is tested offline in demo mode.

## Architecture

- **Process model.** One FastAPI/uvicorn process serves HTTP (webhooks, widget API,
  admin UI, health) and runs one job worker loop (`job` table, leases, dedupe keys).
  In-memory limiters and circuit breakers are valid because there is one process.
- **Core versus adapters.** `src/concierge` core modules depend only on ports
  (`LLMPort`, `WhatsAppPort`, `AvailabilityPort`, `EmailPort`, `VoiceProvider`,
  `PaymentProofPort`, `CRMPort`, `Clock`). Only adapter modules import `anthropic`,
  `httpx2` or vendor code; an AST test enforces the boundary. Every port has a real
  adapter and a fake.
- **Brain.** `Brain.respond(tenant, conversation, inbound)` is channel agnostic:
  guardrails (F03) → prompt assembly (8.2) → tool loop with caps → validation (8.4)
  → side effects (handoff, outbox, usage). Degraded mode routes to the deterministic
  responder (F04) which shares tools and grounding.
- **Tenancy.** Shared schema, `tenant_id` on every business table, composite unique
  keys, `TenantSession` with `with_loader_criteria` and a `before_flush` guard.
  Tenant resolution never trusts the client (phone_number_id, public key + Origin,
  agent id, admin session).
- **Data.** SQLAlchemy 2.1 async with Alembic; SQLite (WAL) for demo, Postgres 18 for
  production, one migration chain. PII columns are AES-256-GCM encrypted with tenant
  AAD plus keyed-HMAC blind indexes; message text lives in its own retention-bound
  table; usage, audit and deletion logs are append-only.
- **Configuration.** Environment (Appendix A, pydantic-settings) plus per-tenant
  profile (Appendix C, pydantic, stored in the DB, exportable as a client pack).
  No hotel fact, price, model id, template text or threshold is a code constant.

## Modules (`src/concierge/`)

`settings`, `clock`, `ids`, `log`, `cli`; `security/` (crypto, signatures, ssrf,
limiter, csrf, sessions); `db/` (models, session, migrations); `tenants/` (profile,
packs, activation); `kb/` (store, resolver, intake, questionnaire); `llm/` (port,
anthropic adapter, scripted fake, prompts); `agent/` (brain, tools, guardrails,
validators, responder, languages); `availability/` (port, chain, policy, ical,
manual, none, beds24, apaleo, mews, booking); `reservations/`; `journeys/`;
`templates/`; `registration/`; `handoff/`; `notify/`; `channels/{whatsapp,web,voice}`;
`jobs/`; `metering/`; `gdpr/`; `admin/` (routes, templates, static); `widget/`;
`reports/`; `sinks/`; `demo/`; `tools/` (trace, handout, doctor, check).

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Vendor docs unreachable from the build environment | adapters follow the prompt's traps, fixtures are synthetic, `doctor --live` verifies at runtime, every such fact is VERIFY |
| Model behaviour cannot be tested offline | `ScriptedLLM` drives orchestration tests; golden sets assert tool calls, flags and canary, never prose |
| Tenant data leakage | isolation guard, composite keys, property test with interleaved tenants |
| Silent failures in messaging | outbox states, status reconciliation, dead-job alerts, every failure ends in handoff or F04 |
| Windows and Python 3.14 not available here | code follows every Windows rule; recorded as not verified |
| Scale of the specification | milestones with gates; `BUILD_STATUS.md` records evidence; nothing stubbed is called done |

## Test plan

Unit, integration (ASGI via `httpx2.ASGITransport`, temp SQLite), golden (YAML, six
languages), property (hypothesis), e2e journeys, browser (Playwright, marker
`browser`), load smoke, live (marker `live`, skipped without a key). Default `pytest`
is offline and blocks sockets. Every test carries `@pytest.mark.feature("Fxx")`; every
module docstring carries `Implements: Fxx`; `python -m concierge.tools.trace` builds
`docs/TRACEABILITY.md`. Static gates run through `concierge check`.

## Feature to milestone map

| Milestone | Features |
|---|---|
| M3 scaffold | F31 (security), settings, DB, CLI skeleton, `concierge check` |
| M4 core | F01, F02, F03, F04, F05, F23, prompts, LLM port and fakes |
| M5 channels | WhatsApp webhook/worker/outbox, F22 widget, F29 wa-sim and mock Graph |
| M6 availability | F07, F08, F09, F10, F12 |
| M7 journeys | F13, F14, F15, F16 |
| M8 staff | F17, F18, F19, F20, F21 |
| M9 voice | F26 |
| M10 operations | F24, F25, F27, F30, F32 |
| M11 evaluation | F35 golden sets, adversarial, property, load, failure injection |
| M12 docs and deploy | F28, F29 demo polish, F33, cold start |
| M13 handout | F34 |
| M14 stretch | F36, Smoobu, ibelsa, `openai_compat`, TOTP, SMS/call for P0 |
| M15 audit | traceability, self-review, completion audit, final report |
