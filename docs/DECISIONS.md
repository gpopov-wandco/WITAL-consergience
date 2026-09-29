# Decisions

Decisions taken during the build, with reasons. Newest at the bottom. Each entry
names the requirement it touches (section or feature id from `docs/BUILD_PROMPT.md`).

## D-001 Intake questions answered by defaults (1.6, Appendix B)

The build runs headless: the requester stated in the task that they cannot answer
questions or monitor the run. Per 1.6 all eight defaults are adopted:

| # | Question | Adopted |
|---|---|---|
| Q1 | Handout language | English |
| Q2 | Voice adapters | Retell complete default, Twilio ConversationRelay complete second adapter, OpenAI-compatible endpoint as (S) |
| Q3 | Villa Westend data | None available. Pilot skeleton with empty required fields (`needs_input`) plus the fictional demo hotel |
| Q4 | WhatsApp model | Hotel-owned WABA and number, operator-entered credentials (Model B), Embedded Signup deferred |
| Q5 | Deployment | venv plus systemd primary, Docker Compose plus Caddy second; Docker daemon is not available in the build environment, so Docker files are statically reviewed, not executed |
| Q6 | Brand and naming | Tokens from 2.8, text lockup, name "WITALCO Guest Concierge (Gäste-Concierge)", placeholder support contacts flagged VERIFY |
| Q7 | Live model evaluation | Not run (no key in the environment; the live suite is skipped, never faked) |
| Q8 | Repository | See D-002 |

## D-002 Build location and git (Q8, 1.7)

The build environment provides an existing, empty git repository
(`gpopov-wandco/WITAL-consergience`) and a designated branch
(`claude/kind-babbage-bbkz2a`). The project is therefore built at the repository
root instead of a new `./guest-concierge` directory, and the work is committed on
that branch. The harness instruction to push to the designated branch overrides the
prompt's "local commits only": the requester cannot otherwise retrieve the result.

## D-003 Interpreter: Python 3.13.12 (2.8, 6.1, C13)

The requester's machine has Python 3.14.7. The build environment has 3.13.12 as its
newest interpreter (uv 0.8.17 only offers a 3.14 release candidate). Per 6.1 "test the
installed interpreter only", all tests run on 3.13.12; `requires-python` is
`>=3.13,<3.15`. Every wheel in the lock file also publishes a cp314 or abi3/pure
wheel according to PyPI metadata checked on 2026-09-29, but 3.14.7 is recorded as
"not verified in this environment" in `docs/BUILD_STATUS.md`.

## D-004 Windows not available (2.8, 10.6)

No Windows machine is reachable. The code follows every Windows rule in the prompt
(no `asyncio.set_event_loop_policy`, no `loop.add_signal_handler`, `encoding="utf-8"`
on every `open`, `Path.as_posix()` database URLs, engines disposed before temp files
are deleted, `PYTHONUTF8=1` in launchers). The cold-start test (10.6) is executed on
Linux and recorded as "not verified on Windows".

## D-005 Vendor documentation reachability (section 2, M2)

Through the environment's egress proxy the following hosts are not reachable:
beds24.com, api.mews.com, api.apaleo.com, apaleo.dev, docs.retellai.com,
developers.facebook.com, twilio.com, gesetze-im-internet.de, unpkg.com. Reachable:
pypi.org, registry.npmjs.org, raw.githubusercontent.com, github.com release assets,
platform.claude.com. Consequences:

- Anthropic facts are verified from platform.claude.com and the installed SDK source.
- PMS, Meta, Retell and Twilio behaviour is implemented from the prompt's own
  descriptions (section 5, F08, F26, 6.6) and stays VERIFY in `docs/VERIFIED_FACTS.md`;
  `concierge doctor --live` performs the runtime verification against the real APIs.
- Contract-test fixtures for the PMS adapters are synthetic and follow the shapes the
  prompt describes; they are labelled `fixture` and not presented as recorded traffic.

## D-006 Fonts (2.8, C1)

Inter 4.1 static TTFs come unmodified from the rsms/inter v4.1 release
(`extras/ttf/Inter-Regular.ttf`, `Inter-Medium.ttf`, `Inter-SemiBold.ttf`,
`Inter-Bold.ttf`). Google Fonts ships Syne only as a variable font, and ReportLab
embeds only a variable font's default instance (Regular), so static instances
`Syne-Regular.ttf`, `Syne-Bold.ttf` and `Syne-ExtraBold.ttf` were generated from
`Syne[wght].ttf` with `fontTools.varLib.instancer`. Both fonts are OFL 1.1 without a
Reserved Font Name (license headers checked), so the modification is permitted. The
character-map check (Inter: Turkish, Ukrainian and Russian glyphs present; Syne:
Turkish present, Cyrillic absent) is recorded in `docs/VERIFIED_FACTS.md`; headings in
Cyrillic fall back to Inter Bold.

