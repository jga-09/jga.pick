import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx

from app.research.weather import load_weather
from app.research.weather_download import download_all


class FakeKalshi:
    def __init__(self):
        now = datetime.now(UTC)
        d = (now - timedelta(days=3)).date()
        tag = f"{d:%y}{d.strftime('%b').upper()}{d:%d}"
        self.ev = f"KXHIGHNY-{tag}"
        self.open = (now - timedelta(days=4)).isoformat()
        self.close = (now - timedelta(days=2)).isoformat()
        self.calls = []

    async def get_markets(self, series_ticker=None, **kw):
        if series_ticker != "KXHIGHNY":
            return []
        return [{"ticker": f"{self.ev}-B70.5", "event_ticker": self.ev, "result": "yes", "strike_type": "between",
                 "floor_strike": 70, "cap_strike": 71, "yes_sub_title": "70° to 71°",
                 "open_time": self.open, "close_time": self.close}]

    async def get(self, path, params=None):
        self.calls.append((path, params))
        if path.startswith("/historical/markets") and "candlesticks" not in path:
            return {"markets": []}
        if "candlesticks" in path:
            ts = int(datetime.now(UTC).timestamp()) - 3 * 86400
            return {"candlesticks": [{"end_period_ts": ts, "yes_bid": {"close_dollars": "0.4100"},
                                      "yes_ask": {"close_dollars": "0.4400"}, "price": {}, "volume_fp": "3"}]}
        return {}


def _om_handler(request: httpx.Request) -> httpx.Response:
    if "open-meteo" in request.url.host:
        q = request.url.params
        start = datetime.fromisoformat(q["start_date"])
        times, vals = [], []
        for i in range(((datetime.fromisoformat(q["end_date"]) - start).days + 1) * 24):
            t = start + timedelta(hours=i)
            times.append(t.strftime("%Y-%m-%dT%H:%M"))
            vals.append(60 + (i % 24) * 0.5)
        return httpx.Response(200, json={"hourly": {"time": times, "temperature_2m_previous_day1": vals,
                                                    "temperature_2m_previous_day2": vals}})
    return httpx.Response(200, json={"results": [{"valid": "2026-10-01", "high": 71}]})


def test_download_all_writes_a_loadable_cache(tmp_path, monkeypatch):
    real = httpx.AsyncClient

    def fake_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(_om_handler)
        return real(*a, **kw)

    monkeypatch.setattr("app.research.weather_download.httpx.AsyncClient", fake_client)
    k = FakeKalshi()
    asyncio.run(download_all(k, tmp_path, days=10, city_keys=["ny"], say=lambda s: None))
    cand = [p for p, prm in k.calls if "candlesticks" in p]
    assert cand and all(prm["period_interval"] == 60 for p, prm in k.calls if "candlesticks" in p)
    fc = json.loads((tmp_path / "forecast_ny.json").read_text())
    some = next(iter(fc.values()))
    assert set(some["1"]) >= {"gfs_seamless", "ecmwf_ifs025"} and some["1"]["gfs_seamless"] == 71.5
    assert json.loads((tmp_path / "obs_ny.json").read_text()) == {"2026-10-01": 71}
    wd = load_weather(tmp_path)
    (ev,) = wd.events.values()
    assert ev.markets[0].bracket == (70, 71) and ev.markets[0].quotes[0][1:] == (41.0, 44.0)
