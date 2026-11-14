"""Pocketful chart history from GET algo.pocketful.in/api/v1/charts.

The plugin returned no history at all ("no longer supported"), so the chart
had nothing to draw. The candle shape below is what the live API returned:
candletype "1" is 1-minute, "3" is daily, prices are in rupees.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

import broker.pocketful.api.data as pdata


def _minute_candles(day: str, n: int, start_price: float = 400.0) -> list:
    t0 = datetime.fromisoformat(f"{day}T09:15:00")
    rows = []
    for i in range(n):
        ts = (t0 + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S") + "+0530"
        p = start_price + i
        rows.append([ts, p, p + 0.5, p - 0.5, p + 0.25, 10])
    return rows


@pytest.fixture
def handler(monkeypatch):
    h = pdata.BrokerData("tok")
    monkeypatch.setattr(h, "_chart_token", lambda s, e: 383)
    monkeypatch.setattr(h, "_get_client_id", lambda: "S9720")
    calls = []

    def fetch(exchange, token, candletype, start, end):
        calls.append((exchange, candletype, start.date().isoformat(), end.date().isoformat()))
        return h._rows.get(candletype, [])

    h._rows = {}
    monkeypatch.setattr(h, "_fetch_chart_candles", fetch)
    h.calls = calls
    return h


def _ist(epoch: int) -> pd.Timestamp:
    return pd.Timestamp(epoch, unit="s", tz="UTC").tz_convert("Asia/Kolkata")


def test_one_minute_candles_come_back_as_they_are(handler):
    handler._rows = {"1": _minute_candles("2026-11-13", 3)}
    df = handler.get_history("BEL", "NSE", "1m", "2026-11-13", "2026-11-13")
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume", "oi"]
    assert len(df) == 3
    assert _ist(df["timestamp"].iloc[0]).strftime("%H:%M") == "09:15"
    assert df["open"].iloc[0] == 400.0  # rupees, not paise


def test_five_minute_bars_are_built_from_minutes_and_aligned_to_the_open(handler):
    handler._rows = {"1": _minute_candles("2026-11-13", 10)}
    df = handler.get_history("BEL", "NSE", "5m", "2026-11-13", "2026-11-13")
    assert len(df) == 2
    first = df.iloc[0]
    assert _ist(first["timestamp"]).strftime("%H:%M") == "09:15"
    assert first["open"] == 400.0 and first["close"] == 404.25
    assert first["high"] == 404.5 and first["low"] == 399.5
    assert first["volume"] == 50
    assert _ist(df["timestamp"].iloc[1]).strftime("%H:%M") == "09:20"


def test_hourly_bars_start_at_nine_fifteen(handler):
    handler._rows = {"1": _minute_candles("2026-11-13", 75)}
    df = handler.get_history("BEL", "NSE", "1h", "2026-11-13", "2026-11-13")
    assert [_ist(t).strftime("%H:%M") for t in df["timestamp"]] == ["09:15", "10:15"]


def test_daily_uses_candletype_three_at_midnight_ist(handler):
    handler._rows = {"3": [["2026-11-13T09:15:00+0530", 404.4, 406, 397.65, 400.85, 7533948]]}
    df = handler.get_history("BEL", "NSE", "D", "2026-11-13", "2026-11-13")
    assert handler.calls[0][1] == "3"
    assert _ist(df["timestamp"].iloc[0]).strftime("%Y-%m-%d %H:%M") == "2026-11-13 00:00"
    assert df["close"].iloc[0] == 400.85 and df["volume"].iloc[0] == 7533948


def test_weekly_bars_start_on_monday(handler):
    days = ["2026-11-09", "2026-11-10", "2026-11-13", "2026-11-16"]  # Mon, Tue, Fri, next Mon
    handler._rows = {
        "3": [[f"{d}T09:15:00+0530", 100 + i, 110 + i, 90 + i, 105 + i, 1000] for i, d in enumerate(days)]
    }
    df = handler.get_history("BEL", "NSE", "W", "2026-11-09", "2026-11-16")
    assert [_ist(t).strftime("%Y-%m-%d") for t in df["timestamp"]] == ["2026-11-09", "2026-11-16"]
    week = df.iloc[0]
    assert week["open"] == 100 and week["close"] == 107 and week["volume"] == 3000


def test_long_minute_ranges_are_split_into_thirty_day_requests(handler):
    handler.get_history("BEL", "NSE", "5m", "2026-09-01", "2026-11-13")
    spans = [(c[2], c[3]) for c in handler.calls]
    assert spans[0] == ("2026-09-01", "2026-09-30")
    assert spans[-1][1] == "2026-11-13"
    assert all(c[1] == "1" for c in handler.calls)


def test_an_index_is_asked_for_on_its_cash_exchange(handler):
    handler.get_history("NIFTY", "NSE_INDEX", "D", "2026-11-13", "2026-11-13")
    assert handler.calls[0][0] == "NSE"


def test_no_candles_is_an_empty_history_not_an_error(handler):
    df = handler.get_history("BEL", "NSE", "D", "2026-11-13", "2026-11-13")
    assert df.empty and "timestamp" in df.columns


def test_an_unknown_timeframe_names_the_ones_available(handler):
    with pytest.raises(pdata.PocketfulAPIError, match="1m"):
        handler.get_history("BEL", "NSE", "2h", "2026-11-13", "2026-11-13")


def test_the_advertised_intervals_are_all_accepted_by_the_history_api():
    from utils.constants import SUPPORTED_INTERVALS

    assert set(pdata.BrokerData("t").timeframe_map) <= set(SUPPORTED_INTERVALS)