## D-007 htmx version (C1)

`static/vendor/htmx.min.js` is htmx 2.0.11 (the newest 2.0.x on 2026-09-29), 0BSD.
The file was downloaded from two independent sources (the npm registry tarball
`htmx.org-2.0.11.tgz` and `dist/htmx.min.js` at git tag `v2.0.11` in the htmx
repository) and both SHA-256 digests matched; the digest is recorded in
`docs/VERIFIED_FACTS.md` and checked by a test.

## D-008 Lock file tooling (C13, 6.1)

`requirements.lock` is generated with `uv pip compile --generate-hashes` from
`pyproject.toml` (uv 0.8.17 is installed in the build environment and produces a
pip-compatible, hash-locked file). pip-tools is not added as a dependency.

## D-009 Package name (C14, Appendix B)

Package and CLI are named `concierge`; `gc` would shadow the standard library module.

## D-010 Structured outputs and effort (8.1, 8.4)

The Anthropic adapter sends `output_config={"effort": ..., "format": {...}}`, never
`temperature`, `top_p`, `top_k`, `thinking` or a forced `tool_choice`, matching the
installed SDK 1.9.0 signature and the platform docs read on 2026-09-29. `inference_geo`
is sent only for models that support it (Claude 4.6 and later); the docs state that
Haiku 4.5 returns 400 when it is present, so the adapter omits it for the cheap model
and logs the effective geo from `usage.inference_geo`.

## D-011 PMS adapters built without vendor documentation (F08, M2, D-005)

The Beds24, apaleo and Mews adapters implement the endpoints and payload shapes listed
in `docs/VERIFIED_FACTS.md` (section "PMS vendors") from the build prompt's traps and
the build model's knowledge of the three APIs, because the vendor specifications were
unreachable during the build. Every adapter parses defensively (a missing or mistyped
field is a `MalformedResponse`, which the chain treats as "try the next source"),
contract tests run against synthetic fixtures, and `doctor --live` exercises the read
endpoints against the real vendor before go-live. Field-name deviations are expected to
surface there, not in a guest conversation.

## D-012 Availability cache, breakers and single-flight are in-process (F07, 6.2)

The service runs as one process (the web app and the job worker share it, D-001 Q5),
so the availability cache, the per-tenant-and-source circuit breakers and the
single-flight map live in memory. A second process would keep its own copies; this is
recorded as a scaling limit in `docs/ARCHITECTURE.md` rather than solved with an
external store the deployment does not have.

## D-013 Metering references must be unique by construction (F24, C16)

`usage_event` rows are unique per tenant and reference. A language-model call without
a provider request id (the scripted fake, or a provider that omits the header) is now
referenced by a fresh ULID instead of a clock timestamp; two calls in the same turn
produced the same timestamp under the injected clock and the insert failed, which the
tool-backend tests caught in M6.

## D-014 Journey phases and caps (F13)

The prompt caps journey messages at "3 before arrival, 1 during, 2 after" while its
default steps place three messages on or between arrival and departure (welcome, day-1
check, checkout reminder). The engine therefore counts four phases: before arrival
(booking follow-up, T-7, T-2, registration link; cap ``before``, T-7 dropped first,
then the follow-up), the arrival day (the welcome, plus the key-safe code at 12:00
local only after a late ETA in a verified conversation, D-018), during the
stay (day-1 check; cap ``during``) and departure day and later (checkout reminder,
review request; cap ``after``, the review dropped first). The registration link is a
message of its own because one message carries one topic. Job dedupe keys are
``journey:{tenant}:{res_id}:{step}:{arrival}``; a re-booked reservation revives its
cancelled keys, a done key never runs twice.

## D-015 Registration confirmation and Stripe (F15)

The electronic confirmation is a Stripe Checkout Session in payment mode with manual
capture of ``preauth_cents`` (default 100) and 3-D Secure requested; the payment method
id from ``checkout.session.completed`` is stored encrypted as the assignment number
and the PaymentIntent is cancelled to release the hold. Whether this satisfies the
"Zuordnungsnummer" requirement, the amount, the release and the account owner are
VERIFY items for the PSP and counsel (compliance pack). The demo and the tests use
``FakeProof``; nothing in the build ever calls Stripe. The BMG exemption of German
citizens since 2025-01-01 is stated as "to be confirmed by the hotel" in every
language and the texts stay ``draft_unreviewed``.

