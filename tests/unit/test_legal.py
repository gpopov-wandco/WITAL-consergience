"""Legal draft texts (F22, C10): every language, no compliance claims, placeholders."""

from __future__ import annotations

import pytest
from tests.unit.test_profile import SAMPLE

from concierge.legal.service import FORBIDDEN_CLAIMS, load_drafts, render
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F22")

LANGS = ("de", "en", "ru", "uk", "tr", "it")
KEYS = {
    "privacy_paragraph",
    "ai_disclosure",
    "whatsapp_optin",
    "chat_terms",
    "emergency_script",
    "complaint_script",
    "voice_notice",
}


def test_every_key_has_every_language() -> None:
    drafts = load_drafts()
    assert set(drafts.keys) == KEYS
    for key in drafts.keys:
        for lang in LANGS:
            text = drafts.text(key, lang)
            assert text and len(text) > 40, (key, lang)
    assert drafts.text("ai_disclosure", "xx") is None
    assert drafts.text("nope", "de") is None


def test_drafts_never_claim_compliance() -> None:
    drafts = load_drafts()
    for key in drafts.keys:
        for lang in LANGS:
            lowered = (drafts.text(key, lang) or "").lower()
            for claim in FORBIDDEN_CLAIMS:
                assert claim not in lowered, (key, lang, claim)
            assert "legally safe" not in lowered


def test_render_fills_hotel_placeholders() -> None:
    profile = TenantProfile.model_validate(SAMPLE)
    text = render("{hotel} – {privacy_url} – {phone} – {imprint_url}", profile)
    assert profile.name in text and profile.contacts.privacy_url in text
    assert profile.contacts.reception_phone in text and profile.contacts.imprint_url in text
    assert "{" not in text
