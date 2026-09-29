# Handout verification (13, 10.6)

The handout is built by `concierge handout build --out docs/handout.pdf` from
`src/concierge/tools/handout.py`; the PDF itself is not committed (C1 allow-list),
so every reviewer builds it from the same source. This file records the checks of
the build of 2026-09-29 (version 0.1.0).

## Automated checks (`tests/unit/test_handout.py`, pypdf)

| Check | Result |
|---|---|
| Page count | 4 pages (limit 6) |
| Fonts | Inter-Regular and Syne-Bold embedded as subsets (`/FontFile2`); Courier for command lines and Helvetica for the header line are PDF base fonts |
| Placeholders | no `{`, `}` or `VERIFY` in the extracted text |
| Register (2.9) | no exclamation mark; "compliant" and "legally safe" absent |
| Commands | every `HANDOUT_COMMANDS` entry appears in the text; the list is executed literally below |
| Metadata | title "WITALCO Guest Concierge handout", 40 KB |

## Page-by-page inspection (rendered with pypdfium2 at 1.4x and read)

| Page | Content | Observed |
|---|---|---|
| 1 | header band (WITAL white, CO red), Part A: install for Linux/macOS and Windows, the five guided scenarios as a table (scenario, command, expected), test suite, doctor, live smoke test with a WhatsApp test number, start of the acceptance checklist | headings in Syne, body in Inter, command blocks on grey; the scenario commands are complete and unbroken; no overflow |
| 2 | rest of the acceptance checklist, Part B: credentials without secrets in files, roles, backups and the restore test, monitoring and alerts, processor agreement and sub-processors, training agenda (45 minutes), acceptance protocol, offboarding and data deletion, support boundaries | the checklist continues without an empty page; `<slug>` and `<name>` placeholders render as angle-bracket text (escaped) |
| 3 | Part C: client pack workflow with the four pack commands, the questionnaire and who fills it, tenant configuration, escalation, WhatsApp number and templates with the three template commands, PMS adapter table, go-live checklist with `record-dpa` and `activate`, pricing and metering with `usage export` | the commands use the CLI's argument forms (`pack export hotel-beispiel --out ...`, `templates sync hotel-beispiel`); the table fits the page |
| 4 | Appendix: troubleshooting table (symptom, check, command or place), where things live, pause and unpause | the small paragraph reads "Pausing a hotel: concierge tenant pause <slug>; resuming: concierge tenant unpause <slug>." as intended; the lower half of the page is empty, which keeps the appendix on one page |

Defects found and fixed during the inspection: the acceptance checklist forced a page
break that left page 2 nearly empty (the break was removed); the appendix paragraph
lost `<slug>` because it was not escaped (every paragraph now goes through the
escaping helper); three commands were written with `--tenant` although the CLI takes
the tenant as an argument (corrected, see the literal run below).

## Literal command run (2026-09-29, 11:12 to 11:21 UTC)

A driver script cloned the repository at commit `3d4fc7b`, copied the working tree
on top, created a fresh venv and a fresh `HOME`, then ran every entry of
`HANDOUT_COMMANDS` literally (the demo stays running in the background while the
other commands execute). Tokens are redacted in the log; nothing left the sandbox.