## D-016 Named template parameters and the 24-hour STOP keyword (F14, C9)

Templates use named parameters (``{{hotel}}``) with samples, sent as
``parameter_name`` body parameters; quick-reply buttons carry ``gc:<id>`` payloads so a
tap comes back as a button id. The exact creation and send payload shapes are VERIFY
against the Meta documentation (unreachable during the build) and are exercised by
``doctor --live`` and the first sync. Every template tells the guest to reply STOP, so
the Latin keyword ``stop`` was added to the Russian, Ukrainian and Turkish opt-out lists
next to the native words.

## D-017 Staff alert delivery and timers (F17, F18)

A new handoff alerts every contact with an address by e-mail at every priority; by
WhatsApp (``gc_staff_alert``) it alerts all contacts inside staffed hours and only the
on-call contacts for a P0 outside them, exactly as F17 states. Alert texts carry the
priority, the trigger summary, the local time and a link to the inbox, never the
guest's number or message. The alert outbox rows are idempotent per handoff, level
and recipient, so a restart never repeats an alert. Timers run as one job per tenant
and minute (the dedupe key carries the due minute because a finished key never runs
again): a breached SLA sets ``sla_breached`` and escalates, an unacknowledged P0
repeats every ``p0_repeat_min`` minutes to the next contact. An emergency in a
conversation someone already took over upgrades the case to P0, re-alerts and reopens
it as escalated, because "acknowledged" must not silence a safety message.

## D-018 Key-safe code and silence after the ETA (F11)

The key-safe code is sent by a journey step at ``journeys.key_safe.at`` (12:00 local)
on the arrival day, planned only once the guest announced a late arrival (the
``after_22`` window or an ETA at or after the KB's ``late_arrival.latest_reception``)
and sent only when a conversation is verified for that reservation; otherwise the step
logs ``not_verified`` and nothing goes out. The code comes from the
``verified_guest_only`` KB entry at send time and travels as a template parameter of
``gc_key_safe``, never as template text. Two hours after the announced window ends,
an ``eta_silence`` job creates a P1 for staff unless the guest wrote in the meantime;
the job shares the reservation's job prefix so STOP and cancellation remove it.

## D-019 Packs, the secret scan and Wi-Fi passwords (F21, C3)

A pack refuses any key that looks like a credential (``secret``, ``token``,
``password``, ``api_key``) in configuration files. In ``kb/entries.yaml`` the
``password`` marker is not applied, because the Wi-Fi password is guest-facing hotel
content with ``verified_guest_only`` sensitivity, not a system secret; credential-style
keys stay refused there. Pack imports write KB and legal changes as new draft versions
(the approved version keeps answering), report profile changes per top-level key, and
are idempotent: a second import of the same pack lists no changes.

## D-020 Demo accounts and in-memory e-mail (F29, C3)

The offline demo seeds three fixture accounts (owner, staff, operator) with published
demo passwords printed by ``concierge demo``; they exist only when ``GC_ENV`` is not
production and are not credentials. In demo mode without a configured provider the
e-mail port is the in-memory fake with the sender ``concierge@hotel-beispiel.invalid``,
so alerts, invites, inquiries and the journey fallback stay visible offline; production
needs ``GC_EMAIL_PROVIDER`` and ``GC_EMAIL_FROM`` or e-mail features report
"not configured" instead of failing silently.

## D-021 Voice engine rules (F26)

The disclosure is spoken by the server as ``response_id`` 0 (Retell) or the first
relay text (Twilio) and is recorded as the first outbound message before any caller
turn; the provider's own greeting is empty so no turn can start without it. The
disclosure uses the hotel's default language (the caller's language is not known
before the first utterance); replies follow the detected language. The engine runs
the Brain as a task, speaks a static filler once after ``voice.filler_ms`` and the
whole validated reply afterwards; a newer response id cancels the running task. A
request for a person transfers during staffed hours (``transfer_number`` on Retell,
``end`` with handoff data and a ``<Dial>`` on Twilio) and otherwise captures a
callback; a farewell without a question ends the call; decline phrases (per-language
table in ``channels/voice/phrases.yaml``) end the call with a callback and no
transcript. A handoff created during a call never silences the assistant, because the
caller is still on the line: the conversation returns to ``bot_handling`` and staff
see the handoff and the callback task. Calls whose end the provider never reports are
closed after six hours by the minute maintenance job with outcome ``unreconciled``.
Usage is metered in seconds per call; seconds beyond the tenant's included minutes in
the local month cost ``overage_eur_per_min`` (minute rounding bills the started
minute). The ``openai_compat`` (S) provider is not built (M14 decision). The WebSocket
path carries the tenant's public key, which is not a secret: an attacker who guesses
it can only hold a text conversation with the assistant at the tenant's model cost,
and the per-tenant LLM budget bounds that (documented in SECURITY.md, M12).

