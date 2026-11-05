"""Per-user accounts: each allowed Telegram user trades their own bankroll with their own settings.

Shared by everyone: the market scanner, signals, research data, the bot on/off switch, the global pause and the
EMERGENCY STOP. Per user: risk / Kelly mode, auto trade, paper or live mode, risk overrides, paper bankroll,
positions, trades, daily P&L and limits, tickets and their own Kalshi API key.

The first id in TELEGRAM_ADMIN_IDS is the *owner*: it keeps the single-account data and key that existed before
multi-user support, and its settings live in the original state record, so upgrading changes nothing for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.state import BotState, StateStore

# Bot-wide switches: one value for every user (safety controls must never be per-user).
GLOBAL_FIELDS = frozenset({"running", "paused", "emergency_stop", "focus_asset"})


class AccountStateView:
    """Duck-types BotState: bot-wide fields from the global store, everything else from the user's own store."""

    def __init__(self, glob: StateStore, user: StateStore) -> None:
        object.__setattr__(self, "_glob", glob)
        object.__setattr__(self, "_user", user)

    def _src(self, name: str) -> BotState:
        return self._glob.state if name in GLOBAL_FIELDS else self._user.state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._src(name), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._src(name), name, value)

    @property
    def trading_allowed(self) -> bool:
        return self._glob.state.trading_allowed

    @property
    def disabled_reason(self) -> str:
        return self._glob.state.disabled_reason

    def to_dict(self) -> dict[str, Any]:
        out = self._user.state.to_dict()
        out.update({k: v for k, v in self._glob.state.to_dict().items() if k in GLOBAL_FIELDS})
        return out


class AccountStore:
    """Duck-types StateStore for one user (update() routes each field to the right store)."""

    def __init__(self, glob: StateStore, user: StateStore) -> None:
        self.glob = glob
        self.user = user
        self.repo = glob.repo
        self.state = AccountStateView(glob, user)

    def save(self) -> None:
        self.glob.save()
        if self.user is not self.glob:
            self.user.save()

    def update(self, **changes: Any) -> AccountStateView:
        g = {k: v for k, v in changes.items() if k in GLOBAL_FIELDS}
        u = {k: v for k, v in changes.items() if k not in GLOBAL_FIELDS}
        if g:
            self.glob.update(**g)
        if u:
            self.user.update(**u)
        return self.state


@dataclass
class Account:
    """Everything that belongs to one Telegram user."""

    user_id: int
    repo: Any  # AccountRepo
    store: AccountStore
    portfolio: Any
    paper: Any
    live: Any
    executor: Any
    tickets: Any
    client: Any  # Kalshi client signing with THIS user's key (the owner shares the bot's client)
    key_store: Any
    is_owner: bool = False

    @property
    def state(self) -> AccountStateView:
        return self.store.state
