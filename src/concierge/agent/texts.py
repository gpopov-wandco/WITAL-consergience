"""Loader for the per-language guest texts (YAML) with placeholder rendering.

Implements: F23, F03, C7, C10

Every text carries a ``status``; texts without one are functional defaults. Legal
texts (disclosure, emergency, opt-out, voice notice) ship as ``draft_unreviewed`` and
are listed by the compliance pack for lawyer review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

TEXTS_DIR = Path(__file__).resolve().parent / "texts"
LANGS: tuple[str, ...] = ("de", "en", "ru", "uk", "tr", "it")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True)
class Text:
    key: str
    lang: str
    text: str
    status: str

    def render(self, **values: Any) -> str:
        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            return str(values.get(name, match.group(0)))

        return _PLACEHOLDER.sub(sub, self.text)


class GuestTexts:
    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self._data = data

    @classmethod
    def load(cls, directory: Path = TEXTS_DIR) -> GuestTexts:
        data: dict[str, dict[str, Any]] = {}
        for lang in LANGS:
            path = directory / f"{lang}.yaml"
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if loaded.get("lang") != lang:
                raise ValueError(f"{path} declares lang {loaded.get('lang')!r}")
            data[lang] = loaded
        return cls(data)

    def languages(self) -> list[str]:
        return list(self._data)

    def get(self, key: str, lang: str) -> Text:
        lang = lang if lang in self._data else "en"
        entry = self._data[lang]["texts"].get(key)
        if entry is None:
            entry = self._data["en"]["texts"].get(key)
            if entry is None:
                raise KeyError(f"unknown guest text {key!r}")
        return Text(key=key, lang=lang, text=entry["text"], status=entry.get("status", "default"))

    def render(self, key: str, lang: str, **values: Any) -> str:
        return self.get(key, lang).render(**values)

    def button(self, button_id: str, lang: str) -> str:
        lang = lang if lang in self._data else "en"
        buttons = self._data[lang].get("buttons", {})
        return str(buttons.get(button_id) or self._data["en"]["buttons"].get(button_id, button_id))

    def keys(self, lang: str = "en") -> list[str]:
        return sorted(self._data[lang]["texts"])

    def unreviewed(self) -> list[tuple[str, str]]:
        """(key, lang) of every text still marked draft_unreviewed."""
        return sorted(
            (key, lang)
            for lang, payload in self._data.items()
            for key, entry in payload["texts"].items()
            if entry.get("status") == "draft_unreviewed"
        )


@lru_cache(maxsize=1)
def guest_texts() -> GuestTexts:
    return GuestTexts.load()
