"""Secrets and personal data at rest: AES-256-GCM with AAD, keyed blind indexes.

Implements: F31, C3

Blob format (bytes): ``0x01`` version, one byte key-id length, key id (UTF-8), 12-byte
random nonce, ciphertext with the 16-byte GCM tag. The additional authenticated data
is ``tenant_id|name|key_id`` so that a blob cannot be moved between tenants, fields or
keys. Blind indexes are HMAC-SHA256 over a normalised value under a key derived from
the active master key; ``concierge secrets rotate`` re-encrypts blobs and recomputes
indexes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import unicodedata

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from concierge.settings import Settings

BLOB_VERSION = 0x01
NONCE_BYTES = 12
KEY_BYTES = 32


class CryptoError(Exception):
    """Decryption or key handling failed. The message never contains key material."""


def generate_master_key() -> str:
    """A fresh base64-encoded 256-bit key."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def _aad(tenant_id: str, name: str, key_id: str) -> bytes:
    for part in (tenant_id, name, key_id):
        if "|" in part:
            raise CryptoError("AAD components must not contain '|'")
    return f"{tenant_id}|{name}|{key_id}".encode()


class Keyring:
    """Master keys by id plus the active id used for new encryptions."""

    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        if not keys:
            raise CryptoError("keyring is empty")
        for key_id, key in keys.items():
            if len(key) != KEY_BYTES:
                raise CryptoError(f"key {key_id!r} must be 32 bytes")
            if not key_id or len(key_id.encode("utf-8")) > 255 or "|" in key_id:
                raise CryptoError("key ids must be 1 to 255 bytes without '|'")
        if active_key_id not in keys:
            raise CryptoError("active key id is not in the keyring")
        self._keys = dict(keys)
        self.active_key_id = active_key_id
        self._index_key = HKDF(
            algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=b"gc-blind-index-v1"
        ).derive(keys[active_key_id])

    @classmethod
    def from_settings(cls, settings: Settings) -> Keyring:
        return cls(settings.master_keys(), settings.active_key_id())

    @property
    def key_ids(self) -> list[str]:
        return sorted(self._keys)

    # ----------------------------------------------------------------- encrypt
    def encrypt(self, plaintext: bytes, tenant_id: str, name: str) -> bytes:
        key_id = self.active_key_id
        nonce = os.urandom(NONCE_BYTES)
        aad = _aad(tenant_id, name, key_id)
        ciphertext = AESGCM(self._keys[key_id]).encrypt(nonce, plaintext, aad)
        key_bytes = key_id.encode("utf-8")
        return bytes([BLOB_VERSION, len(key_bytes)]) + key_bytes + nonce + ciphertext

    def decrypt(self, blob: bytes, tenant_id: str, name: str) -> bytes:
        key_id, nonce, ciphertext = self._split(blob)
        key = self._keys.get(key_id)
        if key is None:
            raise CryptoError(f"blob was encrypted with unknown key id {key_id!r}")
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, _aad(tenant_id, name, key_id))
        except Exception as exc:  # cryptography raises InvalidTag
            raise CryptoError(
                "decryption failed: wrong key, tenant, field name or tampered data"
            ) from exc

    def encrypt_str(self, text: str, tenant_id: str, name: str) -> bytes:
        return self.encrypt(text.encode("utf-8"), tenant_id, name)

    def decrypt_str(self, blob: bytes, tenant_id: str, name: str) -> str:
        return self.decrypt(blob, tenant_id, name).decode("utf-8")

    def key_id_of(self, blob: bytes) -> str:
        return self._split(blob)[0]

    def rotate(self, blob: bytes, tenant_id: str, name: str) -> bytes:
        """Re-encrypt under the active key (no-op when already current)."""
        if self.key_id_of(blob) == self.active_key_id:
            return blob
        return self.encrypt(self.decrypt(blob, tenant_id, name), tenant_id, name)

    @staticmethod
    def _split(blob: bytes) -> tuple[str, bytes, bytes]:
        if len(blob) < 2 or blob[0] != BLOB_VERSION:
            raise CryptoError("unsupported blob format")
        key_len = blob[1]
        start = 2 + key_len
        if len(blob) < start + NONCE_BYTES + 16:
            raise CryptoError("blob too short")
        key_id = blob[2:start].decode("utf-8", errors="strict")
        nonce = blob[start : start + NONCE_BYTES]
        return key_id, nonce, blob[start + NONCE_BYTES :]

    # ------------------------------------------------------------ blind index
    def blind_index(self, value: str, tenant_id: str, kind: str) -> str:
        """Deterministic keyed hash for equality lookups on encrypted columns."""
        material = f"{tenant_id}|{kind}|{normalise(value, kind)}".encode()
        return hmac.new(self._index_key, material, hashlib.sha256).hexdigest()[:40]

    def phone_ref(self, phone: str) -> str:
        """Truncated keyed hash of a phone number, the only form allowed in logs."""
        digest = hmac.new(self._index_key, normalise(phone, "phone").encode(), hashlib.sha256)
        return "ph_" + digest.hexdigest()[:10]


def normalise(value: str, kind: str) -> str:
    """Canonical form before hashing: NFKC, trimmed; e-mails lower-cased; phones digits only."""
    text = unicodedata.normalize("NFKC", value).strip()
    if kind == "email":
        return text.lower()
    if kind == "phone":
        digits = "".join(ch for ch in text if ch.isdigit())
        return "+" + digits if digits else ""
    if kind == "name":
        return " ".join(text.lower().split())
    return text


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
