"""Load fixture and client-pack KB files (YAML) into a tenant's knowledge base.

Implements: F05, F21, F29, C5

The file format keeps one entry per key with per-language ``text``; every row lands
with the file's ``fixture`` flag in its ``source`` and, for fixtures, is approved by
the approver name ``fixture`` so the demo works without manual approval. Rows are
written in one transaction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from concierge.kb.store import KbService

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_kb_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "entries" not in data:
        raise ValueError(f"{path} is not a KB file (needs an 'entries' list)")
    return data


def rows_from_file(
    data: dict[str, Any], path: Path, *, approve: bool, approver: str | None
) -> list[dict[str, Any]]:
    fixture = bool(data.get("fixture"))
    source = f"fixture:{path.name}" if fixture else f"pack:{path.name}"
    rows: list[dict[str, Any]] = []
    for entry in data["entries"]:
        texts = entry.get("text") or {}
        if isinstance(texts, str):
            texts = {"en": texts}
        for lang, text in texts.items():
            rows.append(
                {
                    "key": str(entry["key"]),
                    "lang": str(lang),
                    "text": str(text),
                    "value": dict(entry.get("value") or {}),
                    "status": "approved" if approve else "draft",
                    "truth": str(entry.get("truth", "authoritative_kb")),
                    "sensitivity": entry.get("sensitivity"),
                    "source": source,
                    "volatile": entry.get("volatile"),
                    "approver": (approver or ("fixture" if fixture else None)) if approve else None,
                }
            )
    return rows


async def import_kb_file(
    service: KbService,
    tenant_id: str,
    path: Path,
    *,
    approve: bool = False,
    approver: str | None = None,
    actor: str = "import",
) -> int:
    """Import every entry and language; returns the number of rows written."""
    data = load_kb_file(path)
    rows = rows_from_file(data, path, approve=approve, approver=approver)
    return await service.bulk_upsert(tenant_id, rows, actor=actor)
