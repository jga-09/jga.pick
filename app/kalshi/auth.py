"""Kalshi API-key request signing.

Per the official Kalshi SDK/docs: sign ``timestamp_ms + METHOD + path`` (path
includes ``/trade-api/v2`` and excludes the query string) using RSA-PSS with
SHA-256 (MGF1-SHA256, salt length = digest length), or Ed25519 for Ed25519
keys. Send the base64 signature in ``KALSHI-ACCESS-SIGNATURE`` together with
``KALSHI-ACCESS-KEY`` and ``KALSHI-ACCESS-TIMESTAMP``.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.errors import KalshiAuthError


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: bytes) -> None:
        try:
            key = serialization.load_pem_private_key(private_key_pem, password=None)
        except (ValueError, TypeError) as exc:
            # Never include key material in the error.
            raise KalshiAuthError(0, "could not load Kalshi private key (invalid PEM)") from exc
        if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)):
            raise KalshiAuthError(0, "Kalshi private key must be RSA or Ed25519")
        self.key_id = key_id
        self._key = key

    @classmethod
    def from_file(cls, key_id: str, path: str | Path) -> KalshiSigner:
        p = Path(path)
        if not p.is_file():
            raise KalshiAuthError(0, "KALSHI_PRIVATE_KEY_PATH is not a readable file")
        return cls(key_id, p.read_bytes())

    def __repr__(self) -> str:  # never leak key material through repr()
        return f"KalshiSigner(key_id={self.key_id[:4]}...)"

    def _sign(self, message: bytes) -> bytes:
        if isinstance(self._key, Ed25519PrivateKey):
            return self._key.sign(message)
        return self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def headers(self, method: str, url_or_path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        path = urlparse(url_or_path).path if url_or_path.startswith(("http", "ws")) else url_or_path
        path = path.split("?")[0]
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        sig = self._sign((ts + method.upper() + path).encode())
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
