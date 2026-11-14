"""CMS server-to-server broker login API (blueprints/cms_broker.py)."""

import pytest
from flask import Flask

from blueprints import cms_broker

SECRET = "test-shared-secret"
NONCE = "0123456789abcdef0123"


@pytest.fixture(autouse=True)
def _fresh_nonces(monkeypatch):
    monkeypatch.setattr(cms_broker, "_used_nonces", {})


def _sign(timestamp, nonce=NONCE, method="POST", path="/api/cms/broker/status", body=b"{}"):
    return cms_broker.compute_signature(SECRET, str(timestamp), nonce, method, path, body)


def _verify(signature, timestamp=1_000, nonce=NONCE, body=b"{}", now=1_000, secret=SECRET):
    return cms_broker.verify_signed_request(
        secret, str(timestamp), nonce, signature, "POST", "/api/cms/broker/status", body, now=now
    )


def test_valid_signature_is_accepted_once():
    signature = _sign(1_000)
    assert _verify(signature) is None
    assert _verify(signature) == "Replayed request."


def test_tampered_body_is_rejected():
    assert _verify(_sign(1_000), body=b'{"account_id": "someone-else"}') == "Invalid signature."


def test_signature_covers_the_path():
    signature = _sign(1_000, path="/api/cms/broker/login-url")
    assert _verify(signature) == "Invalid signature."


def test_stale_and_future_timestamps_are_rejected():
    assert "window" in _verify(_sign(1_000), timestamp=1_000, now=1_061)
    assert "window" in _verify(_sign(1_000), timestamp=1_000, now=939)


def test_rejected_request_does_not_burn_the_nonce():
    assert _verify("bad-signature") == "Invalid signature."
    assert _verify(_sign(1_000)) is None


def test_missing_secret_disables_the_api():
    assert "not configured" in _verify(_sign(1_000), secret="")


def test_short_nonce_is_rejected():
    assert _verify(_sign(1_000, nonce="short"), nonce="short") == "Malformed nonce."


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("https://signalz.in/broker-callback/zerodha/", True),
        ("http://localhost/broker-callback/zerodha/", True),
        ("http://signalz.in/broker-callback/zerodha/", False),
        ("javascript:alert(1)", False),
        ("", False),
    ],
)
def test_redirect_uri_validation(monkeypatch, uri, ok):
    monkeypatch.setattr(cms_broker, "CMS_BROKER_REDIRECT_HOSTS", set())
    assert (cms_broker._validate_redirect_uri(uri) is None) is ok


def test_redirect_host_allowlist(monkeypatch):
    monkeypatch.setattr(cms_broker, "CMS_BROKER_REDIRECT_HOSTS", {"signalz.in"})
    assert cms_broker._validate_redirect_uri("https://signalz.in/cb/") is None
    assert "not allowed" in cms_broker._validate_redirect_uri("https://evil.example/cb/")


def test_login_urls_carry_key_redirect_and_state():
    redirect = "https://signalz.in/broker-callback/upstox/"
    url, error = cms_broker.LOGIN_URL_BUILDERS["upstox"]("acc", "KEY", redirect, "st4te")
    assert error is None
    assert "client_id=KEY" in url and f"redirect_uri={redirect}" in url and "state=st4te" in url

    url, _ = cms_broker.LOGIN_URL_BUILDERS["zerodha"]("acc", "KITEKEY", redirect, "st4te")
    assert "api_key=KITEKEY" in url and "redirect_params=state%3Dst4te" in url

    url, _ = cms_broker.LOGIN_URL_BUILDERS["shoonya"]("acc", "USER:::CLIENT", redirect, "st4te")
    assert url.endswith("client_id=CLIENT")


def test_dhan_requires_packed_key():
    url, error = cms_broker.LOGIN_URL_BUILDERS["dhan"]("acc", "only-one-part", "https://x/", "s")
    assert url is None and "client_id:::api_key" in error


def _app_with(broker, auth_result, calls):
    app = Flask(__name__)

    def auth_function(*args, **kwargs):
        calls.append((args, kwargs))
        return auth_result

    app.broker_auth_functions = {f"{broker}_auth": auth_function}
    return app


@pytest.mark.parametrize(
    ("broker", "params", "auth_result", "expected_code"),
    [
        ("upstox", {"code": "C1"}, ("TOKEN", None), "C1"),
        ("fyers", {"auth_code": "C2"}, ("TOKEN", None), "C2"),
        ("paytm", {"requestToken": "C3"}, ("TOKEN", "FEED", None), "C3"),
        ("pocketful", {"code": "C4", "state": "s"}, ("TOKEN", None, "UID", None), "C4"),
        ("shoonya", {"code": "C5"}, ("TOKEN", None), "C5"),
        ("zebu", {"code": "C6"}, ("TOKEN", None), "C6"),
        ("tradesmart", {"request_token": "C7"}, ("TOKEN", None), "C7"),
    ],
)
def test_exchange_reads_each_brokers_callback_parameter(broker, params, auth_result, expected_code):
    calls = []
    app = _app_with(broker, auth_result, calls)
    with app.app_context():
        auth_token, _feed, _uid, error = cms_broker.exchange_callback(
            broker, "acc", params, "https://signalz.in/broker-callback/x/"
        )
    assert error is None and auth_token == "TOKEN"
    assert calls[0][0][0] == expected_code
    assert calls[0][1]["account_id"] == "acc"


def test_the_pocketful_login_url_carries_the_whole_redirect_uri():
    """A CMS callback with its own query string was cut at its first "&"."""
    from urllib.parse import parse_qs, urlparse

    callback = "https://www.signalz.co.in/broker-callback/?broker=pocketful&account=a1"
    url, error = cms_broker._pocketful_url("acc", "CLIENT", callback, "st")
    assert error is None
    query = parse_qs(urlparse(url).query)
    assert query["redirect_uri"] == [callback]
    assert query["client_id"] == ["CLIENT"] and query["state"] == ["st"]


def test_upstox_and_pocketful_get_the_cms_redirect_uri():
    for broker, params, result in (
        ("upstox", {"code": "C"}, ("T", None)),
        ("pocketful", {"code": "C"}, ("T", None, None, None)),
    ):
        calls = []
        with _app_with(broker, result, calls).app_context():
            cms_broker.exchange_callback(broker, "acc", params, "https://signalz.in/cb/")
        assert calls[0][1]["redirect_uri"] == "https://signalz.in/cb/"


def test_zerodha_token_is_prefixed_with_the_api_key(monkeypatch):
    monkeypatch.setattr(cms_broker, "get_broker_api_key", lambda *_a, **_k: "KITEKEY")
    calls = []
    with _app_with("zerodha", ("ACCESS", None), calls).app_context():
        auth_token, *_rest, error = cms_broker.exchange_callback(
            "zerodha", "acc", {"request_token": "R"}, None
        )
    assert error is None and auth_token == "KITEKEY:ACCESS"


def test_missing_code_and_broker_error_are_reported_without_calling_the_broker():
    calls = []
    with _app_with("upstox", ("T", None), calls).app_context():
        assert "did not return" in cms_broker.exchange_callback("upstox", "acc", {}, None)[3]
        denied = cms_broker.exchange_callback(
            "upstox", "acc", {"error": "access_denied", "code": "C"}, None
        )
        assert "access_denied" in denied[3]
    assert calls == []