| Step | Command | Observed |
|---|---|---|
| Install | `python3.13 -m venv .venv`, `python -m pip install --require-hashes -r requirements.lock`, `python -m pip install --no-deps --no-build-isolation -e .` | exit 0; the hash-locked install took 47.2 s |
| Setup | `concierge setup --non-interactive` | exit 0 in 1.3 s: "environment written to .env" |
| Demo | `concierge demo` | `/readyz` answered `200 {"ready":true,"checks":{"database":"ok","migration":"ok","worker":"ok","master_key":"ok"}}`; the banner lists the widget page, the simulator, the admin and the operator console with the three fixture accounts |
| Scenario 1 | `concierge wa-sim send --tenant hotel-beispiel --text "Gibt es einen Parkplatz?"` | exit 0; simulator page: demo banner, then `A:parking` "Die hoteleigene Tiefgarage kostet 18 EUR pro Nacht; die Einfahrtshöhe beträgt 2,10 m. Es gibt 12 Stellplätze ..." |
| Scenario 2 | `... --text "When is breakfast served?" --from 4915120000002` | exit 0; `A:breakfast` "Breakfast is served daily from 07:00 to 10:00 for EUR 16 per person ..." followed by the `A:upsell` offer to request it at the reception |
| Scenario 3 | `... --text "Haben Sie vom 06.10.2026 bis 08.10.2026 ein Zimmer für 2 Erwachsene?"` | exit 0; `P:availability` "Laut unserem Kalender (Stand 29.09.2026): Einzelzimmer: höchstens 1 Personen pro Zimmer. Alternative: 2 Zimmer. Doppelzimmer: verfügb..." |
| Scenario 4 | `... --text "Wir kommen erst spät nach 22 Uhr an"` | exit 0; `A:late_arrival` "Bei Ankunft nach 22:00 Uhr liegt Ihre Schlüsselkarte im Schlüsselsafe neben dem Eingang. Den Code erhalten Sie am Anreisetag ..." |
| Scenario 5 | `... --text "Die Dusche ist kaputt und das Zimmer ist dreckig"` | exit 0; `H:kb_miss` then `H:handoff` "... Eine Antwort kommt in der Regel innerhalb von 2 Stunden."; the inbox check at the end found one handoff `('P1', 'open', 'responder')` and 11 outbound messages |
| Tests | `pytest` | exit 0 in 411.7 s: "726 passed, 3 deselected in 398.90s (0:06:38)" |
| Doctor | `concierge doctor` | exit 1: "39 pass, 5 warn, 0 fail (offline)"; the warnings name `GC_OPERATOR_EMAIL`, the e-mail variables, `GC_LLM_PROVIDER=none`, the missing backup and the 42 unapproved legal texts |
| Doctor live | `concierge doctor --live --tenant hotel-beispiel` | exit 2: "44 pass, 8 warn, 3 fail (live)"; the three failures are `whatsapp.token`, `whatsapp.subscription` and `whatsapp.templates` (`GraphError`, no network); the signed test webhook, `/healthz`, `/readyz` and the clock check pass |
| Usage | `concierge usage export --tenant hotel-beispiel --month 2026-10` | exit 0; JSON with `"rows": []` and `"total_eur": 0.0` |
| Secret | `concierge secrets set hotel-beispiel wa_access_token` | exit 0: "secret wa_access_token stored for hotel-beispiel"; `getpass` warns that it cannot control echo without a terminal (the driver has none) |
| Backup | `concierge backup` | exit 0: "backup written to .../.witalco-concierge/backups/concierge-2026...gcbk" |
| Restore test | `concierge restore-test` | exit 0: migration head `05c5504a3de8` (expected `05c5504a3de8`) and a table of backup versus restored row counts (`alembic_version` 1/1, `app_user` 4/4, `audit_log` 18/18, `consent` 1/1, `conversation` 2/2, `day_inventory` 240/240, `guest` 2/2, `handoff` 1/1, `job` 38/38, `kb_entry` 204/204 and 13 more rows) |
| Compliance | `concierge compliance pack --tenant hotel-beispiel` | exit 0; eight files written (README, sub-processors, retention, TOM checklist, AI disclosures, record of processing, legal texts, PDF); "every page is stamped 'Draft for lawyer review'; nothing here certifies compliance" |
| Pack | `concierge pack export hotel-beispiel --out packs/hotel-beispiel`, `pack validate`, `pack diff`, `pack import` | exit 0 each: eight pack files including `questionnaire_answers.yaml`; "pack ok"; "no changes"; "dry run: nothing written (use --apply)" |
| Tenant | `concierge tenant pause hotel-beispiel`, `unpause`, `record-dpa` | exit 0 each: "paused", "resumed", "recorded" |
| Templates | `concierge whatsapp templates lint`, `templates list hotel-beispiel` | exit 0: "60 templates lint clean"; 60 rows, all `APPROVED` in the demo registry |
| Activation | `concierge tenant activate hotel-beispiel` | exit 2: "activation refused" with one line per unapproved legal text (privacy paragraph, AI disclosure, WhatsApp opt-in and the others in six languages) |
| Template sync | `concierge whatsapp templates sync hotel-beispiel` | exit 1: "error: Meta Graph API: network error: ProxyError; check the network, wa_access_token and whatsapp.waba_id (docs/INTEGRATIONS.md)" |
| Stop | demo stopped by the driver | the secrets table held the five expected names (`reservation_api_secret`, `voice_socket_secret`, `wa_access_token`, `wa_app_secret`, `wa_verify_token`); the driver logs names only and redacts token-like strings before writing the log |

Two earlier attempts of this run are recorded in `docs/BUILD_STATUS.md` (open defects):
the first failed the `pytest` step because the `tests` package was not importable in a
fresh checkout, the second was hijacked by a demo process left over from the first.
Both defects were fixed (`pythonpath`, process-group shutdown and a busy-port refusal in
the driver) before the run above.
