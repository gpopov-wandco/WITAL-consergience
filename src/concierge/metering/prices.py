"""Default price rows seeded into the database and cost arithmetic.

Implements: F24, 2.6, 2.7

These are dated defaults with their source, not constants the code enforces: the
database tables ``model_price``, ``price_table`` and ``fx_rate`` hold the rows that
metering uses, and operators edit them in the console. ``doctor`` warns when the
newest row is older than the configured age. Values come from
``docs/VERIFIED_FACTS.md`` (Anthropic pricing page, 2026-09-29) and the prompt's
Meta rate-card figures (VERIFY before every price change).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MICRO = 1_000_000


@dataclass(frozen=True)
class ModelPriceRow:
    model: str
    effective_from: date
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_write_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    source_url: str


@dataclass(frozen=True)
class MessagePriceRow:
    region: str
    category: str
    effective_from: date
    price_eur: float
    free_allowance_per_month: int
    source_url: str


ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
META_PRICING_URL = "https://developers.facebook.com/docs/whatsapp/pricing"

DEFAULT_MODEL_PRICES: tuple[ModelPriceRow, ...] = (
    ModelPriceRow(
        "claude-sonnet-5-5", date(2026, 9, 29), 2.0, 10.0, 2.5, 0.20, ANTHROPIC_PRICING_URL
    ),
    ModelPriceRow(
        "claude-sonnet-5", date(2026, 9, 29), 2.0, 10.0, 2.5, 0.20, ANTHROPIC_PRICING_URL
    ),
    ModelPriceRow(
        "claude-haiku-4-5", date(2026, 9, 29), 1.0, 5.0, 1.25, 0.10, ANTHROPIC_PRICING_URL
    ),
)

DEFAULT_MESSAGE_PRICES: tuple[MessagePriceRow, ...] = (
    MessagePriceRow("DE", "utility", date(2026, 1, 1), 0.0456, 0, META_PRICING_URL),
    MessagePriceRow("DE", "marketing", date(2026, 1, 1), 0.1131, 0, META_PRICING_URL),
    MessagePriceRow("DE", "authentication", date(2026, 1, 1), 0.0456, 0, META_PRICING_URL),
    MessagePriceRow("DE", "service", date(2026, 1, 1), 0.0, 0, META_PRICING_URL),
    MessagePriceRow("DE", "service", date(2026, 10, 1), 0.0456, 1000, META_PRICING_URL),
)

DEFAULT_USD_EUR = (
    date(2026, 9, 29),
    0.92,
    "operator-entered default; replace with the accounting rate",
)


def model_cost_usd(
    row: ModelPriceRow,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
) -> float:
    """USD cost of one call; cached tokens are billed at their own rates."""
    uncached_input = max(input_tokens, 0)
    return (
        uncached_input * row.input_usd_per_mtok
        + cache_write_tokens * row.cache_write_usd_per_mtok
        + cache_read_tokens * row.cache_read_usd_per_mtok
        + output_tokens * row.output_usd_per_mtok
    ) / MICRO


def default_model_price(model: str) -> ModelPriceRow | None:
    for row in DEFAULT_MODEL_PRICES:
        if model.startswith(row.model):
            return row
    return None


def to_micro(value: float) -> int:
    return round(value * MICRO)
