"""Screen = text + inline keyboard; compact two-column layouts."""

from __future__ import annotations

from dataclasses import dataclass, field

Button = tuple[str, str]  # (label, callback_data)


@dataclass
class Screen:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    route: str = "home"  # callback data that re-renders this screen (for auto refresh)
    refreshable: bool = False


def grid(buttons: list[Button], cols: int = 2) -> list[list[Button]]:
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def to_markup(screen: Screen):  # type: ignore[no-untyped-def]
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not screen.buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in screen.buttons]
    )


MAIN_MENU: list[Button] = [
    ("🟢 Start", "start"), ("⚙️ Config", "cfg"),
    ("🛑 Stop", "stop"), ("📊 Status", "status"),
    ("🎯 Signals", "signals"), ("💰 Trades", "trades:0"),
    ("🛡 Risk", "risk"), ("📜 History", "hist:today"),
    ("🧠 Strategy", "strat"), ("🧹 Clear", "clear"),
]

BACK: Button = ("🔙 Back", "home")
