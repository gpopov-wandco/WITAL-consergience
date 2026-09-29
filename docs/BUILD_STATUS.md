# Build status

Living checklist per 1.2 of `docs/BUILD_PROMPT.md`. Statuses: not started, in progress,
built, tested, verified. Evidence pointers name a test path, a command or a document.
Updated after each unit of work.

## Milestones

| # | Milestone | Status | Evidence |
|---|---|---|---|
| M0 | Intake defaults, prompt saved, repo, status and decisions files | done | `docs/BUILD_PROMPT.md` (sha256 matches upload), `docs/DECISIONS.md` D-001, D-002 |
| M1 | `docs/PLAN.md` | done | `docs/PLAN.md` |
| M2 | Build-time research and dependency install | done | `docs/VERIFIED_FACTS.md`, `requirements.lock`, D-005 |
| M3 | Scaffold, settings, DB, migrations, CLI, `concierge check`, security foundation | done | `pyproject.toml`, `requirements.lock` (hashes), `src/concierge/{settings,clock,ids,log,cli,context}.py`, `db/`, `security/`, `tenants/`; `concierge check` 12 gates pass; `tests/unit/test_security.py`, `tests/integration/test_db.py`, `tests/integration/test_tenants.py`, `tests/unit/test_cli.py` |
| M4 | Core: KB, LLM port, Brain, tools, guardrails, validators, F04, prompts | done | `src/concierge/{agent,kb,llm,conversations,handoff}`; `tests/integration/test_brain.py` (15 orchestration scenarios with `ScriptedLLM`), `tests/integration/test_responder_conversations.py` (15 intents x 6 languages), `tests/unit/test_tools_validators.py`, `tests/unit/test_guardrails_languages.py`, `tests/integration/test_kb.py`; mypy strict on `agent` |
| M5 | Channels: WhatsApp, widget, wa-sim, mock Graph | done | `src/concierge/channels/whatsapp/` (webhook parsing, Graph client, outbox, inbound pipeline, ingress), `channels/web/` (widget API and the three static widget files), `jobs/`, `demo/` (seed, mock Graph, simulator, pages), `runtime.py`, `concierge serve` and `concierge demo`; `tests/integration/test_whatsapp_pipeline.py`, `tests/integration/test_jobs.py`, `tests/integration/test_widget.py`, `tests/integration/test_demo.py`, `tests/unit/test_whatsapp_parsing.py`, `tests/unit/test_graph_client.py` |
| M6 | Availability, adapters, policy, booking handoff, reservation intake | done | `src/concierge/availability/` (models, port, chain, policy, manual, ical, none, beds24, apaleo, mews, vendor_http, factory, booking, backends), `src/concierge/reservations/` (service, signed push API), `handoff/requests.py`; `tests/unit/test_availability_policy.py` (rules 1-7, chain, breaker, single-flight, cache, budget, high demand), `tests/unit/test_pms_adapters.py` (contract tests incl. rule 8), `tests/unit/test_booking.py`, `tests/integration/test_availability_sources.py`, `tests/integration/test_reservations.py`, `tests/integration/test_tool_backends.py`; mypy strict on `availability` and `reservations` |
| M7 | Journeys, templates, registration, upsells | done | `src/concierge/journeys/`, `templates/` (standard set in six languages, lint, sync), `registration/` (hosted form, FakeProof, Stripe adapter, export, purge), `agent/upsells.py`, `notify/port.py` (e-mail port and fake); CLI `whatsapp templates lint|list|sync`, `journeys preview|test-send`, `registration export`, `demo advance --days N`; `tests/unit/test_templates.py`, `tests/integration/test_journeys.py` (DST 2026-10-25 and 2027-03-28, quiet hours, caps, opt-out, cancel, reschedule, six-language end-to-end journey, e-mail fallback, sync and statuses), `tests/integration/test_registration.py`, `tests/integration/test_upsells.py` |
| M8 | Handoff, inbox, notifications, admin UI, operator console | not started | |
| M9 | Voice module and simulators | not started | |
| M10 | Metering, GDPR, analytics, backup, observability | not started | |
| M11 | Evaluation suites | not started | |
| M12 | Docs, deployment, doctor, wizard, demo, cold start | not started | |
| M13 | Handout and compliance pack | not started | |
| M14 | (S) items | not started | |
| M15 | Final audit | not started | |

## Gates

