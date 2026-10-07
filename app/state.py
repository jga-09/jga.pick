"""Runtime control state (persisted in the `settings` table)."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from app.risk.profiles import RiskLevel
from app.utils.logging import log_event

log = logging.getLogger(__name__)
STATE_KEY = "bot_state"


@dataclass
class BotState:
    running: bool = False
    paused: bool = False
    emergency_stop: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    auto_trade: bool = False
    mode: str = "paper"  # "paper" | "live"
    focus_asset: str | None = None
    risk_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def trading_allowed(self) -> bool:
        return self.running and not self.paused and not self.emergency_stop

    @property
    def disabled_reason(self) -> str:
        if self.emergency_stop:
            return "EMERGENCY STOP active"
        if not self.running:
            return "bot stopped"
        if self.paused:
            return "trading paused"
        return ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["risk_level"] = self.risk_level.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, defaults: BotState) -> BotState:
        if not d:
            return defaults
        st = cls(
            running=False,  # never auto-resume trading loops on restart
            paused=bool(d.get("paused", False)),
            emergency_stop=bool(d.get("emergency_stop", False)),
            risk_level=RiskLevel(d.get("risk_level", defaults.risk_level.value)),
            auto_trade=bool(d.get("auto_trade", defaults.auto_trade)),
            mode=d.get("mode", defaults.mode) if d.get("mode") in ("paper", "live") else "paper",
            focus_asset=d.get("focus_asset"),
            risk_overrides=d.get("risk_overrides") or {},
        )
        return st


class StateStore:
    def __init__(self, repo: Any, defaults: BotState) -> None:
        self.repo = repo
        self.state = BotState.from_dict(repo.get_setting(STATE_KEY), defaults)

    def save(self) -> None:
        self.repo.set_setting(STATE_KEY, self.state.to_dict())

    def update(self, **changes: Any) -> BotState:
        for k, v in changes.items():
            setattr(self.state, k, v)
        self.save()
        log_event(log, "STATE_CHANGED", **{k: getattr(v, "value", v) for k, v in changes.items()})
        return self.state
