"""Signed, expiring links (invites, one-time admin link, registration forms, widget sessions).

Implements: F19, F15, F22, F31
"""

from __future__ import annotations

from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class LinkError(ValueError):
    pass


def make_token(payload: dict[str, Any], purpose: str, signing_key: bytes) -> str:
    serializer = URLSafeTimedSerializer(signing_key, salt=purpose)
    return str(serializer.dumps(payload))


def read_token(token: str, purpose: str, signing_key: bytes, max_age_s: int) -> dict[str, Any]:
    serializer = URLSafeTimedSerializer(signing_key, salt=purpose)
    try:
        payload = serializer.loads(token, max_age=max_age_s)
    except SignatureExpired as exc:
        raise LinkError("link expired") from exc
    except BadSignature as exc:
        raise LinkError("invalid link") from exc
    if not isinstance(payload, dict):
        raise LinkError("invalid link payload")
    return payload
