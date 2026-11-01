from datetime import date, datetime, timedelta

from app.research.weather import (
    ET, WeatherData, WMarket, bracket, bracket_prob, daily_max, decision_time, derive_high, distribution,
    event_date, parse_iem_cli, parse_open_meteo,
)


def test_event_date():
    assert event_date("KXHIGHNY-26OCT20") == date(2026, 10, 20)
    assert event_date("KXHIGHCHI-25JAN05-B31.5") == date(2025, 1, 5)
    assert event_date("nonsense") is None


def test_bracket_from_subtitles_and_strikes():
    assert bracket({"yes_sub_title": "68° to 69°"}) == (68, 69)
    assert bracket({"yes_sub_title": "75° or above"}) == (75, None)
    assert bracket({"yes_sub_title": "61° or below"}) == (None, 61)
    assert bracket({"strike_type": "between", "floor_strike": 68, "cap_strike": 69}) == (68, 69)
    assert bracket({"strike_type": "greater", "floor_strike": 74}) == (75, None)
    assert bracket({"strike_type": "less", "cap_strike": 62}) == (None, 61)
    assert bracket({}) is None


def test_bracket_probs_partition_to_one():
    bs = [(None, 61), (62, 63), (64, 65), (66, 67), (68, None)]
    assert abs(sum(bracket_prob(b, 65.2, 2.0) for b in bs) - 1) < 1e-9


def test_derive_high_unique_or_none():
    mk = [((None, 61), "no"), ((62, 63), "yes"), ((64, None), "no"), ((63, None), "yes")]
    assert derive_high(mk) == 63
    assert derive_high([((62, 63), "yes")]) is None


def test_parsers():
    times = [f"2026-10-01T{h:02d}:00" for h in range(24)] + ["2026-10-02T00:00"]
    data = {"hourly": {"time": times, "temperature_2m_previous_day1_gfs_seamless": list(range(24)) + [50],
                       "temperature_2m_previous_day2": [None] * 25}}
    out = parse_open_meteo(data)
    assert out[1] == {"2026-10-01": 23.0} and out[2] == {}
    assert daily_max(["2026-10-01T00:00"] * 5, [1, 2, 3, 4, 5]) == {}
    assert parse_iem_cli({"results": [{"valid": "2026-10-01", "high": 71}, {"valid": "2026-10-02", "high": "M"}]}) \
        == {"2026-10-01": 71}


def _wd(obs_shift_recent: float = 0.0) -> WeatherData:
    fc, obs = {}, {}
    for i in range(60):
        d = (date(2026, 10, 31) - timedelta(days=i)).isoformat()
        fc[d] = {"1": {"gfs_seamless": 70.0, "ecmwf_ifs025": 70.0}}
        obs[d] = 72 + (i % 3) - 1
    # the decision day and the day before must never influence the distribution
    for d in ("2026-10-31", "2026-10-30"):
        obs[d] = int(72 + obs_shift_recent)
    return WeatherData({}, {"ny": fc}, {"ny": obs}, {})


def test_distribution_uses_no_future_or_unpublished_observations():
    a = distribution(_wd(), "ny", date(2026, 10, 31), 1, True)
    b = distribution(_wd(40), "ny", date(2026, 10, 31), 1, True)
    assert a == b
    mu, sigma = a
    assert abs(mu - 72) < 0.5 and sigma >= 1.5
    raw_mu, _ = distribution(_wd(), "ny", date(2026, 10, 31), 1, False)
    assert raw_mu == 70.0


def test_distribution_needs_history_and_two_models():
    wd = _wd()
    wd.forecasts["ny"]["2026-10-31"]["1"] = {"gfs_seamless": 70.0}
    assert distribution(wd, "ny", date(2026, 10, 31), 1, True) is None


def test_quote_staleness_and_no_lookahead():
    at = decision_time(date(2026, 10, 20), 1)
    assert at == datetime(2026, 10, 20, 10, tzinfo=ET)
    t = at.timestamp()
    m = WMarket("X", (60, 61), "yes", [(t - 7200, 40, 44), (t + 60, 90, 95)])
    assert m.quote(at) == (40, 44)  # the later quote is never used
    stale = WMarket("X", (60, 61), "yes", [(t - 4 * 3600, 40, 44)])
    assert stale.quote(at) == (None, None)
