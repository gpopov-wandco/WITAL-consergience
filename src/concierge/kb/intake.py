"""Knowledge-base intake (F06): public pages, text, Markdown and PDF files become draft
entries, approved entries become machine-translation drafts, and the coverage report
compares the store with the go-live keys.

Implements: F06, C5, C6

Imported text is untrusted data: it is classified by keywords, never interpreted,
and lands as ``draft`` with its source. Page fetches go through the SSRF guard and
are capped at five pages of one megabyte; machine translations land as ``draft_mt``
and reach nobody before the owner approves them.
"""

from __future__ import annotations

import asyncio
import io
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from concierge.kb.blocks import validate_key
from concierge.kb.store import KbService
from concierge.llm.port import LLMError, LLMPort, LLMRequest, Message, TextBlock, TextPart
from concierge.security.ssrf import Fetcher, FetchError, SSRFError, validate_url

MAX_PAGES = 5
MAX_PAGE_BYTES = 1_000_000
MAX_FILE_BYTES = 10_000_000
MAX_PDF_PAGES = 50
MAX_TEXT_CHARS = 1500
"""A proposed draft keeps at most this much text; the owner edits the rest."""
MAX_KEYED_CHARS = 4000
"""A file imported under one explicit key keeps at most this much text."""
MIN_SECTION_CHARS = 40
KEYWORDS_PATH = Path(__file__).resolve().parent / "intake_keywords.yaml"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts"
LANGUAGE_NAMES: dict[str, str] = {
    "de": "German",
    "en": "English",
    "ru": "Russian",
    "uk": "Ukrainian",
    "tr": "Turkish",
    "it": "Italian",
}
_BINARY_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".css",
    ".js",
    ".zip",
    ".mp4",
    ".mp3",
    ".woff",
    ".woff2",
    ".ttf",
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Section:
    heading: str
    text: str


@dataclass(frozen=True)
class Proposal:
    key: str
    lang: str
    text: str
    source: str
    truncated: bool = False


@dataclass
class PageResult:
    url: str
    status: str
    sections: int = 0


@dataclass
class IntakeReport:
    pages: list[PageResult] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)


@dataclass
class TranslationReport:
    rows: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ text
def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def clean_text(text: str) -> str:
    """Normalised text without control characters; line structure kept, runs collapsed."""
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")]
    joined = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _paragraphs(heading: str, text: str) -> list[Section]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [Section(heading, part) for part in parts]


