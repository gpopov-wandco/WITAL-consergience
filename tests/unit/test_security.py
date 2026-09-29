"""Security foundation: crypto, signatures, SSRF guard, limiter, passwords, sessions, links."""

from __future__ import annotations

import base64
import json

import httpx2
import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from concierge.security import signatures as sig
from concierge.security.crypto import CryptoError, Keyring, generate_master_key, normalise
from concierge.security.limiter import MovingWindowLimiter
from concierge.security.links import LinkError, make_token, read_token
from concierge.security.passwords import WeakPasswordError, hash_password, verify_password
from concierge.security.sessions import csrf_ok, csrf_token, hash_token, new_session_token
from concierge.security.ssrf import SafeFetcher, SSRFError, is_public_address, validate_url

pytestmark = pytest.mark.feature("F31")


class TestCrypto:
    def test_round_trip_and_aad_binding(self, keyring: Keyring) -> None:
        blob = keyring.encrypt_str("+49 151 12345678", "ten_a", "phone")
        assert keyring.decrypt_str(blob, "ten_a", "phone") == "+49 151 12345678"
        with pytest.raises(CryptoError):
            keyring.decrypt_str(blob, "ten_b", "phone")
        with pytest.raises(CryptoError):
            keyring.decrypt_str(blob, "ten_a", "email")

    def test_tamper_detected(self, keyring: Keyring) -> None:
        blob = bytearray(keyring.encrypt(b"secret", "t", "n"))
        blob[-1] ^= 0x01
        with pytest.raises(CryptoError):
            keyring.decrypt(bytes(blob), "t", "n")

    def test_nonce_is_random(self, keyring: Keyring) -> None:
        assert keyring.encrypt(b"x", "t", "n") != keyring.encrypt(b"x", "t", "n")

    def test_rotation(self) -> None:
        k_old = base64.b64decode(generate_master_key())
        k_new = base64.b64decode(generate_master_key())
        old = Keyring({"k1": k_old}, "k1")
        blob = old.encrypt(b"payload", "t", "n")
        both = Keyring({"k1": k_old, "k2": k_new}, "k2")
        rotated = both.rotate(blob, "t", "n")
        assert both.key_id_of(rotated) == "k2"
        assert both.decrypt(rotated, "t", "n") == b"payload"
        new_only = Keyring({"k2": k_new}, "k2")
        with pytest.raises(CryptoError, match="unknown key id"):
            new_only.decrypt(blob, "t", "n")

    def test_blind_index_normalises(self, keyring: Keyring) -> None:
        a = keyring.blind_index("+49 151 1234 5678", "t", "phone")
        b = keyring.blind_index("004915112345678", "t", "phone")
        assert a != b  # leading 00 is not normalised to +: E.164 normalisation is the caller's job
        assert keyring.blind_index("+49 151 1234 5678", "t", "phone") == keyring.blind_index(
            "+4915112345678", "t", "phone"
        )
        assert keyring.blind_index("Guest@Example.com", "t", "email") == keyring.blind_index(
            "guest@example.com ", "t", "email"
        )
        assert keyring.blind_index("x", "t1", "email") != keyring.blind_index("x", "t2", "email")

    def test_phone_ref_is_short_hash(self, keyring: Keyring) -> None:
        ref = keyring.phone_ref("+4915112345678")
        assert ref.startswith("ph_") and len(ref) == 13
        assert "1511" not in ref

    def test_keyring_rejects_bad_keys(self) -> None:
        with pytest.raises(CryptoError):
            Keyring({"k1": b"short"}, "k1")
        with pytest.raises(CryptoError):
            Keyring({"k1": b"\x00" * 32}, "missing")

    def test_normalise_name(self) -> None:
        assert normalise("  Müller   Anna ", "name") == "müller anna"

    @given(st.binary(max_size=200), st.text(min_size=1, max_size=10).filter(lambda s: "|" not in s))
    @h_settings(max_examples=50, deadline=None)
    def test_property_round_trip(self, payload: bytes, name: str) -> None:
        ring = Keyring({"k": b"\x02" * 32}, "k")
        assert ring.decrypt(ring.encrypt(payload, "t", name), "t", name) == payload


