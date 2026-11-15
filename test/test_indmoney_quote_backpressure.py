"""IndMoney quotes answer inside the caller's timeout.

The CMS gave up after 20s ("cURL error 28") because IndMoney market-data
requests queued without a ceiling under eventlet, and every refused contract
cost three requests (full, LTP, depth) on every refresh.
"""

from __future__ import annotations

import pytest

import broker.indmoney.api.data as idata
import broker.indmoney.api.rate_limiter as rl
from utils.broker_backpressure import BrokerBusyError

INVALID = Exception(
    'Indmoney API HTTP Error 400: {"debug_info":"Invalid scrip codes or mode","message":"Bad Request"}'
)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(rl, "_next_free", {})
    monkeypatch.setattr(idata, "_BAD_SCRIP_CODES", {})


# --- the queue ceiling ----------------------------------------------------------


def test_a_quote_whose_turn_is_too_far_away_is_refused_without_booking(monkeypatch):
    monkeypatch.setattr(rl.time, "monotonic", lambda: 100.0)
    rl._next_free["quote"] = 100.0 + rl.DATA_MAX_WAIT_SECONDS + 1
    with pytest.raises(BrokerBusyError):
        rl.apply_rate_limit("quote")
    # No slot was reserved, so the callers behind it are not pushed back.
    assert rl._next_free["quote"] == 100.0 + rl.DATA_MAX_WAIT_SECONDS + 1


def test_a_quote_within_the_ceiling_still_waits_its_turn(monkeypatch):
    slept = []
    monkeypatch.setattr(rl.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(rl.time, "sleep", slept.append)
    rl._next_free["quote"] = 102.0
    rl.apply_rate_limit("quote")
    assert slept == [2.0]


def test_orders_are_not_bounded_here(monkeypatch):
    slept = []
    monkeypatch.setattr(rl.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(rl.time, "sleep", slept.append)
    rl._next_free["order"] = 100.0 + 60
    rl.apply_rate_limit("order")
    assert slept == [60.0]


def test_a_long_server_backoff_is_not_slept_through(monkeypatch):
    class Resp:
        status_code = 429
        headers = {"Retry-After": "30"}

    class Client:
        def request(self, *a, **k):
            return Resp()

    monkeypatch.setattr(rl.time, "sleep", lambda s: pytest.fail("slept through a long backoff"))
    with pytest.raises(BrokerBusyError, match="slow down"):
        rl.rate_limited_request(Client(), "GET", "https://api.indstocks.com/market/quotes/ltp")


# --- refused contracts ------------------------------------------------------------


@pytest.fixture
def handler(monkeypatch):
    h = object.__new__(idata.BrokerData)
    h.auth_token = "tok"
    monkeypatch.setattr(h, "_get_scrip_code", lambda s, e: "NFO_35295", raising=False)
    calls = []

    def api(endpoint, auth, method="GET", params=None):
        calls.append(endpoint)
        raise INVALID

    monkeypatch.setattr(idata, "get_api_response", api)
    h.calls = calls
    return h


def test_a_refused_contract_costs_one_request_then_none(handler):
    first = handler.get_quotes("BEL23NOV26220PE", "NFO")
    assert first["error"] == idata.UNQUOTABLE_MESSAGE
    assert handler.calls == ["/market/quotes/full"]  # no LTP / depth fallbacks

    second = handler.get_quotes("BEL23NOV26220PE", "NFO")
    assert second["error"] == idata.UNQUOTABLE_MESSAGE
    assert handler.calls == ["/market/quotes/full"]  # answered from memory


def test_depth_and_history_skip_a_known_refused_contract(handler):
    idata._mark_bad("NFO_35295")
    with pytest.raises(Exception, match="does not provide a live price"):
        handler.get_depth("BEL23NOV26220PE", "NFO")
    assert handler.calls == []


def test_a_busy_refusal_is_not_mistaken_for_a_bad_code(handler, monkeypatch):
    def busy(*a, **k):
        raise BrokerBusyError()

    monkeypatch.setattr(idata, "get_api_response", busy)
    with pytest.raises(BrokerBusyError):
        handler._fetch_full_quotes_map(["NFO_1", "NFO_2"])
    assert not idata._BAD_SCRIP_CODES