class _PageParser(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
    HEADINGS = frozenset({"h1", "h2", "h3", "h4"})
    BLOCKS = frozenset(
        {
            "p",
            "li",
            "td",
            "th",
            "div",
            "br",
            "section",
            "article",
            "tr",
            "ul",
            "ol",
            "dd",
            "dt",
            "header",
            "footer",
            "nav",
            "main",
            "blockquote",
            "pre",
            "table",
            "figcaption",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[str] = []
        self.sections: list[Section] = []
        self._in_title = False
        self._skip = 0
        self._heading: list[str] | None = None
        self._current_heading = ""
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
        elif tag in self.HEADINGS:
            self._flush()
            self._heading = []
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        elif tag in self.BLOCKS:
            self._buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag in self.HEADINGS and self._heading is not None:
            self._current_heading = clean_text("".join(self._heading)).replace("\n", " ")
            self._heading = None
        elif tag in self.BLOCKS:
            self._buffer.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        elif self._heading is not None:
            self._heading.append(data)
        else:
            self._buffer.append(data)

    def _flush(self) -> None:
        text = clean_text("".join(self._buffer))
        self._buffer = []
        if text:
            self.sections.append(Section(self._current_heading, text))

    def finish(self) -> list[Section]:
        self.close()
        self._flush()
        title = clean_text(self.title).replace("\n", " ")
        return [Section(s.heading or title, s.text) for s in self.sections]


def html_sections(html: str) -> tuple[list[Section], list[str]]:
    """Sections (heading and text) and the raw links of one page."""
    parser = _PageParser()
    try:
        parser.feed(html)
        sections = parser.finish()
    except (AssertionError, ValueError):
        return [], []
    return sections, parser.links


def markdown_sections(text: str) -> list[Section]:
    """Sections split at Markdown headings, then at blank lines (plain text too).

    Text before the first heading carries no heading: a file name never steers the
    classification.
    """
    sections: list[Section] = []
    heading = ""
    buffer: list[str] = []

    def flush() -> None:
        body = clean_text("\n".join(buffer))
        buffer.clear()
        if body:
            sections.extend(_paragraphs(heading, body))

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            heading = match.group(1).strip()
        else:
            buffer.append(line)
    flush()
    return sections


def pdf_sections(body: bytes) -> list[Section]:
    """Text of the first ``MAX_PDF_PAGES`` pages; an unreadable file gives no sections."""
    try:
        reader = PdfReader(io.BytesIO(body))
        pages = [page.extract_text() or "" for page in reader.pages[:MAX_PDF_PAGES]]
    except (PyPdfError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        return []
    return markdown_sections("\n\n".join(pages))


# ------------------------------------------------------------------ proposals
def load_keywords(path: Path = KEYWORDS_PATH) -> dict[str, list[str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    table: dict[str, list[str]] = {}
    for key, words in data.items():
        if key == "version" or not isinstance(words, list):
            continue
        table[validate_key(str(key))] = [_fold(str(word)) for word in words]
    return table


def classify(section: Section, table: dict[str, list[str]]) -> str | None:
    """The block whose words appear most in the heading (weight 3) and the lead text."""
    heading = _fold(section.heading)
    lead = _fold(section.text[:300])
    best_key: str | None = None
    best = 0
    for key, words in table.items():
        score = sum(3 for word in words if word in heading) + sum(
            1 for word in words if word in lead
        )
        if score > best:
            best, best_key = score, key
    return best_key


def propose(
    sections: Sequence[Section],
    *,
    lang: str,
    source: str,
    table: dict[str, list[str]] | None = None,
) -> list[Proposal]:
    """One draft per classified block; sections of the same block are joined."""
    table = table if table is not None else load_keywords()
    merged: dict[str, list[str]] = {}
    for section in sections:
        if len(section.text) < MIN_SECTION_CHARS:
            continue
        key = classify(section, table)
        if key is None:
            continue
        merged.setdefault(key, []).append(section.text)
    proposals: list[Proposal] = []
    for key, texts in merged.items():
        text = "\n\n".join(texts)
        truncated = len(text) > MAX_TEXT_CHARS
        proposals.append(
            Proposal(key, lang, text[:MAX_TEXT_CHARS].rstrip(), source[:120], truncated)
        )
    return proposals


def _decode(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _same_site_links(page_url: str, host: str, links: Sequence[str]) -> list[str]:
    found: list[str] = []
    for link in links:
        if link.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(page_url, link)
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"} or parts.hostname != host:
            continue
        if parts.path.lower().endswith(_BINARY_SUFFIXES):
            continue
        found.append(urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, "")))
    return found


async def import_url(
    fetcher: Fetcher,
    url: str,
    *,
    lang: str,
    max_pages: int = MAX_PAGES,
    max_bytes: int = MAX_PAGE_BYTES,
) -> IntakeReport:
    """Fetch the page and same-site links it points to (``max_pages`` in total) and
    propose drafts; a refused or failed page is reported, never raised."""
    report = IntakeReport()
    try:
        _scheme, host, _port, _path = validate_url(url)
    except SSRFError as exc:
        report.pages.append(PageResult(url, f"refused: {exc}"))
        return report
    queue = [url]
    seen = {url}
    sections: list[Section] = []
    while queue and len(report.pages) < max_pages:
        current = queue.pop(0)
        try:
            result = await fetcher.fetch(current)
        except SSRFError as exc:
            report.pages.append(PageResult(current, f"refused: {exc}"))
            continue
        except (OSError, TimeoutError) as exc:
            cause = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
            report.pages.append(PageResult(current, f"unreachable: {cause}"))
            continue
        if result.status != 200:
            report.pages.append(PageResult(current, f"http {result.status}"))
            continue
        if len(result.body) > max_bytes:
            report.pages.append(PageResult(current, f"refused: larger than {max_bytes} bytes"))
            continue
        content_type = result.content_type.split(";")[0].strip().lower()
        links: list[str] = []
        if content_type == "application/pdf" or current.lower().endswith(".pdf"):
            page_sections = pdf_sections(result.body)
        elif content_type in {"text/html", "application/xhtml+xml", ""}:
            page_sections, links = html_sections(_decode(result.body, result.content_type))
        elif content_type.startswith("text/"):
            page_sections = markdown_sections(_decode(result.body, result.content_type))
        else:
            report.pages.append(PageResult(current, f"skipped: {content_type}"))
            continue
        sections.extend(page_sections)
        report.pages.append(PageResult(current, "ok", len(page_sections)))
        for link in _same_site_links(current, host, links):
            if link not in seen:
                seen.add(link)
                queue.append(link)
    report.proposals = propose(sections, lang=lang, source=f"url:{host}")
    return report


def import_file(
    path: Path, *, lang: str, key: str | None = None, max_bytes: int = MAX_FILE_BYTES
) -> list[Proposal]:
    """Drafts from a text, Markdown or PDF file; with ``key`` the whole text is one draft."""
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{path.name} is larger than {max_bytes} bytes")
    body = path.read_bytes()
    if path.suffix.lower() == ".pdf" or body.startswith(b"%PDF"):
        sections = pdf_sections(body)
    else:
        sections = markdown_sections(body.decode("utf-8", errors="replace"))
    source = f"file:{path.name}"[:120]
    if key:
        text = "\n\n".join(section.text for section in sections)
        if not text.strip():
            return []
        return [
            Proposal(
                validate_key(key),
                lang,
                text[:MAX_KEYED_CHARS].rstrip(),
                source,
                len(text) > MAX_KEYED_CHARS,
            )
        ]
    return propose(sections, lang=lang, source=source)


async def store_proposals(
    kb: KbService, tenant_id: str, proposals: Sequence[Proposal], *, actor: str
) -> int:
    """Write the proposals as ``draft`` rows with their source; returns the count."""
    rows = [
        {
            "key": p.key,
            "lang": p.lang,
            "text": p.text,
            "status": "draft",
            "truth": "authoritative_kb",
            "source": p.source,
        }
        for p in proposals
    ]
    if not rows:
        return 0
    return await kb.bulk_upsert(tenant_id, rows, actor=actor)


# ------------------------------------------------------------------ translation
async def translate_entries(
    llm: LLMPort,
    model: str,
    entries: Sequence[Any],
    targets: Sequence[str],
    *,
    inference_geo: str = "global",
    prompt_path: Path = PROMPTS_DIR / "translate.md",
) -> TranslationReport:
    """Machine-translation drafts (``draft_mt``) of the entries into the target languages.

    Each entry is sent as plain text under a fixed instruction; the reply is stored as
    a draft with the model as its source. Failures are reported per entry and language.
    """
    template = (await asyncio.to_thread(prompt_path.read_text, encoding="utf-8")).strip()
    report = TranslationReport()
    for entry in entries:
        for target in targets:
            if target == entry.lang or not entry.text.strip():
                continue
            request = LLMRequest(
                model=model,
                system=[TextBlock(template.format(language=LANGUAGE_NAMES.get(target, target)))],
                messages=[Message(role="user", content=[TextPart(entry.text)])],
                max_tokens=min(4096, 256 + 2 * len(entry.text)),
                effort="low",
                inference_geo=inference_geo,
                cache_tail=False,
            )
            try:
                response = await llm.complete(request)
            except LLMError as exc:
                report.failures.append(f"{entry.key}/{target}: {exc.kind}")
                continue
            text = response.text.strip()
            if not text:
                report.failures.append(f"{entry.key}/{target}: empty reply")
                continue
            report.rows.append(
                {
                    "key": entry.key,
                    "lang": target,
                    "text": text,
                    "value": dict(entry.value or {}),
                    "sensitivity": entry.sensitivity,
                    "volatile": entry.volatile,
                    "truth": entry.truth,
                    "status": "draft_mt",
                    "source": f"mt:{model}"[:120],
                }
            )
    return report


# ------------------------------------------------------------------ coverage
def coverage_rows(
    coverage: dict[str, dict[str, str]], languages: Sequence[str]
) -> tuple[list[list[str]], int]:
    """Table rows per go-live key and the number of keys approved in every language."""
    rows = [[key, *[coverage[key].get(lang, "missing") for lang in languages]] for key in coverage]
    complete = sum(
        1 for key in coverage if all(coverage[key].get(lang) == "approved" for lang in languages)
    )
    return rows, complete
