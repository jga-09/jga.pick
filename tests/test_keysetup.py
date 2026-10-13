"""/setkey: key messages are deleted at once, verified, stored privately, never echoed."""

from __future__ import annotations

import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.errors import KalshiAuthError
from app.kalshi.keystore import KeyStore, extract_pem, looks_like_secret
from app.telegram.keysetup import KeySetup

KEY_ID = "a1b2c3d4-1234-4abc-9def-0123456789ab"


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


class FakeMsg:
    def __init__(self, text: str, chat_type: str = "private") -> None:
        self.text, self.caption, self.document = text, None, None
        self.deleted = False
        self.sent: list[str] = []
        self.chat = SimpleNamespace(type=chat_type, send_message=self._send)

    async def _send(self, text: str, **_: object) -> None:
        self.sent.append(text)

    async def delete(self) -> None:
        self.deleted = True


class FakeRT:
    def __init__(self, tmp_path, fail: bool = False) -> None:
        self.store = KeyStore(tmp_path / "kalshi")
        self.fail = fail
        self.set_kalshi_key = AsyncMock(side_effect=self._set)

    async def _set(self, key_id: str, pem: bytes) -> float:
        if self.fail:
            raise KalshiAuthError(401, "unauthorized")
        self.store.save(key_id, pem)
        return 12.34

    def clear_kalshi_key(self) -> bool:
        return self.store.clear()

    def kalshi_key_source(self) -> str:
        return "none"


def test_extract_pem_repairs_phone_paste():
    pem = _pem()
    squashed = pem.replace("\n", " ")
    fixed = extract_pem("here: " + squashed)
    assert fixed is not None
    serialization.load_pem_private_key(fixed, password=None)
    assert looks_like_secret(squashed) and not looks_like_secret("hello")


def test_keystore_permissions(tmp_path):
    ks = KeyStore(tmp_path / "kalshi")
    ks.save(KEY_ID, _pem().encode())
    assert ks.exists() and ks.load() is not None
    assert stat.S_IMODE(ks.pem_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(ks.dir.stat().st_mode) == 0o700
    assert ks.clear() and not ks.exists()


@pytest.mark.asyncio
async def test_setkey_flow_deletes_and_never_echoes(tmp_path):
    rt = FakeRT(tmp_path)
    ks = KeySetup(rt)  # type: ignore[arg-type]
    start = FakeMsg("/setkey")
    await ks.command(start, 1, "setkey")
    assert "Step 1" in start.sent[0]
    m1 = FakeMsg(KEY_ID)
    assert await ks.message(m1, 1)
    assert m1.deleted and "Step 2" in m1.sent[0]
    pem = _pem()
    m2 = FakeMsg(pem)
    assert await ks.message(m2, 1)
    assert m2.deleted
    assert any("Kalshi connected" in t for t in m2.sent)
    body = pem.splitlines()[1]
    assert all(body not in t and KEY_ID not in t for t in m1.sent + m2.sent)
    assert rt.store.exists()
    assert not await ks.message(FakeMsg("hi"), 1)  # flow finished


@pytest.mark.asyncio
async def test_setkey_rejected_saves_nothing(tmp_path):
    rt = FakeRT(tmp_path, fail=True)
    ks = KeySetup(rt)  # type: ignore[arg-type]
    m = FakeMsg(f"/setkey {KEY_ID}\n{_pem()}")
    await ks.command(m, 1, "setkey")
    assert m.deleted and any("Not connected" in t for t in m.sent)
    assert not rt.store.exists()


@pytest.mark.asyncio
async def test_group_chat_refused_and_stray_key_scrubbed(tmp_path):
    ks = KeySetup(FakeRT(tmp_path))  # type: ignore[arg-type]
    g = FakeMsg("/setkey", chat_type="group")
    await ks.command(g, 1, "setkey")
    assert "private chat" in g.sent[0]
    stray = FakeMsg(_pem())
    assert await ks.scrub(stray) and stray.deleted


def test_missing_key_file_is_a_warning_in_paper(tmp_path):
    from tests.conftest import make_settings

    s = make_settings(data_source="kalshi", kalshi_api_key_id=KEY_ID, kalshi_private_key_path="/nope.pem",
                      kalshi_key_dir=tmp_path / "kalshi")
    warnings = s.validate_runtime()
    assert any("not a readable file" in w for w in warnings)
    assert not s.has_kalshi_credentials


def test_live_needs_a_key(tmp_path):
    from app.errors import ConfigError
    from tests.conftest import make_settings

    s = make_settings(data_source="kalshi", live_trading=True, paper_trading=False,
                      kalshi_key_dir=tmp_path / "kalshi")
    with pytest.raises(ConfigError):
        s.validate_runtime()
    KeyStore(tmp_path / "kalshi").save(KEY_ID, _pem().encode())
    assert s.has_kalshi_credentials


async def test_runtime_rejected_key_restores_old_signer(runtime, tmp_path):
    runtime.settings = runtime.settings.model_copy(update={"kalshi_key_dir": tmp_path / "k"})
    runtime.fixture_mode = False
    client = SimpleNamespace(signer=None, auth_failed=False,
                             get_balance=AsyncMock(side_effect=KalshiAuthError(401, "no")))
    runtime.client = client
    with pytest.raises(KalshiAuthError):
        await runtime.set_kalshi_key(KEY_ID, _pem().encode())
    assert client.signer is None and not KeyStore(tmp_path / "k").exists()
    client.get_balance = AsyncMock(return_value={"balance": 1234})
    assert await runtime.set_kalshi_key(KEY_ID, _pem().encode()) == 12.34
    assert client.signer is not None and KeyStore(tmp_path / "k").exists()
