"""Model capability table used by the adapter to shape requests.

Implements: 8.1, F28

Facts from ``docs/VERIFIED_FACTS.md`` (platform docs read 2026-09-29): effort and
``inference_geo`` are supported on Claude 4.6 and later; Haiku 4.5 supports neither
and returns 400 when ``inference_geo`` is present. Model ids themselves come from
configuration; this table only says how to talk to a model family. ``doctor`` compares
configured ids against ``GET /v1/models``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    supports_effort: bool
    supports_inference_geo: bool
    supports_structured_outputs: bool
    context_window: int
    min_cache_prefix_tokens: int


_FAMILIES: list[tuple[str, ModelCapabilities]] = [
    ("claude-haiku-4-5", ModelCapabilities(False, False, True, 200_000, 2048)),
    ("claude-sonnet-4-5", ModelCapabilities(False, False, True, 200_000, 1024)),
    ("claude-opus-4-5", ModelCapabilities(False, False, True, 200_000, 4096)),
    ("claude-sonnet-4-6", ModelCapabilities(True, True, True, 1_000_000, 1024)),
    ("claude-opus-4-", ModelCapabilities(True, True, True, 1_000_000, 4096)),
    ("claude-sonnet-5", ModelCapabilities(True, True, True, 1_000_000, 512)),
    ("claude-opus-5", ModelCapabilities(True, True, True, 1_000_000, 512)),
    ("claude-fable-5", ModelCapabilities(True, True, True, 1_000_000, 512)),
    ("claude-mythos-5", ModelCapabilities(True, True, True, 1_000_000, 512)),
]
_DEFAULT = ModelCapabilities(True, True, True, 1_000_000, 512)


def capabilities_for(model_id: str) -> ModelCapabilities:
    """Capabilities by model-id prefix; unknown ids get the current-generation defaults."""
    for prefix, caps in _FAMILIES:
        if model_id.startswith(prefix):
            return caps
    return _DEFAULT
