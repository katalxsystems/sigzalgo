"""A price watch can be paused outside market hours.

With pauseOutsideMarketHours set, a watch ignores every price at weekends and
overnight: NSE/BSE/NFO/BFO from 15:45 to 09:00 IST, MCX/NCDEX from 23:45 to
08:45. A post-close price can never report a breach, and the watch stays
registered and picks up again at the resume time.
"""

import types
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

import services.flow_price_monitor_service as fpm

IST = ZoneInfo("Asia/Kolkata")
# 2026-11-11 is a Wednesday; 2026-11-14 a Saturday.
WED, SAT = (2026, 11, 11), (2026, 11, 14)


def ist(day, hour, minute):
    return datetime(*day, hour, minute, tzinfo=IST)


@pytest.mark.parametrize(
    ("when", "paused"),
    [
        (ist(WED, 8, 59), True),
        (ist(WED, 9, 0), False),
        (ist(WED, 15, 44), False),
        (ist(WED, 15, 45), True),
        (ist(WED, 23, 0), True),
        (ist(SAT, 11, 0), True),
    ],
)
@pytest.mark.parametrize("exchange", ["NSE", "BSE", "NFO", "BFO", "NSE_INDEX", "nfo"])
def test_the_window_is_0900_to_1545_ist_on_weekdays(exchange, when, paused):
    assert fpm.is_outside_monitoring_hours(exchange, when) is paused


@pytest.mark.parametrize(
    ("when", "paused"),
    [
        (ist(WED, 8, 44), True),
        (ist(WED, 8, 45), False),
        (ist(WED, 15, 50), False),
        (ist(WED, 23, 44), False),
        (ist(WED, 23, 45), True),
        (ist(WED, 2, 0), True),
        (ist(SAT, 11, 0), True),
    ],
)
@pytest.mark.parametrize("exchange", ["MCX", "NCDEX", "MCX_INDEX", "mcx"])
def test_the_commodity_window_is_0845_to_2345_ist_on_weekdays(exchange, when, paused):
    assert fpm.is_outside_monitoring_hours(exchange, when) is paused


@pytest.mark.parametrize("exchange", ["CDS", "BCD", "CRYPTO"])
def test_other_exchanges_are_never_paused(exchange):
    assert fpm.is_outside_monitoring_hours(exchange, ist(WED, 23, 0)) is False
    assert fpm.is_outside_monitoring_hours(exchange, ist(SAT, 11, 0)) is False


def test_the_clock_is_read_in_ist_whatever_the_servers_zone():
    # 04:00 UTC is 09:30 IST; 10:30 UTC is 16:00 IST.
    assert fpm.is_outside_monitoring_hours("NFO", datetime(*WED, 4, 0, tzinfo=UTC)) is False
    assert fpm.is_outside_monitoring_hours("NFO", datetime(*WED, 10, 30, tzinfo=UTC)) is True


@pytest.fixture
def monitor(monkeypatch):
    """A monitor whose clock, quotes and workflow trigger are stubbed."""
    m = fpm.get_flow_price_monitor()
    m._alerts.clear()
    clock = types.SimpleNamespace(now=ist(WED, 16, 0))
    fired, quotes = [], []

    real = fpm.is_outside_monitoring_hours
    monkeypatch.setattr(
        fpm, "is_outside_monitoring_hours", lambda exchange, now=None: real(exchange, clock.now)
    )
    monkeypatch.setattr(
        m, "_trigger_workflow", lambda alert, price, breach=None: fired.append(price)
    )
    monkeypatch.setattr(m, "_start_monitoring", lambda: None)
    monkeypatch.setattr(m, "_stop_monitoring", lambda: None)
    monkeypatch.setattr(m, "_ws_subscribe", lambda *a: None)

    class Client:
        def get_quotes(self, symbol, exchange):
            quotes.append(symbol)
            return {"status": "success", "data": {"ltp": 50.0}}

    monkeypatch.setattr(fpm, "get_flow_client", lambda api_key: Client())

    def arm(pause=True, exchange="NFO", condition="crosses_below", price=100.0):
        m.add_alert(
            workflow_id=901,
            symbol="NIFTY24NOV2624000CE",
            exchange=exchange,
            condition=condition,
            target_price=price,
            api_key="k",
            pause_outside_market_hours=pause,
        )
        return m.get_alert(901)

    yield types.SimpleNamespace(m=m, clock=clock, fired=fired, quotes=quotes, arm=arm)
    m._alerts.clear()


def test_a_paused_watch_ignores_prices_and_spends_no_quotes(monitor):
    alert = monitor.arm()
    monitor.m._apply_price(alert, 50.0, source="websocket tick")
    monitor.m._check_alert(alert)

    assert monitor.fired == []
    assert monitor.quotes == []
    assert alert.last_price is None and alert.triggered is False


def test_monitoring_resumes_at_0900_and_a_gap_through_the_level_still_counts(monitor):
    monitor.clock.now = ist(WED, 15, 30)
    alert = monitor.arm()
    monitor.m._apply_price(alert, 120.0)  # last price before the close, above 100

    monitor.clock.now = ist(WED, 15, 50)
    monitor.m._apply_price(alert, 90.0)  # post-close print: ignored
    assert monitor.fired == [] and alert.last_price == 120.0

    monitor.clock.now = ist((2026, 11, 12), 9, 0)
    monitor.m._apply_price(alert, 95.0)  # opens below the level
    assert monitor.fired == [95.0]


def test_without_the_option_a_watch_runs_through_the_night(monitor):
    alert = monitor.arm(pause=False)
    monitor.m._check_alert(alert)
    assert monitor.quotes == ["NIFTY24NOV2624000CE"]
    assert monitor.fired == [50.0]


def test_an_mcx_watch_is_live_in_the_evening_and_paused_after_2345(monitor):
    # The fixture's clock starts at 16:00, when equity watches are paused.
    alert = monitor.arm(exchange="MCX", condition="below")
    monitor.clock.now = ist(WED, 23, 50)
    monitor.m._apply_price(alert, 50.0)
    assert monitor.fired == []

    monitor.clock.now = ist(WED, 16, 0)
    monitor.m._apply_price(alert, 50.0)
    assert monitor.fired == [50.0]


def test_the_option_does_nothing_on_currency(monitor):
    alert = monitor.arm(exchange="CDS")
    monitor.m._apply_price(alert, 50.0)
    assert monitor.fired == [50.0]


def test_restore_after_a_restart_keeps_the_option(monkeypatch, monitor):
    workflow = types.SimpleNamespace(
        id=902,
        nodes=[
            {
                "type": "priceAlert",
                "data": {
                    "symbol": "SBIN",
                    "exchange": "NSE",
                    "condition": "above",
                    "price": 800,
                    "pauseOutsideMarketHours": True,
                },
            }
        ],
    )
    fake_db = types.ModuleType("database.flow_db")
    fake_db.get_active_workflows = lambda: [workflow]
    fake_db.get_workflow_api_key = lambda wf: "k"
    monkeypatch.setitem(__import__("sys").modules, "database.flow_db", fake_db)

    assert fpm.restore_price_alerts() == 1
    assert monitor.m.get_alert(902).pause_outside_market_hours is True
