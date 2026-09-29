"""Webhook and body signatures, verified over raw bytes with constant-time compares.

Implements: F31, F12, F26, F15

Schemes (sources in ``docs/VERIFIED_FACTS.md``):

- Meta: ``X-Hub-Signature-256: sha256=<hex>``; HMAC-SHA256(app secret, raw body).
- Retell: ``X-Retell-Signature: v=<ms>,d=<hex>``; HMAC-SHA256(api key, body + str(ms));
  five-minute tolerance; the body is the raw bytes or the compact JSON serialisation.
- Twilio: ``X-Twilio-Signature``; base64(HMAC-SHA1(auth token, url + sorted params));
  computed with and without the explicit port; JSON bodies compare ``bodySHA256``.
- Stripe: ``Stripe-Signature: t=<s>,v1=<hex>``; HMAC-SHA256(secret, f"{t}.{body}").
- Reservation API (F12): ``X-GC-Signature: t=<s>,v1=<hex>``, same construction as
  Stripe with a per-tenant secret and a five-minute window.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping
from urllib.parse import parse_qs, urlparse

_RETELL_RE = re.compile(r"^v=(\d{1,20}),d=([0-9a-f]{64})$")
_KV_RE = re.compile(r"^t=(\d{1,20}),v1=([0-9a-f]{64})$")


def _hmac_hex(key: bytes, message: bytes, algorithm: str = "sha256") -> str:
    return hmac.new(key, message, getattr(hashlib, algorithm)).hexdigest()


# ------------------------------------------------------------------ Meta
def sign_meta(raw: bytes, app_secret: str) -> str:
    return "sha256=" + _hmac_hex(app_secret.encode("utf-8"), raw)


def meta_signature_ok(raw: bytes, header: str | None, app_secrets: Iterable[str]) -> int | None:
    """Return the index of the matching secret, or ``None`` when no secret matches."""
    if not header or not header.startswith("sha256="):
        return None
    presented = header[len("sha256=") :].strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", presented):
        return None
    for index, secret in enumerate(app_secrets):
        if not secret:
            continue
        expected = _hmac_hex(secret.encode("utf-8"), raw)
        if hmac.compare_digest(expected, presented):
            return index
    return None


# ---------------------------------------------------------------- Retell
def sign_retell(raw: bytes, api_key: str, timestamp_ms: int) -> str:
    digest = _hmac_hex(api_key.encode("utf-8"), raw + str(timestamp_ms).encode("ascii"))
    return f"v={timestamp_ms},d={digest}"


def retell_signature_ok(
    raw: bytes,
    header: str | None,
    api_key: str,
    now_ms: int,
    tolerance_ms: int = 5 * 60 * 1000,
) -> bool:
    if not header or not api_key:
        return False
    match = _RETELL_RE.match(header.strip())
    if not match:
        return False
    timestamp_ms = int(match.group(1))
    presented = match.group(2)
    if abs(now_ms - timestamp_ms) > tolerance_ms:
        return False
    key = api_key.encode("utf-8")
    suffix = str(timestamp_ms).encode("ascii")
    candidates = [raw]
    compact = _compact_json(raw)
    if compact is not None and compact != raw:
        candidates.append(compact)
    return any(hmac.compare_digest(_hmac_hex(key, body + suffix), presented) for body in candidates)


def _compact_json(raw: bytes) -> bytes | None:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------- Twilio
def _twilio_base(url: str, params: Mapping[str, Iterable[str] | str] | None) -> str:
    base = url
    if params:
        for name in sorted(set(params)):
            values = params[name]
            items = [values] if isinstance(values, str) else list(values)
            for value in sorted(set(items)):
                base += name + value
    return base


def _twilio_digest(auth_token: str, base: str) -> str:
    mac = hmac.new(auth_token.encode("utf-8"), base.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def sign_twilio(url: str, params: Mapping[str, Iterable[str] | str] | None, auth_token: str) -> str:
    return _twilio_digest(auth_token, _twilio_base(url, params))


def _with_and_without_port(url: str) -> list[str]:
    parsed = urlparse(url)
    variants = [url]
    host = parsed.hostname or ""
    if parsed.port:
        variants.append(parsed._replace(netloc=host).geturl())
    else:
        default = 443 if parsed.scheme == "https" else 80
        variants.append(parsed._replace(netloc=f"{parsed.netloc}:{default}").geturl())
    return variants


def twilio_signature_ok(
    url: str,
    params: Mapping[str, Iterable[str] | str] | None,
    header: str | None,
    auth_token: str,
    body: bytes | None = None,
) -> bool:
    if not header or not auth_token:
        return False
    query = parse_qs(urlparse(url).query)
    effective_params: Mapping[str, Iterable[str] | str] | None = params
    if "bodySHA256" in query:
        if body is None:
            return False
        if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), query["bodySHA256"][0]):
            return False
        effective_params = None
    presented = header.strip()
    for variant in _with_and_without_port(url):
        expected = _twilio_digest(auth_token, _twilio_base(variant, effective_params))
        if hmac.compare_digest(expected, presented):
            return True
    return False


# ---------------------------------------------------- Stripe and reservations
def sign_timestamped(raw: bytes, secret: bytes, timestamp_s: int) -> str:
    digest = _hmac_hex(secret, str(timestamp_s).encode("ascii") + b"." + raw)
    return f"t={timestamp_s},v1={digest}"


def timestamped_signature_ok(
    raw: bytes,
    header: str | None,
    secret: bytes,
    now_s: int,
    tolerance_s: int = 300,
) -> bool:
    if not header or not secret:
        return False
    match = _KV_RE.match(header.strip())
    if not match:
        # Stripe may send several signatures: t=..,v1=..,v1=..; accept the strict form
        # and the multi-signature form.
        return _multi_signature_ok(raw, header, secret, now_s, tolerance_s)
    timestamp_s = int(match.group(1))
    if abs(now_s - timestamp_s) > tolerance_s:
        return False
    expected = _hmac_hex(secret, str(timestamp_s).encode("ascii") + b"." + raw)
    return hmac.compare_digest(expected, match.group(2))


def _multi_signature_ok(
    raw: bytes, header: str, secret: bytes, now_s: int, tolerance_s: int
) -> bool:
    timestamp: int | None = None
    signatures: list[str] = []
    for item in header.split(","):
        if "=" not in item:
            return False
        key, value = item.strip().split("=", 1)
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and re.fullmatch(r"[0-9a-f]{64}", value):
            signatures.append(value)
    if timestamp is None or not signatures or abs(now_s - timestamp) > tolerance_s:
        return False
    expected = _hmac_hex(secret, str(timestamp).encode("ascii") + b"." + raw)
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


sign_stripe = sign_timestamped
stripe_signature_ok = timestamped_signature_ok
sign_reservation_body = sign_timestamped
reservation_signature_ok = timestamped_signature_ok