## D-022 Metering at write time with correction rows (F24)

Every WhatsApp message and model call is priced when its usage event is written, from
the dated `price_table`, `model_price` and `fx_rate` rows in force on the event date
(per-tenant rows win over global rows; when no row exists the built-in defaults from
`docs/VERIFIED_FACTS.md` apply and the event's `price_source` says `default` instead of `global` or `tenant`). The
free allowance is counted per tenant calendar month in the tenant's time zone, so a
message at 00:30 local on the first of the month starts the new allowance even though
it is still the previous day in UTC. Usage events are append-only (C16); when a price
row is corrected later, `reconcile_whatsapp` appends one `correction` row per month with
the difference and zero quantity, dated inside the month it corrects, and a second run
appends nothing. Prices are stored as micro-EUR and micro-USD integers; conversion uses
the dated FX row (default 0.92 EUR per USD, flagged as a default). Reports are read
from the events, so JSON, CSV and PDF exports always agree with the stored rows.

## D-023 Retention defaults and special categories (F25, C9)

Retention runs daily at 03:00 local per tenant (`maintenance.py`) with the periods
from the tenant profile (`retention` block): message texts and conversation notes after
`chat_days`, voice transcripts after `voice_transcript_days`, reservation personal data
(name, phone, e-mail, blind indexes) `booking_days_after_departure` days after departure
while the booking skeleton stays for statistics, guest identifiers once the guest has no
reservation and no message inside `chat_days`, closed callback tasks and journey log
rows with the chat, webhook payloads after `webhook_days`, usage events after
`metering_months` and withdrawn consents after `consent_years`. Guardrails flag inbound texts that match the
special-category keyword table (health, pregnancy, disability, religion, ethnicity,
sexual orientation, trade union, biometrics) in six languages; the answer still goes
out, the message and the conversation carry `special_category`, the conversation is
excluded from analytics aggregates and QA sampling, and its inbound texts are removed
seven days after the stay ends (or seven days after receipt without a booking),
whichever the profile makes earlier. Every deletion writes a `deletion_log` row with
table, count and reason. Erasure on request deletes content rows, keeps the suppression
row (so a STOP stays honoured) and the consent rows (proof of the opt-in), and records
the request status; sub-processor deletions are tracked as tasks with the 30-day
deadline because the service cannot delete data on Meta's or the model provider's side.

## D-024 QA sampling and analytics exclusions (F27)

The QA queue receives every handoff, every validator rejection, staff flags, and a
deterministic five-percent sample chosen by the SHA-256 of the conversation id (its
first 32 bits modulo 100 below five), so a conversation is either always or never
sampled and the sample does not depend on timing. Aggregates count conversations, not
guests, and exclude special-category conversations. The weekly digest is an e-mail to
the hotel contact on Monday 08:00 local with inline SVG charts (no script, no external
resource) and links to the dashboard; the same charts render on `/admin/reports`.
Exports redact phones, e-mails, IBANs and the names known from reservations.

## D-025 Backup format and operator alerts (F30, F32)

Backups are one file per run: the header `GCBK1` plus the kind (`sqlite` or `pgdump`)
and the nonce, then AES-256-GCM ciphertext of a SQLite online-backup snapshot or a
`pg_dump` custom-format dump; the header is the associated data, so a truncated or
edited file fails authentication instead of restoring garbage. The key is
`GC_BACKUP_KEY`, separate from the master key, so an off-site copy of the backups
does not carry the key that decrypts them. `restore-test` restores SQLite snapshots
into a scratch database and compares the migration head and row counts; PostgreSQL
dumps are verified with `pg_restore` outside the service. A failed backup raises the
`backup_failed` operator alert. Operator alerts are one row per kind and key with a
counter; the notifier mails an open alert once, again after 24 hours while it stays
open, and runs at most every 15 minutes from the timers hook. Price tables older than
180 days raise `price_table_stale` so the operator refreshes the rates.

