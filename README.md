# WITALCO Guest Concierge (Gäste-Concierge)

A self-contained Python service that answers hotel guests on WhatsApp, on a website
widget and on a phone line in German, English, Russian, Ukrainian, Turkish and
Italian. It answers only from approved hotel facts and a real availability layer,
hands over to staff through an inbox with response timers, runs pre-arrival to
review journeys, meters usage per hotel and ships the tooling to onboard, test and
hand over each hotel.

The service runs and is tested in demo mode on a machine without API keys. Keys and
account details are added afterwards through configuration; `concierge doctor --live`
checks every integration.

## Install and start the demo (Linux, macOS)

```
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
concierge setup --non-interactive
concierge demo
```

## Install and start the demo (Windows, PowerShell)

```
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
concierge setup --non-interactive
concierge demo
```

`concierge setup` writes a `.env` file (never committed), generates the master key,
migrates the database and prints a one-time admin link. `concierge demo` seeds the
fictional "Hotel Beispiel", starts the server on `http://127.0.0.1:8000` and prints
the links to the widget test page, the WhatsApp simulator and the admin inbox.

Keep the SQLite database outside synced folders (OneDrive, Dropbox, iCloud). The
default location is `~/.witalco-concierge/`; `GC_DATABASE_URL` overrides it.

## Everyday commands

| Command | Purpose |
|---|---|
| `concierge doctor` | offline configuration checks (exit 0 clean, 1 warnings, 2 failures) |
| `concierge doctor --live --tenant hotel-beispiel` | live checks against every configured integration, recorded for the activation gate |
| `concierge check` | all static gates in one table |
| `pytest` | the offline test suite (unit, integration, golden, property, e2e) |
| `pytest -m load tests/load -s` | the load smoke with its numbers |
| `pytest -m browser tests/browser` | the browser suite (Playwright and Chromium): widget and admin driven in a real browser against a demo server |
| `concierge golden run` | the golden cases outside pytest, one table |
| `concierge serve` | start the service without seeding |
| `concierge wa-sim send --tenant hotel-beispiel --text "Gibt es einen Parkplatz?"` | post a signed WhatsApp webhook to the running service |
| `concierge demo voice-sim --say "Gibt es einen Parkplatz?"` | play a phone call against the running demo |
| `concierge demo advance --days 7` | warp the clock and run the journey steps that became due |
| `concierge tenant add\|list\|pause\|activate` | tenant lifecycle |
| `concierge secrets set <tenant> <name>` | enter a tenant secret with a hidden prompt |
| `concierge pack export\|import\|validate\|diff` | client packs |
| `concierge kb import-url <url> --tenant X`, `kb import-file <path> --tenant X` | draft entries from public pages (SSRF guard) or text, Markdown and PDF files |
| `concierge kb questionnaire items\|export\|import\|apply` | the 85-item questionnaire as YAML and its draft entries |
| `concierge kb translate --tenant X --to ru,uk` and `kb coverage --tenant X` | machine-translation drafts through the model; go-live key coverage |
| `concierge backup` and `concierge restore-test` | encrypted backups and restore rehearsal |
| `concierge handout build` | the PDF handout |
| `concierge compliance pack --tenant X` | the compliance pack |

## Documentation

`docs/ARCHITECTURE.md`, `docs/OPERATIONS.md` (deploy, upgrade, rollback, backup,
restore, key rotation, incidents), `docs/SECURITY.md`, `docs/COMPLIANCE.md`,
`docs/INTEGRATIONS.md` (per integration: variables and where to get them),
`docs/CUSTOMISATION.md`, `docs/ACCESSIBILITY_TEST.md`, `docs/VERIFIED_FACTS.md`,
`docs/DECISIONS.md`, `docs/BUILD_STATUS.md`, `docs/TRACEABILITY.md`,
`docs/COLD_START.md`, `docs/CHANGELOG.md`. The build specification is
`docs/BUILD_PROMPT.md`. Deployment files: `Dockerfile`, `compose.yaml`, `Caddyfile`,
`deploy/systemd/concierge.service`.

## Licence

Proprietary. Copyright WITALCO GmbH. Third-party components: htmx (0BSD), Inter and
Syne fonts (SIL Open Font License 1.1), see `src/concierge/static`.
