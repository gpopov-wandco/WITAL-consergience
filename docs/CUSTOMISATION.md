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

Eighty-five items in groups A to I: A business and contacts (10), B channels (8),
C PMS and availability (8), D times and arrival (10), E policies and money (12),
F facilities (14), G guest information (8), H handoff and staff (7), I brand, legal
and consent (8). Every item has a key (`a_hotel_name`, `f_breakfast_price`), a kind
(text, phone, e-mail, URL, time, yes/no, choice, number, amount, list), the
knowledge-base key it feeds or the profile field it informs, a required flag and
German and English labels; `concierge kb questionnaire items --lang de` prints the
list. The hotel answers in the admin (Questionnaire) or in
`questionnaire_answers.yaml` inside the pack; amounts are written as `16.00`, lists
one entry per line, phone numbers in international form. Answers are validated with
field paths (`answers.a_phone: expected an international number`), stored per tenant,
exported and imported with the pack, and turned into one draft knowledge-base entry
per target key (`concierge kb questionnaire apply --tenant X` or the button in the
admin). Nothing reaches a guest before the owner approves the draft. Credentials never
belong in an answer; the PMS item asks for the property id only.

```
concierge kb questionnaire export --tenant hotel-beispiel --out answers.yaml
concierge kb questionnaire import answers.yaml --tenant hotel-beispiel
concierge kb questionnaire apply --tenant hotel-beispiel
```

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

### Intake

`concierge kb import-url <url> --tenant X` fetches the page and the same-site pages it
links to (five pages of one megabyte at most) through the SSRF guard, splits them into
sections, classifies each section by the words in its heading and first lines
(`kb/intake_keywords.yaml`: parking, breakfast, check-in, cancellation and so on) and
writes one draft per block with the source `url:<host>`; unclassified text is dropped
and reported. `concierge kb import-file <path> --tenant X` does the same for text,
Markdown and PDF files (`pypdf`, first 50 pages); `--key parking` stores the whole
file under one key. `--dry-run` prints the proposals without writing. Imported text is
untrusted data: it is classified, never interpreted, kept verbatim for the owner to
edit, and stays a draft that no prompt and no guest sees.

`concierge kb translate --tenant X --to ru,uk` sends the approved entries of the
default language to the configured model with a fixed translation instruction and
stores the replies as `draft_mt` (source `mt:<model>`); without a model the command
stops with the variables to set. Machine translations fall back to English until the
owner approves them (or the profile allows `machine_translation_allowed`).
`concierge kb coverage --tenant X` prints the go-live keys per language (approved,
pending, draft, missing) and exits with 1 while a key lacks approval.

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
