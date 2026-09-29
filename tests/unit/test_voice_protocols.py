"""Voice provider protocols (F26): signatures, event parsing, TwiML, speech, summaries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from concierge.channels.voice import retell, twilio_cr
from concierge.channels.voice.postcall import billable_seconds, summary_de
from concierge.channels.voice.session import VoicePhrases
from concierge.channels.voice.speech import for_speech, spoken_time

pytestmark = pytest.mark.feature("F26")

API_KEY = "key_test_with_webhook_badge"
NOW_MS = 1_790_000_000_000


# ------------------------------------------------------------------ retell
def test_retell_signature_vectors_and_mutations() -> None:
    body = json.dumps({"event": "call_ended", "call": {"call_id": "c1"}}).encode()
    expected = hmac.new(API_KEY.encode(), body + str(NOW_MS).encode(), hashlib.sha256).hexdigest()
    header = retell.sign(body, API_KEY, NOW_MS)
    assert header == f"v={NOW_MS},d={expected}"
    assert retell.verify_signature(body, header, API_KEY, now_ms=NOW_MS)
    assert retell.verify_signature(body, header, API_KEY, now_ms=NOW_MS + 4 * 60 * 1000)
    # one-byte mutations of body, digest and key fail; so does an old timestamp
    assert not retell.verify_signature(body + b" ", header, API_KEY, now_ms=NOW_MS)
    flipped = header[:-1] + ("0" if header[-1] != "0" else "1")
    assert not retell.verify_signature(body, flipped, API_KEY, now_ms=NOW_MS)
    assert not retell.verify_signature(body, header, API_KEY + "x", now_ms=NOW_MS)
    assert not retell.verify_signature(body, header, API_KEY, now_ms=NOW_MS + 6 * 60 * 1000)
    assert not retell.verify_signature(body, None, API_KEY, now_ms=NOW_MS)
    assert not retell.verify_signature(body, "v=abc,d=00", API_KEY, now_ms=NOW_MS)
    assert not retell.verify_signature(body, header, "", now_ms=NOW_MS)


def test_retell_signature_accepts_compact_reserialisation() -> None:
    pretty = json.dumps({"event": "call_ended", "call": {"call_id": "c1"}}, indent=2).encode()
    compact = json.dumps(json.loads(pretty), separators=(",", ":"), ensure_ascii=False).encode()
    header = retell.sign(compact, API_KEY, NOW_MS)
    assert retell.verify_signature(pretty, header, API_KEY, now_ms=NOW_MS)


def test_retell_event_parsing() -> None:
    assert retell.parse_event({"event": "other"}) is None
    assert retell.parse_event({"event": "call_ended", "call": {}}) is None
    event = retell.parse_event(
        {
            "event": "call_ended",
            "call": {
                "call_id": "call_1",
                "agent_id": "agent_1",
                "from_number": "+4915120000009",
                "to_number": "+4969000009",
                "start_timestamp": 1_790_000_000_000,
                "end_timestamp": 1_790_000_125_500,
                "transcript_object": [
                    {"role": "agent", "content": "Hallo"},
                    {"role": "user", "content": "Parkplatz?"},
                ],
                "call_cost": {"combined_cost": 12.5},
                "disconnection_reason": "user_hangup",
            },
        }
    )
    assert event is not None and event.kind == "call_ended" and event.end is not None
    assert event.start.agent_id == "agent_1" and event.start.from_number == "+4915120000009"
    assert event.end.duration_s == 125 and event.end.transcript[1] == ("user", "Parkplatz?")
    assert event.end.provider_cost_micro_eur == 125_000
    assert event.end.disconnect_reason == "user_hangup"


def test_retell_agent_payload_uses_profile_values_only() -> None:
    from tests.unit.test_profile import SAMPLE

    from concierge.tenants.profile import TenantProfile

    profile = TenantProfile.model_validate(
        dict(SAMPLE, voice={"provider": "retell", "voice_id": "v1", "number": "+4969000009"})
    )
    payload = retell.agent_payload(
        profile, websocket_url="wss://x/voice/retell/llm/pk/{call_id}", webhook_url="https://x/w"
    )
    assert payload["response_engine"] == {
        "type": "custom-llm",
        "llm_websocket_url": "wss://x/voice/retell/llm/pk/{call_id}",
    }
    assert payload["voice_id"] == "v1" and payload["language"] == "de-DE"
    assert payload["begin_message"] == ""  # the server speaks the disclosure first


# ------------------------------------------------------------------ twilio
def test_twilio_signature_vectors_port_variants_and_body_hash() -> None:
    token = "12345"
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234"}
    expected = base64.b64encode(
        hmac.new(
            token.encode(),
            (url + "CallSidCA1234567890ABCDECaller+14158675310Digits1234").encode(),
            hashlib.sha1,
        ).digest()
    ).decode()
    assert twilio_cr.compute_signature(token, url, params) == expected
    assert twilio_cr.verify_signature(token, url, params, expected)
    assert twilio_cr.verify_signature(
        token, "https://mycompany.com:443/myapp.php?foo=1&bar=2", params, expected
    )
    assert not twilio_cr.verify_signature(token, url, params, expected[:-2] + "==")
    assert not twilio_cr.verify_signature(token, url, dict(params, Digits="1235"), expected)
    assert not twilio_cr.verify_signature("", url, params, expected)
    assert not twilio_cr.verify_signature(token, url, params, None)

    body = b'{"a":1}'
    digest = hashlib.sha256(body).hexdigest()
    json_url = f"https://mycompany.com/hook?bodySHA256={digest}"
    signature = twilio_cr.compute_signature(token, json_url, {})
    assert twilio_cr.verify_signature(token, json_url, {}, signature, body=body)
    assert not twilio_cr.verify_signature(token, json_url, {}, signature, body=body + b" ")


def test_twiml_builders_escape_and_carry_attributes() -> None:
    twiml = twilio_cr.relay_twiml(
        websocket_url="wss://h/voice/twilio/relay/pk&x",
        action_url="https://h/voice/twilio/handoff",
        language="en",
        status_callback="https://h/voice/twilio/status",
    )
    assert twiml.startswith('<?xml version="1.0" encoding="UTF-8"?><Response><Connect')
    assert 'url="wss://h/voice/twilio/relay/pk&amp;x"' in twiml
    assert 'language="en-US"' in twiml and 'ttsLanguage="en-US"' in twiml
    assert 'action="https://h/voice/twilio/handoff"' in twiml
    assert twilio_cr.dial_twiml("+49 69 <1>") == (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Dial>+49 69 &lt;1&gt;</Dial></Response>'
    )
    assert (
        twilio_cr.hangup_twiml()
        == '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    )
    assert '<Say language="de-DE">Tsch&amp;' in twilio_cr.hangup_twiml("Tsch&", language="de")
    assert twilio_cr.parse_handoff_data('{"transfer": "+4969000000"}') == {
        "transfer": "+4969000000"
    }
    assert (
        twilio_cr.parse_handoff_data("not json") == {} and twilio_cr.parse_handoff_data(None) == {}
    )


# ------------------------------------------------------------------ speech
@pytest.mark.parametrize(
    ("lang", "text", "expected"),
    [
        (
            "de",
            "Check-in ab 15:00 Uhr, Frühstück 07:00-10:00.",
            "Check-in ab 15 Uhr, Frühstück 7 Uhr-10 Uhr.",
        ),
        (
            "en",
            "Check-in from 15:00, breakfast at 07:30.",
            "Check-in from 3 pm, breakfast at 7:30 am.",
        ),
        ("it", "Check-in dalle 15:00.", "Check-in dalle le 15."),
        ("tr", "Giriş 15:00.", "Giriş saat 15."),
        ("ru", "Заезд с 15:00, завтрак 07:30.", "Заезд с 15 часов, завтрак 7:30."),
        ("uk", "Заїзд з 15:00.", "Заїзд з 15 годин."),
    ],
)
def test_times_are_spoken_per_language(lang: str, text: str, expected: str) -> None:
    assert for_speech(text, lang) == expected


def test_speech_strips_lists_urls_markup_and_limits_sentences() -> None:
    text = (
        "**Parken:** Die Garage kostet EUR 18 pro Nacht.\n"
        "- Einfahrt 2,10 m\n- Reservierung: https://example.invalid/p oder mail@example.invalid\n"
        "Dritter Satz. Vierter Satz."
    )
    spoken = for_speech(text, "de", website="auf unserer Website")
    assert spoken == "Parken: Die Garage kostet 18 Euro pro Nacht. Einfahrt 2,10 m."
    assert "http" not in spoken and "@" not in spoken and "*" not in spoken
    assert for_speech("One. Two. Three.", "en") == "One. Two."
    assert for_speech("Costs 1 EUR.", "en") == "Costs 1 euro."
    assert for_speech("Стоит 18 €.", "ru") == "Стоит 18 евро."
    assert spoken_time(0, 5, "en") == "12:05 am" and spoken_time(12, 0, "en") == "12 pm"


def test_decline_phrases_match_at_word_boundaries() -> None:
    phrases = VoicePhrases.load()
    assert phrases.declines("Ich bin damit nicht einverstanden.", "de")
    assert phrases.declines("I do not agree to this", "en")
    assert phrases.declines("Я не согласен", "ru")
    assert phrases.declines("Kabul etmiyorum", "tr")
    assert not phrases.declines("Wir kommen mit zwei Kindern, keine Kinderbetten nötig", "de")
    assert not phrases.declines("Haben Sie einen Parkplatz?", "de")
    assert phrases.declines("Bitte keine KI, ich will einen Menschen", "de")


def test_summary_and_billing_helpers() -> None:
    text = summary_de(
        lang="de",
        duration_s=125,
        transcript=[("agent", "Hallo"), ("user", "Haben Sie Parkplätze?"), ("agent", "Ja.")],
        outcome="completed",
    )
    assert (
        text
        == "Anruf (de), 3 Min., 1 Anliegen. Erstes Anliegen: „Haben Sie Parkplätze?“. Ergebnis: beantwortet."
    )
    assert "Erstes Anliegen: „-“" in summary_de(
        lang="en", duration_s=0, transcript=[], outcome="declined"
    )
    assert billable_seconds(125, "second") == 125
    assert billable_seconds(125, "minute") == 180
    assert billable_seconds(0, "minute") == 0
