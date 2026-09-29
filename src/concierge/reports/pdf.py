"""Small PDF pages with the bundled Inter font (F24 usage report, F25 export, F34).

Implements: F24, F25, F34, C1

ReportLab draws plain text pages; Inter carries Latin, Cyrillic and Turkish glyphs so
every supported language renders. Long content paginates.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONTS_DIR = Path(__file__).resolve().parents[1] / "static" / "fonts"
FONT = "Inter"
FONT_BOLD = "Inter-Bold"
_registered = False


def register_fonts() -> None:
    global _registered
    if _registered:
        return
    pdfmetrics.registerFont(TTFont(FONT, str(FONTS_DIR / "Inter-Regular.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONTS_DIR / "Inter-Bold.ttf")))
    _registered = True


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if pdfmetrics.stringWidth(candidate, font, size) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def simple_pdf(
    title: str,
    sections: Sequence[tuple[str, Sequence[str]]],
    *,
    subtitle: str = "",
    table: Sequence[Sequence[str]] | None = None,
    footer: str = "",
) -> bytes:
    """A titled document: sections of lines and an optional table; paginates as needed."""
    register_fonts()
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 48.0
    y = height - margin

    def newline(step: float) -> None:
        nonlocal y
        y -= step
        if y < margin:
            page.showPage()
            y = height - margin

    page.setFont(FONT_BOLD, 16)
    page.drawString(margin, y, title)
    newline(22)
    if subtitle:
        page.setFont(FONT, 10)
        page.drawString(margin, y, subtitle)
        newline(18)
    for heading, lines in sections:
        page.setFont(FONT_BOLD, 11)
        page.drawString(margin, y, heading)
        newline(15)
        page.setFont(FONT, 9.5)
        for line in lines:
            for piece in _wrap(line, FONT, 9.5, width - 2 * margin) or [""]:
                page.setFont(FONT, 9.5)
                page.drawString(margin, y, piece)
                newline(13)
        newline(6)
    if table:
        columns = max(len(row) for row in table)
        col_width = (width - 2 * margin) / max(columns, 1)
        for index, row in enumerate(table):
            page.setFont(FONT_BOLD if index == 0 else FONT, 9)
            for col, cell in enumerate(row):
                page.drawString(margin + col * col_width, y, str(cell)[:40])
            newline(13)
    if footer:
        page.setFont(FONT, 8)
        page.drawString(margin, margin / 2, footer[:150])
    page.save()
    return buffer.getvalue()
