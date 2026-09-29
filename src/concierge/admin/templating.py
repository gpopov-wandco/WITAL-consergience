"""Shared Jinja2 environment for admin, operator and demo pages.

Implements: F19, F29, F31

Autoescape is on for every template; no inline scripts are rendered, so the CSP can
stay at ``script-src 'self'``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def build_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(default=True, default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )


templates = Jinja2Templates(env=build_environment())
