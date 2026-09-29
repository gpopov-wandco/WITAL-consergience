# Changelog

## 0.1.0 (2026-09-29) — first testable iteration

Built in one continuous run from `docs/BUILD_PROMPT.md`; milestones and evidence in
`docs/BUILD_STATUS.md`, decisions in `docs/DECISIONS.md`.

- Conversation engine with guardrails, tools, validators, deterministic responder,
  six languages, upsell rules, handoff triggers.
- WhatsApp Cloud API channel (webhook ingress, worker, outbox, statuses, media,
  templates, metering from status webhooks), website widget with staff-reply
  polling, voice fallback (Retell, Twilio ConversationRelay) with simulators.
- Availability chain with Beds24, apaleo, Mews, iCal, manual and no-data adapters,
  policy wording, booking links, inquiries, reservation intake and signed push API.
- Journeys with DST-correct scheduling, quiet hours, caps, opt-in gates, e-mail
  fallback; template registry and lint; registration module (§ 29 BMG) with a fake
  and a Stripe proof adapter.
- Staff inbox with SLA timers and escalation, notifications, hotel admin, operator
  console, client packs, legal drafts with an approval gate.
- Metering with dated price tables and corrections, GDPR retention, export, erasure
  and restriction, analytics and digest, QA queue, encrypted backups and restore
  test, metrics, operator alerts.
- Evaluation: 104 golden cases, property tests, failure-injection map, load smoke,
  end-to-end conversations per language, live evaluation command.
- Self-review (docs/REVIEW_FINDINGS.md): 24 findings, every high and medium one
  fixed with a regression test (STOP while staff handle a case, per-guest message
  limit, voice socket secret and transcript redaction, Stripe proof wiring, shared
  Meta app secrets, retried inbound jobs, history order, billable allowance, model
  price rows, erasure of webhook payloads, rescheduling after failures, staff
  templates under STOP and quiet hours, guest text as a fact source, persisted PMS
  verification) plus the low ones that were cheap (open redirect, local midnight,
  counters on resolve, retention without a phone, phone numbers versus cards,
  primed daily budget, hotel contacts as facts).
- Knowledge-base intake: the 85-item questionnaire (admin page, YAML in the pack,
  draft entries), page import through the SSRF guard, text, Markdown and PDF
  import, machine-translation drafts, coverage report.
- Doctor (offline and live), setup wizard, demo with mock Graph and simulators,
  deployment files (systemd, Docker, Compose, Caddy), documentation.
- Browser suite (Playwright, Chromium) for the widget and the admin; it found and
  led to fixes for a panel that stayed visible after close, a launcher that vanished
  after the first open and late focus. Rows created by the services carry the
  injected clock (D-035); the simulator stamps payloads with it and the 24-hour
  window never opens later than the service clock. A job without a registered
  handler is marked dead in the table, not only reported. Coverage is measured
  across greenlet switches (D-036).

Not built: the (S) items Smoobu, ibelsa, `openai_compat` voice, TOTP, HubSpot, Stripe
usage export, SMS or call for P0 (reasons in `docs/BUILD_STATUS.md`).
