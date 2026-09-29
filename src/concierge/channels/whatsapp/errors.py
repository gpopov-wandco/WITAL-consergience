"""Meta error codes and the action the outbox takes (6.6).

Implements: 6.6, F18, F32

The table follows the build prompt (VERIFY against developers.facebook.com before
go-live; the actions, not the codes, are what the outbox relies on).
"""

from __future__ import annotations

from typing import Literal

Action = Literal[
    "token_invalid",
    "backoff",
    "window_closed",
    "restricted",
    "no_retry_24h",
    "opted_out",
    "undeliverable",
    "pair_rate",
    "template_error",
    "retry",
    "fail",
]

ACTIONS: dict[int, Action] = {
    0: "token_invalid",
    190: "token_invalid",
    4: "backoff",
    80007: "backoff",
    130429: "backoff",
    131047: "window_closed",
    131048: "restricted",
    368: "restricted",
    131049: "no_retry_24h",
    131050: "opted_out",
    131026: "undeliverable",
    131056: "pair_rate",
    132000: "template_error",
    132001: "template_error",
    132015: "template_error",
    132016: "template_error",
}

RETRYABLE_ACTIONS: frozenset[str] = frozenset({"backoff", "pair_rate", "retry"})
ALERT_ACTIONS: frozenset[str] = frozenset({"token_invalid", "restricted", "template_error"})


def action_for(code: int, status: int | None = None) -> Action:
    if code in ACTIONS:
        return ACTIONS[code]
    if code == -1:
        return "retry"
    if status is not None and (status == 429 or status >= 500):
        return "retry"
    return "fail"
