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
| M4 | Core: KB, LLM port, Brain, tools, guardrails, validators, F04, prompts | not started | |
| M5 | Channels: WhatsApp, widget, wa-sim, mock Graph | not started | |
| M6 | Availability, adapters, policy, booking handoff, reservation intake | not started | |
| M7 | Journeys, templates, registration, upsells | not started | |
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
| F01 | Conversation engine | not started | |
| F02 | Agent tools | not started | |
| F03 | Guardrails | not started | |
| F04 | Deterministic responder | not started | |
| F05 | Knowledge base | not started | |
| F06 | KB intake | not started | |
| F07 | Availability core | not started | |
| F08 | PMS adapters | not started | |
| F09 | Fallback sources | not started | |
| F10 | Booking handoff | not started | |
| F11 | Arrival practicalities | not started | |
| F12 | Reservation intake | not started | |
| F13 | Journey engine | not started | |
| F14 | WhatsApp templates | not started | |
| F15 | Registration module | not started | |
| F16 | Honest upsells | not started | |
| F17 | Handoff and staff inbox | not started | |
| F18 | E-mail and notifications | not started | |
| F19 | Hotel admin UI | not started | |
| F20 | Operator console | not started | |
| F21 | Tenant config and client packs | not started | |
| F22 | Widget and branding | not started | |
| F23 | Multilingual behaviour | not started | |
| F24 | Metering and cost | not started | |
| F25 | GDPR tooling | not started | |
| F26 | Voice fallback | not started | |
| F27 | Analytics and QA | not started | |
| F28 | Doctor and setup wizard | not started | |
| F29 | Demo and simulator | not started | |
| F30 | Backup and restore | not started | |
| F31 | Security foundation | in progress | |
| F32 | Observability | not started | |
| F33 | Deployment and docs | not started | |
| F34 | Handout and compliance pack | not started | |
| F35 | Evaluation harness | not started | |
| F36 | (S) Optional sinks | not started | |

## Open defects

None recorded yet.

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
