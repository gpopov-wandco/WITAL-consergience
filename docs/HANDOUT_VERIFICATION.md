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
