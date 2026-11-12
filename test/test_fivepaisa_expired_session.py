"""An expired 5paisa token must not keep hitting 5paisa all night.

5paisa tokens expire at midnight. The feed reconnected with the expired token
(401 every attempt) and price polling called MarketSnapshot for every symbol
every 30 seconds, logging "Invalid Session" each time, until someone logged in.
The token carries its own expiry, so both now stop and wait instead.
"""

import base64
import json
import time
from unittest import mock

import pytest

import websocket_proxy  # noqa: F401  isort:skip  -- breaks the adapter import cycle
from broker.fivepaisa.api import data
from broker.fivepaisa.streaming import fivepaisa_adapter as fa
from utils.token_expiry import expired_at, jwt_expiry
from websocket_proxy.base_adapter import BaseBrokerWebSocketAdapter


def _jwt(exp: float) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256'})}.{part({'unique_name': 'C1', 'exp': exp})}.sig"


EXPIRED = _jwt(time.time() - 3600)
LIVE = _jwt(time.time() + 3600)


def test_the_expiry_is_read_from_the_token():
    assert jwt_expiry(_jwt(1_800_000_000)) == 1_800_000_000
    assert expired_at(LIVE) is None
    assert expired_at(EXPIRED) is not None


@pytest.mark.parametrize("token", [None, "", "not-a-jwt", "a.b.c", "a.e30.c"])
def test_an_unreadable_token_never_counts_as_expired(token):
    assert expired_at(token) is None


def test_quotes_with_an_expired_token_skip_5paisa_and_warn_once():
    data._REPORTED_EXPIRED.clear()
    broker = data.BrokerData(EXPIRED)
    # get_quotes swallows its own exceptions, so "nothing raised" proves
    # nothing: assert that no request was even prepared.
    with (
        mock.patch.object(data, "get_httpx_client") as http,
        mock.patch.object(data, "get_token") as lookup,
        mock.patch.object(data.logger, "warning") as warning,
    ):
        assert broker.get_quotes("SBIN", "NSE") is None
        assert broker.get_quotes("INFY", "NSE") is None
        with pytest.raises(Exception, match="5paisa session expired"):
            broker.get_multiquotes([{"symbol": "SBIN", "exchange": "NSE"}])
        with pytest.raises(Exception, match="5paisa session expired"):
            broker.get_depth("SBIN", "NSE")

    http.assert_not_called()
    lookup.assert_not_called()
    assert warning.call_count == 1


def test_the_feed_waits_for_a_new_login_instead_of_connecting_with_an_expired_token():
    with mock.patch.object(BaseBrokerWebSocketAdapter, "__init__", lambda self: None):
        adapter = fa.FivepaisaWebSocketAdapter()
    adapter.user_id = "acct_fivepaisa_1"
    adapter.client_code = "C1"
    adapter._access_token = EXPIRED
    adapter.running = True
    adapter.EXPIRED_TOKEN_RECHECK_SECONDS = 0.01

    stored = iter([EXPIRED, EXPIRED, LIVE])  # still expired, then a new login
    built = []

    def make_client(access_token, client_code):
        client = mock.Mock()
        client.token = access_token

        def connected():
            adapter.running = False  # end the driver after the first real connect

        client.connect.side_effect = connected
        built.append(client)
        return client

    with (
        mock.patch.object(fa, "get_auth_token", side_effect=lambda *a, **k: next(stored, LIVE)),
        mock.patch.object(fa, "FivePaisaWebSocket", side_effect=make_client),
    ):
        adapter._connect_with_retry()

    connected = [c.token for c in built if c.connect.called]
    assert connected == [LIVE]