| Gate | Status | Evidence |
|---|---|---|
| G1 offline pytest and browser suite | not started | |
| G2 static gates | not started | |
| G3 coverage | not started | |
| G4 cold start | not started | |
| G5 doctor offline and live-with-fake-keys | not started | |
| G6 golden counts, adversarial, load smoke | not started | |
| G7 traceability | not started | |
| G8 handout | not started | |
| G9 no TODO, FIXME, unexplained skip | not started | |
| G10 docs complete | not started | |
| G11 every feature verified with evidence | not started | |
| G12 VERIFIED_FACTS covers every VERIFY item code depends on | in progress | `docs/VERIFIED_FACTS.md` |
| G13 (S) items built or listed | not started | |
| G14 self-review found no medium or higher defect | not started | |
| G15 no secrets; `.env.example` matches Appendix A both ways | not started | |

## Features

| Id | Feature | Status | Evidence |
|---|---|---|---|
| F01 | Conversation engine | tested | `agent/brain.py`; `tests/integration/test_brain.py` (caps, tool packing, cache-prefix stability, failure injection, timeout, breaker, budget, concurrency) |
| F02 | Agent tools | tested | `agent/tools.py`, `availability/backends.py` (real backends re-authorised from the session); `tests/unit/test_tools_validators.py` (schema contract, cross-tenant, bad-input matrix), `tests/integration/test_tool_backends.py` (availability, verify then ETA, booking link, request, callback through the Brain) |
| F03 | Guardrails | tested | `agent/guardrails.py`, `agent/keywords.yaml`; `tests/unit/test_guardrails_languages.py` |
| F04 | Deterministic responder | tested | `agent/responder.py`, `agent/intents.yaml`; 15 intents x 6 languages in `tests/integration/test_responder_conversations.py` |
| F05 | Knowledge base | tested | `kb/store.py`, `kb/blocks.py`, `kb/fixtures/hotel_beispiel.yaml`; `tests/integration/test_kb.py` |
| F06 | KB intake | not started | |
| F07 | Availability core | tested | `availability/{models,port,chain,policy}.py`; `tests/unit/test_availability_policy.py` (one test per policy rule, chain, breaker 3 failures/60 s, single-flight, cache validity and expiry, budget, high-demand dates) |
| F08 | PMS adapters | tested (endpoints VERIFY) | `availability/{beds24,apaleo,mews,vendor_http}.py`; `tests/unit/test_pms_adapters.py` (availability, restriction violation, 401 with token refresh, 429 Retry-After, timeout, malformed body, credit budget, write gates, Mews UTC time units); endpoints implemented without vendor docs, see D-011 and `docs/VERIFIED_FACTS.md`; Smoobu and ibelsa (S) not built |
| F09 | Fallback sources | tested | `availability/{ical,manual,none}.py` (iCal edge cases, conditional GET, SSRF guard; manual grid with audited bulk set, CSV import with German formats and dry run, staleness; no-live-data handoff with response window); `tests/integration/test_availability_sources.py` |
| F10 | Booking handoff | tested | `availability/booking.py` (URL template with escaping, Mews deep link, inquiry with consent, card refusal, idempotency, P2 handoff, PMS write gate: Beds24 and Mews only with `allow_pms_write`, apaleo never); `tests/unit/test_booking.py`, `tests/integration/test_availability_sources.py`; the inquiry e-mail to the hotel is F18 (M8) |
| F11 | Arrival practicalities | in progress | responder intents and KB (M4), `create_request`, `set_eta` (verified conversations only), ETA windows from the T-2 template buttons, stay-check button replies (problem is P1), callbacks in `handoff/requests.py`; not built yet: key-safe code at T-0 12:00 and the ETA-plus-2-hours silence alert (listed under open items), golden scenarios per intent in six languages (M11) |
| F12 | Reservation intake | tested | `reservations/service.py` (upsert by res_id, E.164, encrypted fields with blind indexes, opt-in only with evidence, reschedule cancels journey jobs, cancellation, CSV, PMS sync), `reservations/routes.py` (HMAC-SHA256 `X-Signature`, 5-minute window); `tests/integration/test_reservations.py` (signature, replay, idempotency, reschedule, cancel) |
| F13 | Journey engine | tested | `journeys/service.py` (planning with DST-correct local times, quiet hours, caps per phase D-014, send-time gates: cancelled, opt-in evidence, marketing opt-in, suppression, paused, template approval with e-mail fallback, open complaint), replies to journey buttons; `tests/integration/test_journeys.py` (DST days, quiet hours, caps, opt-out via STOP, cancel, reschedule, six-language journey with exact templates, variables and local times) |
| F14 | WhatsApp templates | tested (payload shapes VERIFY) | `templates/standard.yaml` (9 templates x 6 languages), `templates/service.py` (lint table, install, sync with the 100-per-hour budget, status tracking, approval gate); `tests/unit/test_templates.py`, `tests/integration/test_journeys.py::test_registry_sync_statuses_and_budget`; Meta category changes are recorded and alerted by the inbound pipeline |
| F15 | Registration module | tested (PSP mapping VERIFY) | `registration/{service,routes,proof,stripe_proof}.py`, `registration/texts.yaml` (draft_unreviewed, six languages), hosted form under `/r/{token}`; `tests/integration/test_registration.py` (closed field set, signed link, arrival-day gate with the frozen clock, deletion after 380 days with a deletion log, export, hosted form flow, Stripe adapter and signed webhook against MockTransport) |
| F16 | Honest upsells | tested | `agent/upsells.py` (KB `upsells.*` only, relevant topic, once per conversation, not in complaint or safety threads or after a no, explicit confirmation records a staff request); `tests/integration/test_upsells.py` (rule table, golden cases) |
| F17 | Handoff and staff inbox | in progress | `handoff/service.py` (create, acknowledge, resolve, SLA timers), `handoff/requests.py` (guest requests, ETA, callbacks); inbox UI and SLA jobs pending (M8) |
| F18 | E-mail and notifications | in progress | `notify/port.py` (`EmailPort`, `FakeEmail`), journey e-mail fallback; operator alerts persisted (`OperatorAlert`); Resend and SMTP adapters, the e-mail outbox and staff alerts pending (M8) |
| F19 | Hotel admin UI | not started | |
| F20 | Operator console | not started | |
| F21 | Tenant config and client packs | in progress | `examples/hotel-beispiel/tenant.yaml`, `examples/villa-westend/tenant.yaml` (needs_input skeleton); pack CLI not started |
| F22 | Widget and branding | tested | `channels/web/`, `static/widget/{loader.js,gc-widget.js,gc-widget.css}` (279 lines together); `tests/integration/test_widget.py` (CORS matrix, sessions, limits, inert loader, hashed session key) |
| F23 | Multilingual behaviour | tested | `agent/languages.py`, `agent/texts/*.yaml`; `tests/unit/test_guardrails_languages.py` |
| F24 | Metering and cost | not started | |
| F25 | GDPR tooling | not started | |
| F26 | Voice fallback | not started | |
| F27 | Analytics and QA | not started | |
| F28 | Doctor and setup wizard | not started | |
| F29 | Demo and simulator | tested | `demo/{seed,mock_graph,wa_sim,routes}.py`, `concierge demo`; `tests/integration/test_demo.py` (seed idempotent and refused in production, simulator through the real Graph client into the mock server, error injection, pages) |
| F30 | Backup and restore | not started | |
| F31 | Security foundation | in progress | `security/` (envelope encryption, key rotation, blind index, signatures, SSRF-safe fetcher, limiter, passwords, sessions, links) with `tests/unit/test_security.py`; security headers and CSP in `app.py`; admin auth pending (M8) |
| F32 | Observability | in progress | structured redacted logs (`log.py`), `/healthz`, `/readyz` (db, migration head, worker heartbeat, master key); metrics endpoint pending (M10) |
| F33 | Deployment and docs | not started | |
| F34 | Handout and compliance pack | not started | |
| F35 | Evaluation harness | not started | |
| F36 | (S) Optional sinks | not started | |