class TestSignatures:
    def test_meta_matches_one_of_several_secrets(self) -> None:
        raw = b'{"object":"whatsapp_business_account"}'
        header = sig.sign_meta(raw, "secret-b")
        assert sig.meta_signature_ok(raw, header, ["secret-a", "secret-b"]) == 1
        assert sig.meta_signature_ok(raw, header, ["secret-a"]) is None
        assert sig.meta_signature_ok(raw, "sha256=zz", ["secret-b"]) is None
        assert sig.meta_signature_ok(raw, None, ["secret-b"]) is None

    @given(st.binary(min_size=1, max_size=64), st.integers(min_value=0, max_value=63))
    @h_settings(max_examples=60, deadline=None)
    def test_property_one_byte_mutation_fails(self, raw: bytes, position: int) -> None:
        header = sig.sign_meta(raw, "s")
        mutated = bytearray(raw)
        mutated[position % len(raw)] ^= 0x01
        assert sig.meta_signature_ok(bytes(mutated), header, ["s"]) is None
        h = bytearray(header.encode())
        idx = 7 + (position % 64)
        h[idx] = ord("0") if chr(h[idx]) != "0" else ord("1")
        assert sig.meta_signature_ok(raw, h.decode(), ["s"]) is None

    def test_retell_raw_and_compact(self) -> None:
        payload = {"event": "call_ended", "call": {"call_id": "c1"}}
        raw = json.dumps(payload, indent=2).encode()
        compact = json.dumps(payload, separators=(",", ":")).encode()
        now = 1_700_000_000_000
        header = sig.sign_retell(compact, "key", now)
        assert sig.retell_signature_ok(raw, header, "key", now)
        assert sig.retell_signature_ok(compact, header, "key", now)
        assert not sig.retell_signature_ok(raw, header, "other", now)
        assert not sig.retell_signature_ok(raw, header, "key", now + 6 * 60 * 1000)
        assert not sig.retell_signature_ok(raw, "v=1,d=zz", "key", now)

    def test_twilio_with_and_without_port(self) -> None:
        params = {"CallSid": "CA1", "From": "+4930123", "Digits": ["1", "2"]}
        url = "https://example.invalid/voice/twilio"
        header = sig.sign_twilio(url, params, "tok")
        assert sig.twilio_signature_ok(url, params, header, "tok")
        assert sig.twilio_signature_ok(
            "https://example.invalid:443/voice/twilio", params, header, "tok"
        )
        assert not sig.twilio_signature_ok(url, {**params, "Digits": ["1"]}, header, "tok")
        assert not sig.twilio_signature_ok(url, params, header, "nope")

    def test_twilio_json_body_hash(self) -> None:
        body = b'{"a":1}'
        import hashlib

        url = "https://example.invalid/hook?bodySHA256=" + hashlib.sha256(body).hexdigest()
        header = sig.sign_twilio(url, None, "tok")
        assert sig.twilio_signature_ok(url, {}, header, "tok", body=body)
        assert not sig.twilio_signature_ok(url, {}, header, "tok", body=b'{"a":2}')

    def test_stripe_and_reservation_scheme(self) -> None:
        raw = b'{"res_id":"R1"}'
        header = sig.sign_timestamped(raw, b"whsec", 1_700_000_000)
        assert sig.timestamped_signature_ok(raw, header, b"whsec", 1_700_000_100)
        assert not sig.timestamped_signature_ok(raw, header, b"whsec", 1_700_000_400)
        assert not sig.timestamped_signature_ok(raw + b" ", header, b"whsec", 1_700_000_000)
        multi = header + ",v1=" + "0" * 64
        assert sig.timestamped_signature_ok(raw, multi, b"whsec", 1_700_000_000)
        assert not sig.timestamped_signature_ok(raw, "garbage", b"whsec", 1_700_000_000)


