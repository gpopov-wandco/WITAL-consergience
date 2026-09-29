# Compliance as software requirements

Not legal advice. This document maps legal requirements to software behaviour and
lists what a lawyer must review before go-live. Every shipped legal text is
`draft_unreviewed` until a named person approves it in the admin with a date.

| Requirement | Software behaviour | Where |
|---|---|---|
| AI Act Art. 50(1) transparency | disclosure inserted by code on first contact on every channel (WhatsApp text, widget label and message, spoken voice notice), stored as an event; the assistant never claims to be human (validator) | `agent/brain.py`, `channels/web`, `channels/voice/session.py` |
| GDPR Art. 13 information | the first message and the widget link to the hotel's privacy notice; the notice template names the AI provider category, WhatsApp, US transfers, retention and rights | `legal/drafts.yaml`, widget footer |
| Art. 28 processor terms | hotel is controller, WITALCO processor; `concierge compliance pack` lists the sub-processors with locations; activation requires the DPA-signed flag and the sub-processor acknowledgement | `tenants/service.py` (activation gate), `tools/compliance` |
| Art. 6, 7 basis and consent | consent rows with text version, time, channel and source; suppression list survives content deletion | `gdpr/service.py`, `db/models.py` |
| UWG § 7, Meta opt-in | journey messages need recorded opt-in with proof; marketing needs its own opt-in; sender identity and stop wording in every business-initiated message; utility and marketing templates are linted apart | `journeys/service.py`, `templates/` |
| TDDDG § 25 | the widget is inert until the launcher is clicked: no cookie, no storage, no request beyond the script | `static/widget/loader.js` |
| BMG §§ 29, 30, 54 | registration module off by default; closed field set; electronic confirmation on the arrival day through the payment-instrument route; separate encrypted table; hard delete after 365 to 455 days | `registration/` |
| Art. 22, Art. 9 | no automated decision with legal effect (refusals go to staff); special-category data is never asked, is flagged when volunteered, excluded from analytics and scrubbed after the stay | `agent/guardrails.py`, `gdpr/retention.py` |
| Arts. 15, 17, 18 | search by blind index, JSON and PDF export, erasure keeping suppression and consent proof, restriction flag, request queue with 30-day due dates and sub-processor tasks | `gdpr/service.py`, admin data requests |
| BFSG, WCAG 2.2 AA | widget and admin built to the checklist in ACCESSIBILITY_TEST.md; accessibility statement template in the legal drafts | `static/widget`, `admin/templates` |
| StGB § 201 | no audio recording; the voice notice says so; callers who decline get a callback without a transcript | `channels/voice` |
| Liability for answers | never confirms bookings or prices (validators, policy wording), cancellation text quoted as written, stale data not quoted, facts approved before go-live | `agent/validators.py`, `availability/policy.py`, `kb/` |
| Third-country transfer | `inference_geo` is set explicitly and logged per request; `ai_mode: faq_only` for hotels that decline US processing; Retell only with the transfer acknowledgement | `llm/anthropic_adapter.py`, `tenants/profile.py` |
| Meta automation duty | escalation offered in chat; phone and e-mail shown in the widget and the KB | `agent/`, widget footer |

## Texts a lawyer must approve before go-live

1. Processor agreement (AVV) and the TOM annex.
2. The privacy-notice paragraph for the hotel's website.
3. AI disclosures per channel and language.
4. Widget wording and the cookie or storage statement.
5. Opt-in and opt-out wording for WhatsApp (utility and marketing).
6. The templates and their categories.
7. Registration form and its consent wording (when F15 is on).
8. Chat terms of use.
9. Cancellation policy and house rules as quoted by the assistant.
10. Emergency and complaint scripts.
11. The voice notice.
12. The accessibility statement.
13. WITALCO's terms for the hotel.
14. DPIA and transfer impact assessment.
15. Impressum lines shown by the assistant (DDG § 5).

`concierge compliance pack --tenant X` collects the drafts, the sub-processor list,
the retention schedule, the TOM checklist and a record-of-processing draft, every
page stamped "Draft for lawyer review".

## Retention defaults (lawyer-review proposals)

Chat texts 30 days after the last message; booking-linked personal data 30 days after
departure; consent and suppression proof for the life of the consent plus the
limitation period (three years from year end: VERIFY); metering 24 months; voice
transcripts 30 days; special-category texts seven days after the stay; Meldeschein
per BMG. All values live in the tenant profile.

## Open VERIFY items with legal weight

See `docs/VERIFIED_FACTS.md`: registration for German citizens after 2025-01-01, the
assignment-number interpretation of § 29 Abs. 5 BMG, Resend's data region, Retell's
erasure route, the DPO requirement, city-tax rates per municipality.
