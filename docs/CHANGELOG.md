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
- Knowledge-base intake: the 85-item questionnaire (admin page, YAML in the pack,
  draft entries), page import through the SSRF guard, text, Markdown and PDF
  import, machine-translation drafts, coverage report.
- Doctor (offline and live), setup wizard, demo with mock Graph and simulators,
  deployment files (systemd, Docker, Compose, Caddy), documentation.

Not built: the (S) items Smoobu, ibelsa, `openai_compat` voice, TOTP, HubSpot, Stripe
usage export, SMS or call for P0 (reasons in `docs/BUILD_STATUS.md`).