## Open defects

None recorded yet.

## Open items carried forward

| Item | Feature | Planned in |
|---|---|---|
| Key-safe code message at T-0 12:00 for verified conversations | F11 | M8 (needs the staff inbox to expose the code and a verified conversation) |
| Silence at ETA plus 2 hours alerts staff | F11 | M8 (staff alert delivery, `gc_staff_alert`) |
| Inquiry e-mail to the hotel with reply-to the guest | F10, F18 | M8 |

## Blockers

| Item | What was tried | State |
|---|---|---|
| Vendor documentation for Beds24, apaleo, Mews, Retell, Twilio, Meta, gesetze-im-internet.de | direct fetch through the environment proxy (HTTP 403 CONNECT) | parked: implemented from the prompt, flagged VERIFY, checked by `doctor --live` (D-005) |
| Windows and Python 3.14.7 | not available in the build environment | parked: code follows the Windows rules; recorded as not verified (D-003, D-004) |
| Docker daemon | `docker info` fails | parked: Docker files statically reviewed (D-001 Q5) |

## VERIFY items

See `docs/VERIFIED_FACTS.md`; every open item names its expected source and the runtime
check (`doctor --live`) or the human step (handout) that covers it.

## Decisions

See `docs/DECISIONS.md`.

## Skipped or unrun tests

None yet. Every skip carries a written reason in the test and is listed here.
