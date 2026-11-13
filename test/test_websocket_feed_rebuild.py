"""A re-login must not leave connected clients on a dead broker feed.

A token change (a morning auto-login, the ~3 AM rollover, a manual re-login)
publishes a cache invalidation, and the proxy tears down the user's broker
adapter. Before this fix nothing rebuilt it: connected clients kept their
subscriptions but received no ticks, and any new subscribe was refused with
"Broker adapter not found" until the client authenticated again. The Flow
price monitor never does, so breaches were only seen by its 30-second REST
poll for the rest of the day.

Self-contained: no port, socket, thread or ZMQ context is opened.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest

import websocket_proxy.broker_factory as broker_factory
from websocket_proxy import server as proxy_server
from websocket_proxy.server import WebSocketProxy

USER = "siddharth_fivepaisa_86e25945"


class _Adapter:
    def __init__(self, init_result=None, connect_result=None, refuse=()):
        self.init_result = init_result
        self.connect_result = connect_result
        self.refuse = set(refuse)
        self.initialized: list[tuple[str, str]] = []
        self.connected = 0
        self.disconnected = 0
        self.subscribed: list[tuple[str, str, int, int]] = []

    def initialize(self, broker_name, user_id):
        self.initialized.append((broker_name, user_id))
        return self.init_result

    def connect(self):
        self.connected += 1
        return self.connect_result

    def disconnect(self):
        self.disconnected += 1

    def subscribe(self, symbol, exchange, mode, depth):
        self.subscribed.append((symbol, exchange, mode, depth))
        if symbol in self.refuse:
            return {"status": "error", "message": "not found"}
        return {"status": "success"}


def _sub(symbol, exchange="NFO", mode=1, depth=5):
    return json.dumps(
        {
            "symbol": symbol,
            "exchange": exchange,
            "mode": mode,
            "depth_level": depth,
            "broker": "fivepaisa",
        }
    )


@pytest.fixture
def proxy(monkeypatch):
    """A proxy with two connected clients of USER sharing one symbol, and one
    client of another user."""
    p = WebSocketProxy.__new__(WebSocketProxy)
    old = _Adapter()
    p.broker_adapters = {USER: old, "other": _Adapter()}
    p.user_broker_mapping = {USER: "fivepaisa", "other": "fivepaisa"}
    p.user_mapping = {1: USER, 2: USER, 3: "other"}
    p.subscriptions = {
        1: {_sub("SUNPHARMA23NOV261860PE"), _sub("NIFTY", "NSE_INDEX")},
        2: {_sub("SUNPHARMA23NOV261860PE")},
        3: {_sub("SBIN", "NSE")},
    }
    p.subscription_index = defaultdict(set)
    monkeypatch.setattr(broker_factory, "cleanup_pools_for_user", lambda *a, **k: 1)

    created: list[_Adapter] = []
    next_adapter: dict[str, Any] = {"value": None}

    def create(broker_name):
        adapter = next_adapter["value"] or _Adapter()
        created.append(adapter)
        return adapter

    monkeypatch.setattr(proxy_server, "create_broker_adapter", create)
    p._test = {"old": old, "created": created, "next": next_adapter}
    return p


def _invalidate(p, user=USER):
    p._handle_cache_invalidation(
        f"CACHE_INVALIDATE_ALL_{user}",
        json.dumps({"action": "invalidate", "user_id": user, "cache_type": "ALL"}),
    )


def test_the_feed_is_rebuilt_and_every_live_subscription_restored_once(proxy):
    _invalidate(proxy)

    old, created = proxy._test["old"], proxy._test["created"]
    assert old.disconnected == 1
    assert len(created) == 1
    new = created[0]
    assert proxy.broker_adapters[USER] is new
    assert new.initialized == [("fivepaisa", USER)] and new.connected == 1
    # Two clients share SUNPHARMA: one broker subscription. The other user's
    # SBIN is not this user's to restore.
    assert sorted(new.subscribed) == [
        ("NIFTY", "NSE_INDEX", 1, 5),
        ("SUNPHARMA23NOV261860PE", "NFO", 1, 5),
    ]
    # The clients' own records are what routes ticks back; left as they were.
    assert proxy.subscriptions[2] == {_sub("SUNPHARMA23NOV261860PE")}


def test_other_users_feeds_are_left_alone(proxy):
    other = proxy.broker_adapters["other"]
    _invalidate(proxy)
    assert proxy.broker_adapters["other"] is other and other.disconnected == 0


def test_nothing_is_rebuilt_when_no_client_of_the_user_is_connected(proxy):
    proxy.user_mapping = {3: "other"}
    _invalidate(proxy)
    assert proxy._test["created"] == []
    assert USER not in proxy.broker_adapters


def test_a_logout_leaves_the_feed_down(proxy):
    # A revoked session has no token, so the new adapter cannot initialize.
    proxy._test["next"]["value"] = _Adapter(
        init_result={"status": "error", "message": "No authentication token found"}
    )
    _invalidate(proxy)

    new = proxy._test["created"][0]
    assert USER not in proxy.broker_adapters
    assert new.connected == 0 and new.subscribed == []


def test_a_failed_connect_is_cleaned_up_and_not_kept(proxy):
    # The connection pool's result shape, not the adapter's.
    proxy._test["next"]["value"] = _Adapter(
        connect_result={"success": False, "error": "Failed to connect"}
    )
    _invalidate(proxy)

    new = proxy._test["created"][0]
    assert USER not in proxy.broker_adapters
    assert new.disconnected == 1 and new.subscribed == []


def test_a_connect_that_raises_is_still_disconnected(proxy):
    class Raising(_Adapter):
        def connect(self):
            self.connected += 1
            raise ConnectionError("socket reset")

    proxy._test["next"]["value"] = Raising()
    _invalidate(proxy)

    new = proxy._test["created"][0]
    assert USER not in proxy.broker_adapters
    assert new.disconnected == 1


def test_a_refused_symbol_does_not_stop_the_rest(proxy):
    proxy._test["next"]["value"] = _Adapter(refuse={"NIFTY"})
    _invalidate(proxy)

    new = proxy._test["created"][0]
    assert proxy.broker_adapters[USER] is new
    assert len(new.subscribed) == 2


def test_a_relogin_after_a_failed_rebuild_still_recovers(proxy):
    proxy._test["next"]["value"] = _Adapter(init_result={"status": "error", "message": "x"})
    _invalidate(proxy)
    assert USER not in proxy.broker_adapters

    proxy._test["next"]["value"] = None
    _invalidate(proxy)
    assert proxy.broker_adapters[USER] is proxy._test["created"][-1]


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (None, None),
        ({"status": "success"}, None),
        ({"success": True}, None),
        ({"status": "error", "message": "bad token"}, "bad token"),
        ({"success": False, "error": "refused"}, "refused"),
        ({"success": False}, "unknown error"),
    ],
)
def test_call_failed_reads_both_result_shapes(result, error):
    assert proxy_server._call_failed(result) == error
