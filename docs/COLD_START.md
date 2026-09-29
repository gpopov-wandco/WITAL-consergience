# Cold-start test (10.6)

Run on 2026-09-29 on Linux (the build container: Python 3.13.12, no API keys, no
Docker daemon), commit `e2e30ca`, from a fresh clone in an empty directory with a
fresh virtual environment and a fresh home directory, following the README literally.
The script and its full log are the build session's scratch files; the observed
outputs are recorded here.

| Step | Command | Observed |
|---|---|---|
| 1 | `python3.13 -m venv .venv` and activate | Python 3.13.12 |
| 2 | `python -m pip install --require-hashes -r requirements.lock` | exit 0, 48 s, every package installed from the hash-locked file |
| 3 | `python -m pip install --no-deps --no-build-isolation -e .` | exit 0 |
| 4 | `concierge setup --non-interactive` | exit 0; "database migrated to head", ".env" written with mode 0600, the SQLite file under `~/.witalco-concierge/`, one-time admin link printed once |
| 5 | `concierge demo --port 8765` | `/readyz` answered 200 after 4 s: database ok, migration ok, worker ok, master key ok; the banner lists the widget page, the simulator, the admin, the operator console, the fixture accounts and "no ANTHROPIC_API_KEY: the scripted responder answers" |
| 6 | `concierge wa-sim send --tenant hotel-beispiel --text "Gibt es einen Parkplatz?" --base-url http://127.0.0.1:8765` | "webhook answered 200; events stored: 1"; the simulator page showed the parking answer with the garage price |
| 7 | widget page and static files | `/demo/widget`, `/w/loader.js`, `/w/gc-widget.js`, `/w/gc-widget.css` answered 200 |
| 8 | inbox with the demo owner account | login page 200, login 303, `/admin/inbox` 200 (a plain question creates no handoff, so the inbox was empty; `/admin/conversations` is not a page, conversations open from the inbox and the search) |
| 9 | `concierge doctor` | exit 1: 38 pass, 5 warn, 0 fail; the warnings name `GC_OPERATOR_EMAIL`, `RESEND_API_KEY` and `GC_EMAIL_FROM` ("e-mail not configured"), `GC_LLM_PROVIDER` ("the scripted responder answers"), the missing backup and the 42 unreviewed legal texts; prompt prefixes about 3,000 tokens per language and channel |
| 10 | fake keys and `concierge doctor --live --tenant hotel-beispiel` | exit 2: 46 pass, 4 warn, 5 fail. Anthropic: "auth ... invalid x-api-key", fix names `ANTHROPIC_API_KEY`; WhatsApp token, subscription and templates failed with the fix naming `wa_access_token`, `whatsapp.phone_number_id`, `whatsapp.waba_id`; Stripe webhook 401 with the fix naming `STRIPE_WEBHOOK_SECRET`; the signed test webhook and the platform probes passed; no fake key appeared in the output; the run was recorded for the activation gate |
| 11 | kill the server 0.3 s after a second message, restart | at the kill the reply row existed but the send had not happened; after the restart the outbox job sent it once: one message row, one send recorded by the mock Graph, every inbound job done with one attempt |
| 12 | `docker build` | not executed: no Docker daemon in the build environment ("statically reviewed": `tests/unit/test_deploy_files.py`) |

Not verified on Windows: the build environment is Linux. The README's Windows block
uses the same commands with the PowerShell activation; the code avoids the known
Windows traps (no `add_signal_handler`, engines disposed before temp files are
deleted, UTF-8 forced on the standard streams).

The handout's commands are executed literally in a second clean environment as part
of the handout verification (`docs/HANDOUT_VERIFICATION.md`).
