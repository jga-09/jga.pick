"""Project-wide exception hierarchy."""


class BotError(Exception):
    """Base class for all bot errors."""


class ConfigError(BotError):
    """Configuration is missing or invalid; the bot must not start."""


class KalshiError(BotError):
    """Base class for Kalshi API errors."""


class KalshiHTTPError(KalshiError):
    def __init__(self, status_code: int, message: str, path: str = "") -> None:
        super().__init__(f"HTTP {status_code} on {path}: {message}")
        self.status_code = status_code
        self.path = path


class KalshiAuthError(KalshiHTTPError):
    """401/403 from Kalshi, or a missing/invalid signing key."""


class KalshiRateLimitError(KalshiHTTPError):
    def __init__(self, message: str, path: str = "", retry_after: float | None = None) -> None:
        super().__init__(429, message, path)
        self.retry_after = retry_after


class KalshiTransientError(KalshiError):
    """Timeouts, connection resets and 5xx responses - safe to retry for reads."""


class MalformedResponseError(KalshiError):
    """The API returned data that does not match the documented schema."""


class TradingError(BotError):
    """Base class for execution errors."""


class TradingDisabledError(TradingError):
    """Trading is globally disabled (emergency stop, paused, stopped)."""


class LiveTradingDisabledError(TradingError):
    """A live order was attempted while live trading is not fully enabled."""


class DuplicateOrderError(TradingError):
    """An identical order is already in flight or was already executed."""


class OrderRejectedError(TradingError):
    """The risk manager or exchange rejected the order."""


class InvalidCallbackError(BotError):
    """Telegram callback data failed validation."""
