"""The Pocketful market-data feed is authenticated with a Bearer header.

With the token only in the query string the feed answered 401 to a token REST
accepted, so quotes and streaming never connected.
"""

from __future__ import annotations

import pytest

import broker.pocketful.api.pocketfulwebsocket as pws


class _RecordingApp:
    calls: list[dict] = []

    def __init__(self, url, header=None, **kwargs):
        _RecordingApp.calls.append({"url": url, "header": header})
        self.on_open = None

    def run_forever(self, *a, **k):
        return None


def test_the_header_carries_the_token():
    assert pws.feed_auth_header("tok") == ["Authorization: Bearer tok"]
    assert pws.feed_auth_header(None) == []


def test_the_quote_client_sends_the_header(monkeypatch):
    _RecordingApp.calls = []
    monkeypatch.setattr(pws.websocket, "WebSocketApp", _RecordingApp)
    sock = pws.PocketfulSocket("S9720", "tok")
    sock._connect("wss://trade.pocketful.in/ws/v1/feeds?login_id=S9720&access_token=tok", "tok")
    assert _RecordingApp.calls[0]["header"] == ["Authorization: Bearer tok"]


class _FakeSock:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, raw):
        import json

        self.sent.append(json.loads(raw))


@pytest.fixture
def feed(monkeypatch):
    """A PocketfulSocket on a fake connection, with clean shared state."""
    fake = _FakeSock()
    monkeypatch.setattr(pws, "websock", fake)
    monkeypatch.setattr(pws, "dtlmktdata_dict", {})
    monkeypatch.setattr(pws, "_sub_refs", {})
    monkeypatch.setattr(pws, "_sub_last_used", {})
    monkeypatch.setattr(pws, "_subscribed", set())
    clock = {"now": 1000.0}
    monkeypatch.setattr(pws.time, "time", lambda: clock["now"])
    return pws.PocketfulSocket("S9720", "tok"), fake, clock


A = {"exchangeCode": 2, "instrumentToken": 111}
B = {"exchangeCode": 2, "instrumentToken": 222}


def _tick(payload, ltp):
    pws.dtlmktdata_dict[pws.detailed_key(payload)] = {
        "instrument_token": payload["instrumentToken"],
        "exchange_code": payload["exchangeCode"],
        "last_traded_price": ltp,
    }


def _actions(fake):
    return [m["a"] for m in fake.sent]


def test_each_symbol_reads_its_own_tick(feed):
    sock, _fake, _clock = feed
    sock.acquire_detailed(A)
    sock.acquire_detailed(B)
    _tick(A, 100)
    _tick(B, 200)  # B ticking last no longer hides A's tick
    assert sock.read_detailed_for(A)["last_traded_price"] == 100
    assert sock.read_detailed_for(B)["last_traded_price"] == 200


def test_one_request_finishing_does_not_cut_off_another(feed):
    sock, fake, _clock = feed
    sock.acquire_detailed(A)
    sock.acquire_detailed(A)  # a second, overlapping request
    _tick(A, 100)
    sock.release_detailed(A)
    assert sock.read_detailed_for(A) is not None
    assert _actions(fake) == ["subscribe"]  # subscribed once, never torn down


def test_a_released_symbol_lingers_then_unsubscribes(feed):
    sock, fake, clock = feed
    sock.acquire_detailed(A)
    _tick(A, 100)
    sock.release_detailed(A)

    clock["now"] += pws.SUBSCRIPTION_LINGER_SEC - 1
    sock.acquire_detailed(A)  # the next refresh: answered from the live tick
    assert sock.read_detailed_for(A)["last_traded_price"] == 100
    assert _actions(fake) == ["subscribe"]
    sock.release_detailed(A)

    clock["now"] += pws.SUBSCRIPTION_LINGER_SEC + 1
    sock.acquire_detailed(B)  # any later call sweeps the idle symbol
    assert _actions(fake) == ["subscribe", "subscribe", "unsubscribe"]
    assert sock.read_detailed_for(A) is None


def test_a_reconnect_resubscribes(feed):
    sock, fake, _clock = feed
    sock.acquire_detailed(A)
    sock.release_detailed(A)
    pws._forget_subscriptions()  # what on_open does after a reconnect
    sock.acquire_detailed(A)
    assert _actions(fake) == ["subscribe", "subscribe"]


def test_the_streaming_adapter_sends_the_header(monkeypatch):
    import websocket_proxy  # noqa: F401 - the app loads adapters through it
    from broker.pocketful.streaming import pocketful_adapter as adapter_mod

    _RecordingApp.calls = []
    monkeypatch.setattr(adapter_mod.websocket, "WebSocketApp", _RecordingApp)

    import logging

    monkeypatch.setattr(adapter_mod, "get_auth_token", lambda *a, **k: "tok")
    adapter = object.__new__(adapter_mod.PocketfulWebSocketAdapter)
    adapter.access_token = "old"
    adapter.user_id = "S9720"
    adapter.logger = logging.getLogger("test")
    adapter.reconnect_attempts = 0
    adapter.max_reconnect_attempts = 1
    adapter._token_miss_count = 0

    # One pass of the connect loop: stop after the first run_forever returns.
    adapter.running = True

    def stop_after_first(*a, **k):
        adapter.running = False

    monkeypatch.setattr(_RecordingApp, "run_forever", lambda self, *a, **k: stop_after_first())
    try:
        adapter._connect_with_retry()
    except Exception:
        pass
    assert _RecordingApp.calls, "the adapter never opened the feed"
    assert _RecordingApp.calls[0]["header"] == ["Authorization: Bearer tok"]
