# Security

## Tenant isolation

Every business table carries `tenant_id`; sessions opened with
`db.tenant_scope(tenant_id)` filter every query through `with_loader_criteria` and a
`before_flush` guard refuses rows of another tenant. Unique keys include the tenant.
Raw SQL is banned outside migrations by a test. Tenant resolution never trusts the
client: WhatsApp by `metadata.phone_number_id` (unique across tenants) with the
matching app secret, widget by public key plus origin, voice by agent id or number,
admin by the session row. A property test interleaves two tenants' operations.

## Secrets and keys

Tenant secrets (tokens, PMS credentials, feed URLs) are AES-256-GCM encrypted with a
random 96-bit nonce and the AAD `tenant_id|name|key_id`, keyed from `GC_MASTER_KEYS`;
`concierge secrets set` reads them with a hidden prompt, the admin form is write-only,
`concierge secrets rotate` re-encrypts. Guest name, phone and e-mail and the
Meldeschein fields are encrypted the same way with a keyed-HMAC blind index for
lookups. Keys never appear in logs, prompts, fixtures, error messages or the CLI
output; the secret scan in `concierge check` fails the build on known key patterns.

## Webhooks and signatures

Meta: HMAC-SHA256 over the raw body, constant-time compare, before parsing. Retell:
`v={ts},d={hex}` over body plus timestamp with the API key. Twilio: the request
validator over URL and form fields, with and without the port, and `bodySHA256`.
Stripe: `t=,v1=` with a 300-second tolerance. Reservation push: HMAC-SHA256 over the
raw body with a per-tenant secret, five-minute window, idempotent by `res_id`. Tests
mutate one byte of every signed body and header.

## Admin authentication

Argon2id passwords, server-side sessions (random 256-bit token stored hashed, 12-hour
life, 2-hour idle), cookies `HttpOnly; Secure; SameSite=Lax`, CSRF token on every
mutation (form field or `X-CSRF-Token`), lockout after five failures for fifteen
minutes, no default passwords: the first run prints a one-time admin link, invites are
single-use 15-minute links. Roles owner, staff and operator; every operator tenant
switch is audited. The demo accounts exist only when `GC_ENV` is not production.

## Browser security

CSP without `unsafe-inline` scripts, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY`,
`Cache-Control: no-store` on application pages; HSTS at Caddy. The widget uses a
Shadow DOM, a per-tenant CORS allow-list, per-IP and per-session limits, no cookies
and `sessionStorage` only after the launcher is clicked.

## Outbound fetches

One SSRF guard for every URL the service fetches (iCal feeds, KB import): http and
https only, the address is resolved and pinned, private, loopback and link-local
ranges are blocked, redirects are capped and re-checked, size and time are limited.

## Voice

The Retell custom-LLM and Twilio relay WebSocket paths carry the tenant's public key
and a per-tenant socket secret (`voice_socket_secret`, created by `concierge voice
provision`, stored encrypted like every tenant secret, compared in constant time). A
socket without the right secret is closed before any message is read, so nobody can
drive the assistant, create staff tasks or spend the model budget by guessing the
public key. Webhooks are signed; audio is never recorded; transcripts expire; caller
utterances are redacted (card, IBAN, ID numbers) before they are stored.

## Logging

structlog JSON with tenant, conversation and request ids; the processor redacts
`text`, `body`, `message`, `name`, `email`, `phone`, `address` and `token`; phones
appear only as truncated keyed hashes; the uvicorn access log is off.

## Model boundary

Guest text, PMS notes, iCal summaries, imported pages and e-mail bodies are untrusted:
guest text goes into the user turn, fetched text only inside tool results marked as
untrusted. The system prompt carries a canary that validators reject in any reply.
Tools are re-authorised from the session (tenant and guest never come from model
arguments), read-only except the queue-creating ones, with strict schemas.

## Dependencies

`requirements.lock` pins every package with hashes; `pip-audit`, `pip check` and
`bandit` run in `concierge check`. Only adapter modules import vendor SDKs.

## Reporting

Report suspected vulnerabilities to the operator address configured in
`GC_OPERATOR_EMAIL`; the incident checklist is in OPERATIONS.md.
