"""Kalshi API key stored on the bot's own disk (set from Telegram with /setkey).

The key is written to the data volume with owner-only permissions and is never
logged, echoed or returned to Telegram. Environment variables
(KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH) still take precedence when they
point to a readable file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.kalshi.auth import KalshiSigner

KEY_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PEM_RE = re.compile(r"(-----BEGIN ([A-Z ]*)PRIVATE KEY-----)(.+?)(-----END \2PRIVATE KEY-----)", re.S)


def looks_like_secret(text: str | None) -> bool:
    """True for anything that resembles private-key material (always deleted from chat)."""
    return bool(text) and ("PRIVATE KEY" in text or "BEGIN RSA" in text)  # type: ignore[operator]


def extract_pem(text: str) -> bytes | None:
    """Pull a PEM private key out of pasted text, repairing line breaks phones turn into spaces."""
    m = PEM_RE.search(text)
    if not m:
        return None
    body = re.sub(r"\s+", "", m.group(3))
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"{m.group(1)}\n{lines}\n{m.group(4)}\n".encode()


class KeyStore:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.key_id_file = self.dir / "key_id"
        self.pem_file = self.dir / "private_key.pem"

    def exists(self) -> bool:
        return self.key_id_file.is_file() and self.pem_file.is_file()

    def load(self) -> KalshiSigner | None:
        if not self.exists():
            return None
        return KalshiSigner(self.key_id_file.read_text().strip(), self.pem_file.read_bytes())

    def save(self, key_id: str, pem: bytes) -> KalshiSigner:
        signer = KalshiSigner(key_id, pem)  # validates before anything is written
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        for path, data in ((self.pem_file, pem), (self.key_id_file, key_id.encode())):
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return signer

    def clear(self) -> bool:
        found = False
        for path in (self.pem_file, self.key_id_file):
            if path.exists():
                path.unlink()
                found = True
        return found
