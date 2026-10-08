from types import SimpleNamespace

from telegram.error import BadRequest

from app.telegram.callbacks import CallbackRouter
from app.telegram.dashboard import AlertManager, DashboardManager
from app.telegram.handlers import Handlers
from app.telegram.keyboards import Screen
from tests.conftest import ADMIN_ID


class FakeBot:
    def __init__(self):
        self.sent, self.edited, self.deleted = [], [], []
        self._id = 100

    async def send_message(self, chat_id, text, **kw):
        self._id += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._id)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        if self.edited and self.edited[-1][2] == text:
            raise BadRequest("Message is not modified")
        self.edited.append((chat_id, message_id, text))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


async def test_dashboard_edits_in_place(runtime):
    bot = FakeBot()
    dash = DashboardManager(bot, CallbackRouter(runtime, frozenset({ADMIN_ID})), 15)
    await dash.show(1, ADMIN_ID, Screen("a", route="home", refreshable=True), new=True)
    await dash.show(1, ADMIN_ID, Screen("b", route="status", refreshable=True))
    await dash.show(1, ADMIN_ID, Screen("b", route="status", refreshable=True))  # unchanged -> no error
    assert len(bot.sent) == 1 and len(bot.edited) == 1
    assert dash.chats[1].route == "status"


async def test_alerts_cooldown_and_clear(runtime):
    bot = FakeBot()
    dash = DashboardManager(bot, CallbackRouter(runtime, frozenset({ADMIN_ID})), 15)
    alerts = AlertManager(bot, dash, frozenset({ADMIN_ID}), cooldown_sec=120)
    await alerts.alert("strong_signal", "strong:X:UP", "🚨")
    await alerts.alert("strong_signal", "strong:X:UP", "🚨")  # within cooldown
    await alerts.alert("trade_opened", "open:1", "📝")
    assert len(bot.sent) == 2
    assert await dash.clear(ADMIN_ID) == 2


async def test_unauthorized_command_refused(runtime):
    replies = []
    msg = SimpleNamespace(text="/start", chat_id=5,
                          reply_text=lambda t, **kw: _record(replies, t))
    update = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=999))
    h = Handlers(CallbackRouter(runtime, frozenset({ADMIN_ID})), None)  # type: ignore[arg-type]
    await h.command(update, None)  # type: ignore[arg-type]
    assert "Unauthorized" in replies[0]


async def _record(lst, t):
    lst.append(t)


def test_html_guard_and_plain_fallback():
    from app.telegram.dashboard import _bad_html, _plain

    assert _bad_html("Best window: <1 min")
    assert not _bad_html("<b>Best</b> window: &lt;1 min")
    assert _plain("<b>x</b> &lt;1") == "x <1"


async def test_screens_escape_bucket_labels(runtime):
    """Regression: a '<1' time bucket / '<50' price bucket broke /menu (Telegram rejected the HTML)."""
    from datetime import timedelta

    from app.research.calibration import HistoricalModel
    from app.telegram import analytics_views as AV
    from app.telegram import messages as M
    from app.telegram.dashboard import _bad_html
    from tests.test_research import obs

    rows = [obs(i, minute=0, outcome="yes", price=45) for i in range(40)]
    for o in rows:
        o.time_remaining = 30  # "<1" minute bucket
        o.ts = o.close_time - timedelta(seconds=30)
    runtime.model = HistoricalModel(rows, min_samples=30)
    assert runtime.model.best("time") is not None
    for screen in (M.home(runtime), AV.analytics(runtime), AV.detailed(runtime), AV.calibration(runtime)):
        assert not _bad_html(screen.text), screen.text
