"""Inline SVG charts built in Python (F27): no JavaScript, no external assets.

Implements: F27, C1
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape


def bar_chart(
    labels: Sequence[str],
    values: Sequence[float],
    *,
    title: str,
    width: int = 520,
    height: int = 200,
    color: str = "#C22113",
) -> str:
    """A horizontal-axis bar chart; an empty series renders the title and a note."""
    pairs = list(zip(labels, values, strict=True))
    top = max((v for _, v in pairs), default=0.0)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="{escape(title)}">',
        f"<title>{escape(title)}</title>",
        f'<text x="8" y="16" font-size="12" font-family="sans-serif">{escape(title)}</text>',
    ]
    if not pairs or top <= 0:
        parts.append(
            f'<text x="8" y="{height // 2}" font-size="11" font-family="sans-serif">–</text>'
        )
        parts.append("</svg>")
        return "".join(parts)
    chart_top, chart_bottom = 28, height - 36
    slot = (width - 16) / len(pairs)
    bar_width = max(4.0, slot * 0.6)
    for index, (label, value) in enumerate(pairs):
        bar_height = (chart_bottom - chart_top) * (value / top)
        x = 8 + index * slot + (slot - bar_width) / 2
        y = chart_bottom - bar_height
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" '
            f'fill="{color}"><title>{escape(label)}: {value:g}</title></rect>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{y - 3:.1f}" font-size="10" '
            f'text-anchor="middle" font-family="sans-serif">{value:g}</text>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{height - 20}" font-size="10" '
            f'text-anchor="middle" font-family="sans-serif">{escape(str(label)[:14])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
