"""The Pocketful market-data feed is authenticated with a Bearer header.

With the token only in the query string the feed answered 401 to a token REST
accepted, so quotes and streaming never connected.
"""

from __future__ import annotations

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
