"""LOW / MEDIUM / HIGH risk profiles.

Defaults below can be overridden with ``LOW_*``, ``MEDIUM_*`` and ``HIGH_*``
environment variables (e.g. ``LOW_MIN_CONFIDENCE=85``). Every profile - HIGH
included - is bounded by the hard limits enforced in :class:`RiskProfile`.
Risk level is independent of paper/live mode.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from enum import StrEnum
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigError


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def emoji(self) -> str:
        return {"low": "🟢", "medium": "🟡", "high": "🔴"}[self.value]

    @property
    def title(self) -> str:  # type: ignore[override]
        return self.value.upper()

    @property
    def badge(self) -> str:
        return f"{self.emoji} {self.title}"


# Hard ceilings no profile or Telegram customisation can exceed.
HARD_LIMITS = {
    "min_confidence": (50, 99),
    "max_position_usd": (1.0, 250.0),
    "max_open_positions": (1, 10),
    "max_daily_loss_usd": (1.0, 500.0),
    "min_time_remaining_sec": (30, 840),
    "max_spread_cents": (1, 20),
    "max_total_exposure_usd": (1.0, 1000.0),
    "min_liquidity_contracts": (1, 10_000),
    "loss_cooldown_sec": (0, 7200),
    "trade_cooldown_sec": (0, 3600),
    "min_entry_price_cents": (1, 50),
    "max_entry_price_cents": (50, 99),
    "max_balance_fraction": (0.001, 0.5),
    "min_signal_quality": (0, 100),
    "min_stability": (0.0, 1.0),
    "max_soft_flags": (0, 10),
    "min_ev_cents": (-5.0, 50.0),
    # BTC decision engine (STRATEGY_MODE=btc)
    "min_btc_strength": (0, 100),
    "max_prob_width": (0.02, 0.6),
    "max_late_move_cents": (3.0, 60.0),
    "max_consecutive_losses": (1, 20),
    "streak_pause_sec": (0, 86_400),
    "kelly_fraction": (0.01, 0.5),
}


_INT_FIELDS = {"min_confidence", "max_open_positions", "min_time_remaining_sec", "loss_cooldown_sec",
               "trade_cooldown_sec", "min_signal_quality", "max_soft_flags", "min_btc_strength",
               "max_consecutive_losses", "streak_pause_sec"}


@dataclass(frozen=True)
class RiskProfile:
    level: RiskLevel
    min_confidence: int
    max_position_usd: float
    max_open_positions: int
    max_daily_loss_usd: float
    min_time_remaining_sec: int
    max_spread_cents: float
    max_total_exposure_usd: float
    min_liquidity_contracts: float
    loss_cooldown_sec: int
    trade_cooldown_sec: int
    min_entry_price_cents: float
    max_entry_price_cents: float
    max_balance_fraction: float
    # Setup-quality gates
    min_signal_quality: int = 75
    allowed_grades: str = "A+,A,B"
    min_stability: float = 0.5
    max_soft_flags: int = 1
    min_ev_cents: float = 1.0  # minimum estimated EV per contract after costs (when data exists)
    require_known_ev: bool = False  # block paper trades until history supports an EV estimate / calibration
    # BTC decision engine (STRATEGY_MODE=btc). Defaults are a-priori starting points, not fitted values.
    min_btc_strength: int = 45  # 0-100 agreement of BTC evidence with the trade direction
    max_prob_width: float = 0.20  # widest acceptable model probability range (uncertainty)
    blocked_regimes: str = "HIGH_VOLATILITY,INSUFFICIENT_DATA"
    max_late_move_cents: float = 15.0  # our side already up this much in 5 min -> needs 2x the edge
    max_consecutive_losses: int = 3  # pause new trades after this many losses in a row ...
    streak_pause_sec: int = 3600  # ... for this long after the last loss
    kelly_fraction: float = 0.2  # cap stake at this fraction of full Kelly (on the conservative probability)

    @property
    def regimes_blocked(self) -> frozenset[str]:
        return frozenset(r.strip().upper() for r in self.blocked_regimes.split(",") if r.strip())

    @property
    def grades(self) -> frozenset[str]:
        return frozenset(g.strip().upper() for g in self.allowed_grades.split(",") if g.strip())

    def __post_init__(self) -> None:
        for f in fields(self):
            if f.name in HARD_LIMITS:
                cast = int if f.name in _INT_FIELDS else float
                object.__setattr__(self, f.name, cast(getattr(self, f.name)))
        for name, (lo, hi) in HARD_LIMITS.items():
            v = getattr(self, name)
            if not lo <= v <= hi:
                raise ConfigError(f"{self.level.value.upper()}_{name.upper()}={v} outside safe range [{lo}, {hi}]")
        if self.max_total_exposure_usd < self.max_position_usd:
            object.__setattr__(self, "max_total_exposure_usd", self.max_position_usd)

    def with_overrides(self, overrides: dict[str, Any]) -> RiskProfile:
        valid = {f.name for f in fields(self)} - {"level"}
        clean = {k: _cast(k, v) for k, v in overrides.items() if k in valid}
        return replace(self, **clean)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["level"] = self.level.value
        return d


def _cast(name: str, v: Any) -> Any:
    if name in ("allowed_grades", "blocked_regimes"):
        return str(v)
    if name == "require_known_ev":
        return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
    return (int if name in _INT_FIELDS else float)(v)


DEFAULT_PROFILES: dict[RiskLevel, dict[str, Any]] = {
    RiskLevel.LOW: dict(
        min_confidence=80, max_position_usd=5, max_open_positions=1, max_daily_loss_usd=10,
        min_time_remaining_sec=180, max_spread_cents=5, max_total_exposure_usd=5,
        min_liquidity_contracts=20, loss_cooldown_sec=900, trade_cooldown_sec=300,
        min_entry_price_cents=35, max_entry_price_cents=85, max_balance_fraction=0.02,
        min_signal_quality=85, allowed_grades="A+,A", min_stability=0.8, max_soft_flags=0,
        min_ev_cents=3.0, require_known_ev=True,
        min_btc_strength=55, max_prob_width=0.15, max_late_move_cents=12,
        blocked_regimes="HIGH_VOLATILITY,RAPID_REVERSAL,INSUFFICIENT_DATA",
        max_consecutive_losses=2, streak_pause_sec=3600, kelly_fraction=0.10,
    ),
    RiskLevel.MEDIUM: dict(
        min_confidence=70, max_position_usd=15, max_open_positions=2, max_daily_loss_usd=30,
        min_time_remaining_sec=120, max_spread_cents=8, max_total_exposure_usd=30,
        min_liquidity_contracts=10, loss_cooldown_sec=600, trade_cooldown_sec=120,
        min_entry_price_cents=25, max_entry_price_cents=90, max_balance_fraction=0.05,
        min_signal_quality=75, allowed_grades="A+,A,B", min_stability=0.6, max_soft_flags=1,
        min_ev_cents=2.0, require_known_ev=True,
        min_btc_strength=45, max_prob_width=0.20, max_late_move_cents=15,
        blocked_regimes="HIGH_VOLATILITY,INSUFFICIENT_DATA",
        max_consecutive_losses=3, streak_pause_sec=2700, kelly_fraction=0.20,
    ),
    RiskLevel.HIGH: dict(
        min_confidence=60, max_position_usd=30, max_open_positions=3, max_daily_loss_usd=60,
        min_time_remaining_sec=60, max_spread_cents=12, max_total_exposure_usd=90,
        min_liquidity_contracts=5, loss_cooldown_sec=300, trade_cooldown_sec=60,
        min_entry_price_cents=15, max_entry_price_cents=95, max_balance_fraction=0.10,
        min_signal_quality=65, allowed_grades="A+,A,B", min_stability=0.5, max_soft_flags=2,
        min_ev_cents=1.0, require_known_ev=False,
        min_btc_strength=35, max_prob_width=0.25, max_late_move_cents=20,
        blocked_regimes="INSUFFICIENT_DATA",
        max_consecutive_losses=4, streak_pause_sec=1800, kelly_fraction=0.25,
    ),
}


class _ProfileEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    min_confidence: int | None = None
    max_position_usd: float | None = None
    max_open_positions: int | None = None
    max_daily_loss_usd: float | None = None
    min_time_remaining_sec: int | None = None
    max_spread_cents: float | None = None
    max_total_exposure_usd: float | None = None
    min_liquidity_contracts: float | None = None
    loss_cooldown_sec: int | None = None
    trade_cooldown_sec: int | None = None
    min_entry_price_cents: float | None = None
    max_entry_price_cents: float | None = None
    max_balance_fraction: float | None = None
    min_signal_quality: int | None = None
    allowed_grades: str | None = None
    min_stability: float | None = None
    max_soft_flags: int | None = None
    min_ev_cents: float | None = None
    require_known_ev: bool | None = None
    min_btc_strength: int | None = None
    max_prob_width: float | None = None
    blocked_regimes: str | None = None
    max_late_move_cents: float | None = None
    max_consecutive_losses: int | None = None
    streak_pause_sec: int | None = None
    kelly_fraction: float | None = None


def load_profiles(read_env: bool = True) -> dict[RiskLevel, RiskProfile]:
    profiles: dict[RiskLevel, RiskProfile] = {}
    for level, defaults in DEFAULT_PROFILES.items():
        values = dict(defaults)
        if read_env:
            env = _ProfileEnv(_env_prefix=f"{level.value.upper()}_")  # type: ignore[call-arg]
            values.update({k: v for k, v in env.model_dump().items() if v is not None})
        profiles[level] = RiskProfile(level=level, **values)
    return profiles
