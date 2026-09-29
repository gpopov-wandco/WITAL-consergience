# Operations

## Deploy (venv and systemd)

1. Create the user and directories: `useradd --system --home /var/lib/gc gc`,
   `mkdir -p /opt/gc /var/lib/gc /etc/gc`, `chown gc:gc /var/lib/gc`.
2. Install: `cd /opt/gc && python3.13 -m venv .venv && . .venv/bin/activate &&
   python -m pip install --require-hashes -r requirements.lock &&
   python -m pip install --no-deps --no-build-isolation .`
3. Environment: `concierge setup --non-interactive --env /etc/gc/env
   --public-base-url https://<host> --operator-email <address>` writes the generated
   keys; `chmod 0600 /etc/gc/env`. Set `GC_ENV=production` and `GC_DATABASE_URL`
   (PostgreSQL) in that file. Never commit it.
4. `cp deploy/systemd/concierge.service /etc/systemd/system/ && systemctl daemon-reload
   && systemctl enable --now concierge`. The unit runs as `gc`, reads
   `/etc/gc/env`, restarts on failure and mounts the file system read-only except
   `/var/lib/gc`.
5. Put Caddy (or another TLS proxy) in front of `127.0.0.1:8000`; the `Caddyfile`
   in the repository is the reference: automatic TLS, HSTS, WebSocket pass-through.
6. `concierge doctor` must show no `fail`; `concierge doctor --live` after the keys
   are entered.

## Deploy (Docker Compose)

`cp .env.example .env`, fill it (or run `concierge setup --non-interactive --env .env`
locally), export `POSTGRES_PASSWORD` and `CONCIERGE_DOMAIN`, then `docker compose up
-d`. The app container is non-root with a read-only root file system; data lives in
the `app-data` volume, PostgreSQL 18 in `db-data`, certificates in `caddy-data`.
`docker compose exec app concierge doctor` runs the checks inside the container.

## Upgrade and rollback

Upgrade: stop the service, `git pull` (or copy the new release), install with the
new lock file, `concierge db upgrade`, start, `concierge doctor`. Migrations are
forward-only in production; take a backup before every upgrade
(`concierge backup`). Rollback: stop, reinstall the previous release, and either
restore the pre-upgrade backup (`concierge restore-test` first) or, when the
migration is reversible, `alembic downgrade` with the previous code.

## Backup and restore

`concierge backup [--out DIR] [--keep-days N]` writes an AES-256-GCM file
(`concierge-<UTC stamp>.sqlite.enc` or `.pgdump.enc`) with `GC_BACKUP_KEY` and prunes
older files; a failure raises the `backup_failed` operator alert. Schedule it daily
(cron or a systemd timer) and copy the directory off the box (a Hetzner Storage Box or
snapshot; pricing VERIFY). Keep `GC_BACKUP_KEY` outside the backup location. Backups
contain guest data: apply the same retention to old copies. `concierge restore-test
[--file F]` restores the newest SQLite backup into a scratch database and compares the
migration head and row counts; PostgreSQL dumps are restored with `pg_restore` after
`concierge backup` decrypts them (see `tools/backup.py`, `decrypt`).

## Key rotation

Add a new key id to `GC_MASTER_KEYS`, set `GC_ACTIVE_KEY_ID` to it, restart, then
`concierge secrets rotate` re-encrypts every tenant secret; remove the old id after
the next backup. `GC_SIGNING_KEY` rotation invalidates signed links (invites,
registration links) and the prompt canary; rotate it in a quiet hour.
`GC_BACKUP_KEY` rotation means older backups need the old key: keep it in the key
store with its date range.

## Monitoring

`/healthz` (liveness), `/readyz` (database, migration head, worker heartbeat under
30 s, master key), `/metrics` (Prometheus text, bearer `GC_METRICS_TOKEN`). Operator
alerts go to `GC_OPERATOR_EMAIL` and the operator console: token invalid, spend cap,
circuit open, dead jobs, template paused, backup failure, stale price table. Logs are
JSON on stdout without PII (`journalctl -u concierge`).

## Routine tasks

| When | What |
|---|---|
| daily | check `/readyz`, the operator console (dead jobs, alerts), backup success |
| weekly | the Monday digest arrives; review the QA queue |
| monthly | `concierge usage export --tenant X --month YYYY-MM`; compare with Meta's invoice; `concierge metering reconcile` corrections appear in the report |
| every price change | re-check Meta's rate card and the model price table (operator console, Prices) |
| every 180 days | price tables age out: the doctor and the operator alert remind you |

## Incident checklist

1. Guests get no answers: `/readyz`, worker heartbeat, `concierge doctor`, the
   operator console's tenant health (webhook age, token state, circuit). A model
   outage degrades to the scripted responder automatically; a WhatsApp token failure
   raises `token_invalid` and stops sends until a new token is entered with
   `concierge secrets set <tenant> wa_access_token`.
2. Wrong answers: flag the reply in the inbox ("fix in KB"), correct the entry, approve
   it; the next answer uses the new snapshot.
3. Suspected data incident: pause the tenant (`concierge tenant pause`), rotate the
   affected keys (above), export the audit log, note the timeline; the hotel as
   controller decides on notifications (see COMPLIANCE.md).
4. Disk full: prune backups, check `GC_DATABASE_URL`; SQLite keeps working once space
   is free.
5. Restore: `concierge restore-test`, stop the service, replace the database file (or
   `pg_restore`), start, `concierge doctor`.
