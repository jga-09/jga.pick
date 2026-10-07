import logging

from app.utils.logging import RedactingFormatter, redact, register_secret


def test_redacts_telegram_token_and_keys():
    token = "1234567890:AAH" + "x" * 32
    assert token not in redact(f"url https://api.telegram.org/bot{token}/getMe")
    pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    assert "abc" not in redact(pem)
    register_secret("supersecretvalue")
    rec = logging.LogRecord("x", logging.INFO, "", 0, "value=supersecretvalue", None, None)
    assert "supersecretvalue" not in RedactingFormatter().format(rec)
