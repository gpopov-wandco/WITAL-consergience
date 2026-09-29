# Customisation per client

## The client pack

A pack is a directory or zip with `tenant.yaml` (Appendix C schema), `kb/entries.yaml`,
`templates/`, `legal/`, `journeys.yaml`, `availability.yaml`, `branding/` and
`questionnaire_answers.yaml`; never secrets (the validator scans for them).

```
concierge pack export --tenant hotel-beispiel --out packs/hotel-beispiel
concierge pack validate packs/hotel-beispiel
concierge pack diff packs/hotel-beispiel          # against the stored tenant
concierge pack import packs/hotel-beispiel        # dry run first, writes drafts, one audit row
```

`examples/hotel-beispiel` is the fixture tenant; `examples/villa-westend` shows a pack
with open questions (`needs_input`).

## The questionnaire

Eighty-five items in groups A to I (business and contacts, channels, PMS and
availability, times and arrival, policies and money, facilities, guest information,
handoff and staff, brand and legal). The hotel answers them in the admin
(Knowledge base, questionnaire) or as `questionnaire_answers.yaml`; each answer lands
as a draft KB entry that the owner approves.

## Knowledge base

Entries have a key (block and sub-key, for example `parking` or `directions.airport`),
a language, a structured value, a text, validity dates, a source, a truth level
(`authoritative_pms`, `authoritative_kb`, `indicative`), a status (`draft`,
`pending_approval`, `approved`, `retired`), a sensitivity (`public`,
`verified_guest_only`, `staff_only`) and a review date (default 180 days). Only
approved entries reach guests; volatile entries past their review date are ignored
and trigger a reminder; machine translations stay `draft_mt` and fall back to English
with a note unless the tenant allows them. The go-live keys are listed by
`concierge doctor` (`kb.coverage`).

Imports: `concierge kb import-url <url>` proposes drafts from public pages; text,
Markdown and PDF import; machine-translation drafts. Everything imported is untrusted
data and lands as a draft.

## Languages and tone

`languages` in the profile (German and English mandatory); `default_language` for
first contact; the `tone` and `refusals` KB entries (staff-only) shape the persona and
the refusal wording; guest texts per language live in `agent/texts/*.yaml`.

## Branding

Widget colour, logo text, position, greeting and language menu in `widget` and
`branding`; the admin shows the hotel name; the handout and PDFs use the WITALCO brand.

## Escalation and staffed hours

`staffed_hours`, `handoff.contacts` (WhatsApp numbers and e-mail addresses),
`handoff.sla` (P1 and P2 minutes), `handoff.on_call` for P0 outside staffed hours,
quiet hours for journeys.

## WhatsApp number and templates

One number per tenant; `whatsapp.waba_id` and `phone_number_id` in the profile, the
three secrets through `concierge secrets set`. `concierge whatsapp templates lint`
checks the standard set; `sync` creates the missing templates at Meta and tracks
their status; sending refuses templates that are not approved in the language.

## Availability sources

`availability.chain` in order, for example `[beds24, ical, manual, none]`; per source
the fields and secrets of INTEGRATIONS.md; `booking_url` with the placeholders
`{arrival} {departure} {adults} {children} {lang} {room}`; `allow_pms_write` only for
Beds24 and Mews inquiries.

## Go-live checklist (activation gates)

`concierge tenant activate <slug>` refuses until: every legal text is approved; the
DPA-signed flag and the sub-processor acknowledgement are recorded
(`concierge tenant record-dpa`); the core KB keys are approved in every tenant
language; the WhatsApp number is mapped uniquely; `concierge doctor --live` recorded a
passing run; the templates are approved in the tenant's languages.

## Pricing and metering

`pricing` in the profile names the price table (`default_de`), the model prices and
the USD-EUR row; the operator console edits dated rows. Included voice minutes and
the overage rate live in `voice`. Before every price change re-check Meta's rate
card; the doctor prints the URL and the last edit date.
