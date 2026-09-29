# Integrations

"Only API keys are left" means: every integration below has an adapter, a fake for
tests, a doctor check and this page saying where the human gets the key. Tenant
secrets are entered with `concierge secrets set <tenant> <name>` (hidden prompt) or
the write-only admin form, never in files or chat.

| Tool | Configuration | Where the human gets it | Sandbox | Doctor (`--live`) |
|---|---|---|---|---|
| Meta WhatsApp Cloud API | secrets `wa_access_token`, `wa_app_secret`, `wa_verify_token`; profile `whatsapp.waba_id`, `whatsapp.phone_number_id`; env `GC_WA_GRAPH_VERSION` | Business Suite, Business settings, System users: a token for the app with `whatsapp_business_messaging`, `whatsapp_business_management`, `business_management`; app secret under App settings, Basic; the verify token is any random string you also enter in the webhook configuration; callback `{GC_PUBLIC_BASE_URL}/wa/webhook`, subscribe `messages` and `message_template_status_update`; business verification for the display name and limits | Meta test WABA and number (five recipients, `hello_world`), public HTTPS through a tunnel; not the 24-hour temporary token | token (`GET /{phone_number_id}`), subscription, template statuses, a signed test webhook, tier, reminders for the payment method and the rate card |
| Anthropic API | `GC_LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, `GC_LLM_MODEL_PRIMARY`, `GC_LLM_MODEL_FAILOVER`, optional `GC_LLM_MODEL_CHEAP` | platform.claude.com console, workspace API key with a monthly spend limit; the DPA is part of the commercial terms | none; a small budget | `GET /v1/models` has the IDs, `count_tokens`, one 16-token call, retirement warning |
| Beds24 | secret `beds24_refresh_token`; profile `availability.beds24.property_id` | `https://beds24.com/control3.php?pagetype=apiv2`: the hotel creates an invite code, `concierge` exchanges it (read scopes `inventory`, `properties`, `bookings`) | trial account (VERIFY) | authentication details, scopes, room mapping, 7-day availability, a quote, credit headers |
| apaleo | secrets `apaleo_client_id`, `apaleo_client_secret`; profile `availability.apaleo.property_id` | apaleo.dev developer account; live client (whether a hotel can issue one itself: VERIFY); scopes `availability.read offers.read unitgroups.read setup.read rateplans.read reservations.read` | free resettable sandbox | token, properties, scopes, 7-day availability |
| Mews | profile `availability.mews.hotel_id`, `configuration_id` (Distributor); secrets `mews_client_token`, `mews_access_token` (Connector) | the hotel's booking-engine configuration; the Connector token after certification | public demo tokens (`api.mews-demo.com`) | `configuration/get`, time zone equals the tenant's |
| iCal | secrets `ical_url_<room>_<n>`; profile `availability.ical_units` | channel-manager calendar export URL per unit | any public feed | fetch, parse, feed age, unknown `RRULE` warning |
| E-mail | `GC_EMAIL_PROVIDER=resend` with `RESEND_API_KEY`, `GC_EMAIL_FROM`; or `smtp` with `GC_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_STARTTLS` | Resend dashboard: sending-only key, verified domain (SPF, DKIM) | Resend test addresses | configuration; `--live` sends a test mail to `GC_OPERATOR_EMAIL`; Resend region: VERIFY |
| Stripe (registration) | `STRIPE_SECRET_KEY` (restricted: Checkout Sessions, PaymentIntents), `STRIPE_WEBHOOK_SECRET`; profile `registration.enabled` | Stripe dashboard; account owner: VERIFY | test mode, published test cards | key mode, signed webhook round trip to `{GC_PUBLIC_BASE_URL}/r/psp/webhook` |
| Retell | `RETELL_API_KEY` (key with the webhook badge); profile `voice.provider=retell`, `voice.number`; secrets `voice_sip_user`, `voice_sip_password` | Retell dashboard; `concierge voice provision` creates the agent and imports the number; a sipgate or Twilio trunk carries the German number | web call (billing VERIFY) | signed webhook round trip, WebSocket reachability, number mapping, US-transfer gate, DPA-with-SCCs reminder |
| Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`; profile `voice.provider=twilio_cr` | Twilio console; a German number needs a regulatory bundle (approval time VERIFY) | call your own number | signature check, TwiML reachable |
| HubSpot, Stripe usage (S) | `HUBSPOT_TOKEN`; the Stripe key | HubSpot private apps | Stripe test mode | not built in this iteration (see BUILD_STATUS.md) |
| Platform | `GC_ENV`, `GC_DATABASE_URL`, `GC_PUBLIC_BASE_URL`, `GC_MASTER_KEYS`, `GC_ACTIVE_KEY_ID`, `GC_SIGNING_KEY`, `GC_BACKUP_KEY`, `GC_METRICS_TOKEN`, `GC_OPERATOR_EMAIL` | `concierge setup` generates the keys; DNS and hosting give the URL | localhost | public URL over valid TLS, `/readyz`, clock skew, disk |

## Reservation push API

`POST /api/v1/tenants/{public_key}/reservations` with `X-Signature` (HMAC-SHA256 of
the raw body with the tenant's `reservation_api_secret`) and `X-Timestamp` within five
minutes; idempotent by `res_id`. A website or a small PHP bridge pushes bookings and
the opt-in evidence (`whatsapp_optin`, `optin_source`, `optin_at`,
`optin_wording_version`).

## Non-goals

Booking.com, Expedia, HRS, protel, Clock, RoomRaccoon, Little Hotelier (partner-only)
and Cloudbeds are not built; Embedded Signup for WhatsApp is deferred (needs Meta app
review and browser JavaScript).
