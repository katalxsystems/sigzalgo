"""The scalping risk monitor and the strategy tick feed retry refused subscribes.

Both recorded a subscribe as done whatever the proxy answered. After a re-login
tore the broker feed down, a refused symbol ("Broker adapter not found") was
never asked for again:

- scalping takes prices from live ticks only, so its stop-loss could not fire;
- the strategy feed fell back to REST polling for the rest of the session.
"""

from __future__ import annotations

import pytest

import services.scalping_risk_monitor_service as srm
import services.strategy_module.tick_feed as tf
from services.websocket_client import accepted_symbol_keys

SYM, EXCH = "NIFTY25JUN2623600CE", "NFO"
KEY = f"{EXCH}:{SYM}"
REFUSED = {"status": "error", "message": "Broker adapter not found"}


# --- the shared ack reader ------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "accepted"),
    [
        (None, set()),
        (REFUSED, set()),
        ({"status": "success"}, {"NFO:A", "NFO:B"}),
        (
            {
                "status": "partial",
                "subscriptions": [
                    {"symbol": "A", "exchange": "NFO", "status": "success"},
                    {"symbol": "B", "exchange": "NFO", "status": "error"},
                ],
            },
            {"NFO:A"},
        ),
        (
            # An entry the caller did not ask for is not adopted.
            {
                "status": "success",
                "subscriptions": [{"symbol": "Z", "exchange": "NFO", "status": "success"}],
            },
            set(),
        ),
    ],
)
def test_accepted_symbol_keys(result, accepted):
    assert accepted_symbol_keys(result, {"NFO:A", "NFO:B"}) == accepted


# --- scalping risk monitor --------------------------------------------------------


class ScriptedWs:
    connected = True

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[list[dict]] = []

    def subscribe(self, symbols, mode):
        self.calls.append(symbols)
        answer = self.answers.pop(0) if self.answers else {"status": "success"}
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def scalping(monkeypatch):
    """A private monitor instance (not the process singleton) with one SL."""
    m = object.__new__(srm.ScalpingRiskMonitor)
    m._init_once()
    m._states = {f"{EXCH}:{SYM}:NRML": {"symbol": SYM, "exchange": EXCH, "product": "NRML"}}
    monkeypatch.setattr(m, "_ensure_ws", lambda: m._ws is not None)
    yield m
    monkeypatch.setattr(m, "stop", lambda: None)


def test_a_refused_scalping_symbol_is_not_recorded_as_subscribed(scalping):
    scalping._ws = ScriptedWs(REFUSED)
    scalping._subscribe({KEY})
    assert KEY not in scalping._subscribed
    assert KEY in scalping._refused


def test_the_scalping_retry_asks_again_until_accepted_then_stops(scalping):
    ws = scalping._ws = ScriptedWs(REFUSED, REFUSED, {"status": "success"})
    scalping._subscribe({KEY})
    assert scalping._retry_missing() == {KEY}
    assert KEY not in scalping._subscribed
    assert scalping._retry_missing() == {KEY}
    assert KEY in scalping._subscribed and not scalping._refused
    assert scalping._retry_missing() == set()
    assert len(ws.calls) == 3


def test_a_scalping_subscribe_that_raises_is_retried(scalping):
    scalping._ws = ScriptedWs(TimeoutError("ack timed out"))
    scalping._subscribe({KEY})
    assert KEY not in scalping._subscribed
    scalping._retry_missing()
    assert KEY in scalping._subscribed


def test_an_sl_restored_before_the_feed_was_up_is_picked_up(scalping):
    # No feed at the boot sync: nothing was subscribed or even attempted.
    scalping._ws = None
    assert scalping._retry_missing() == {KEY}
    assert not scalping._subscribed

    scalping._ws = ScriptedWs({"status": "success"})
    scalping._retry_missing()
    assert KEY in scalping._subscribed


def test_a_removed_sl_is_not_retried(scalping):
    scalping._ws = ScriptedWs(REFUSED)
    scalping._subscribe({KEY})
    scalping._states = {}
    assert scalping._retry_missing() == set()
    assert not scalping._refused


def test_the_scalping_loop_starts_with_the_drain_and_stops_with_it(scalping, monkeypatch):
    monkeypatch.setattr(srm, "RESUBSCRIBE_RETRY_SEC", 3600)
    scalping._ensure_drain()
    assert scalping._resubscribe_thread.is_alive()
    srm.ScalpingRiskMonitor.stop(scalping)
    assert scalping._resubscribe_thread is None


# --- strategy tick feed ------------------------------------------------------------


class FeedWs:
    connected = True

    def __init__(self):
        self.result = {"status": "success"}
        self.calls: list[list[dict]] = []

    def subscribe(self, symbols, mode="LTP"):
        self.calls.append(symbols)
        return self.result

    def unsubscribe(self, symbols, mode="LTP"):
        return {"status": "success"}

    def register_callback(self, *a):
        pass

    def unregister_callback(self, *a):
        pass


@pytest.fixture
def feed():
    ws = FeedWs()
    f = tf.RiskTickFeed(
        clock=lambda: 1000.0,
        ws_provider=lambda _key: ws,
        quote_fetcher=lambda symbols, key: (True, {"status": "success", "results": []}, 200),
        api_key_provider=lambda: "k",
    )
    f._running = True
    yield f, ws
    f.stop()


def _source(f, symbol=SYM, exchange=EXCH):
    return f._symbols[tf._key(symbol, exchange)].source


def test_a_refused_strategy_symbol_polls_and_is_retried_onto_live_ticks(feed):
    f, ws = feed
    ws.result = REFUSED
    f.add_run_subscriptions(1, [(SYM, EXCH)])
    assert _source(f) == tf.POLLING
    assert f._ws_refused == {KEY}

    assert f._retry_refused() == 1  # still refused
    ws.result = {"status": "success"}
    assert f._retry_refused() == 1
    assert f._ws_refused == set()
    assert f._retry_refused() == 0
    assert len(ws.calls) == 3


def test_only_the_refused_symbols_of_a_partial_ack_poll(feed):
    f, ws = feed
    ws.result = {
        "status": "partial",
        "subscriptions": [
            {"symbol": "A", "exchange": "NFO", "status": "success"},
            {"symbol": "B", "exchange": "NFO", "status": "error"},
        ],
    }
    f.add_run_subscriptions(1, [("A", "NFO"), ("B", "NFO")])
    assert _source(f, "A", "NFO") == tf.WS_LIVE
    assert _source(f, "B", "NFO") == tf.POLLING
    assert f._ws_refused == {"NFO:B"}


def test_a_released_symbol_is_not_retried(feed):
    f, ws = feed
    ws.result = REFUSED
    f.add_run_subscriptions(1, [(SYM, EXCH)])
    f.remove_run_subscriptions(1)
    calls = len(ws.calls)
    assert f._retry_refused() == 0
    assert len(ws.calls) == calls and f._ws_refused == set()