class TestSSRF:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "::1",
            "fe80::1",
            "::ffff:10.0.0.1",
            "0.0.0.0",
            "224.0.0.1",
        ],
    )
    def test_private_blocked(self, address: str) -> None:
        assert not is_public_address(address)

    def test_public_allowed(self) -> None:
        assert is_public_address("93.184.216.34")
        assert is_public_address("2606:2800:220:1:248:1893:25c8:1946")

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://x.invalid/",
            "file:///etc/passwd",
            "http://user:pw@x.invalid/",
            "http://localhost/",
            "http:///nohost",
        ],
    )
    def test_url_rules(self, url: str) -> None:
        with pytest.raises(SSRFError):
            validate_url(url)

    async def test_resolver_pinning_and_private_rejection(self) -> None:
        async def resolver(host: str) -> list[str]:
            return ["10.0.0.5"]

        fetcher = SafeFetcher(
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200)), resolver=resolver
        )
        with pytest.raises(SSRFError, match="non-public"):
            await fetcher.fetch("https://hotel.invalid/")

    async def test_redirect_to_internal_blocked_and_cap(self) -> None:
        seen: list[str] = []

        async def resolver(host: str) -> list[str]:
            return {"hotel.invalid": ["93.184.216.34"], "intranet.invalid": ["192.168.0.10"]}[host]

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.headers["host"])
            if request.headers["host"] == "hotel.invalid" and request.url.path == "/":
                return httpx2.Response(302, headers={"location": "https://intranet.invalid/admin"})
            if request.url.path == "/loop":
                return httpx2.Response(302, headers={"location": "/loop"})
            return httpx2.Response(200, content=b"ok")

        fetcher = SafeFetcher(
            transport=httpx2.MockTransport(handler), resolver=resolver, max_redirects=2
        )
        with pytest.raises(SSRFError, match="non-public"):
            await fetcher.fetch("https://hotel.invalid/")
        assert seen == ["hotel.invalid"]
        with pytest.raises(SSRFError, match="too many redirects"):
            await fetcher.fetch("https://hotel.invalid/loop")

    async def test_size_cap_and_pinned_host_header(self) -> None:
        async def resolver(host: str) -> list[str]:
            return ["93.184.216.34"]

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert request.url.host == "93.184.216.34"
            assert request.headers["host"] == "hotel.invalid"
            assert request.extensions.get("sni_hostname") == "hotel.invalid"
            return httpx2.Response(200, content=b"x" * 2000, headers={"content-type": "text/html"})

        fetcher = SafeFetcher(
            transport=httpx2.MockTransport(handler), resolver=resolver, max_bytes=1000
        )
        with pytest.raises(SSRFError, match="larger"):
            await fetcher.fetch("https://hotel.invalid/page")
        ok = SafeFetcher(transport=httpx2.MockTransport(handler), resolver=resolver, max_bytes=5000)
        result = await ok.fetch("https://hotel.invalid/page")
        assert result.status == 200 and len(result.body) == 2000


class TestLimiterAndAuth:
    def test_moving_window(self) -> None:
        limiter = MovingWindowLimiter()
        assert all(limiter.hit("g1", "3/minute") for _ in range(3))
        assert not limiter.hit("g1", "3/minute")
        assert limiter.hit("g2", "3/minute")
        assert limiter.remaining("g1", "3/minute") == 0

    def test_passwords(self) -> None:
        h = hash_password("correct horse battery staple")
        assert h.startswith("$argon2id$")
        assert verify_password(h, "correct horse battery staple")
        assert not verify_password(h, "wrong")
        assert not verify_password("not-a-hash", "x")
        with pytest.raises(WeakPasswordError):
            hash_password("short")

    def test_session_and_csrf(self) -> None:
        token, stored = new_session_token()
        assert hash_token(token) == stored and token != stored
        key = b"k" * 32
        csrf = csrf_token(stored, key)
        assert csrf_ok(stored, key, csrf)
        flipped = csrf[:-1] + ("0" if csrf[-1] != "0" else "1")  # always a different digest
        assert not csrf_ok(stored, key, flipped)
        assert not csrf_ok(stored, b"other-key-other-key-other-key-00", csrf)
        assert not csrf_ok(stored, key, None)

    def test_links(self) -> None:
        key = b"s" * 32
        token = make_token({"user": "usr_1"}, "invite", key)
        assert read_token(token, "invite", key, 900) == {"user": "usr_1"}
        with pytest.raises(LinkError, match="invalid"):
            read_token(token, "reset", key, 900)
        with pytest.raises(LinkError, match="invalid"):
            read_token(token[:-2] + "zz", "invite", key, 900)
