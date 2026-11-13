"""Sahi API keys with two-factor protection need a TOTP code at login.

Sahi refused such a key with "This API key needs a six-digit totp code"
because the login sent only the key id and secret. The base32 seed is now
stored with the secret as "secret:::seed" and the code generated at login.
"""

from __future__ import annotations

import pyotp
import pytest

import blueprints.broker_accounts as ba
import broker.sahi.api.auth_api as sahi

SEED = "JBSWY3DPEHPK3PXP"


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_request(client, method, url, bucket, json=None, headers=None, timeout=None):
        calls.append(json)
        return _Response(200, {"access_token": "tok", "expires_at": "x", "scope": "s"})

    monkeypatch.setattr(sahi, "sahi_request", fake_request)
    monkeypatch.setattr(sahi, "get_httpx_client", lambda: None)
    monkeypatch.setattr(sahi, "get_broker_api_key", lambda *a, **k: "pk_1")
    return calls


def _secret(monkeypatch, value):
    monkeypatch.setattr(sahi, "get_broker_api_secret", lambda *a, **k: value)


def test_a_stored_seed_sends_the_current_code(sent, monkeypatch):
    _secret(monkeypatch, f"sahi_sk_1:::{SEED}")
    assert sahi.authenticate_broker(account_id="a") == ("tok", None)
    assert sent[0]["secret"] == "sahi_sk_1"
    assert pyotp.TOTP(SEED).verify(sent[0]["totp"], valid_window=1)


def test_a_key_without_a_seed_sends_no_totp(sent, monkeypatch):
    _secret(monkeypatch, "sahi_sk_1")
    sahi.authenticate_broker(account_id="a")
    assert sent[0] == {"key_id": "pk_1", "secret": "sahi_sk_1"}


def test_a_seed_in_authenticator_layout_is_accepted(sent, monkeypatch):
    _secret(monkeypatch, "sahi_sk_1:::jbsw y3dp ehpk 3pxp")
    assert sahi.authenticate_broker(account_id="a")[0] == "tok"


def test_a_six_digit_code_saved_as_the_seed_is_explained(sent, monkeypatch):
    _secret(monkeypatch, "sahi_sk_1:::123456")
    token, error = sahi.authenticate_broker(account_id="a")
    assert token is None and "base32" in error and not sent


def test_the_totp_refusal_tells_the_trader_what_to_add():
    message = sahi._login_error(
        400, {"code": "InvalidRequest", "message": "This API key needs a six-digit totp code"}
    )
    assert "TOTP secret" in message


def test_the_account_api_packs_the_seed_with_the_secret():
    assert ba._pack_totp_seed("sahi", "sahi_sk_1", SEED.lower()) == (f"sahi_sk_1:::{SEED}", None)
    assert ba._pack_totp_seed("sahi", "sahi_sk_1", None) == ("sahi_sk_1", None)
    assert ba._pack_totp_seed("zerodha", "x", SEED)[1]
    assert ba._pack_totp_seed("sahi", "sahi_sk_1", "123456")[1]


def test_a_seed_alone_keeps_the_stored_secret(monkeypatch):
    import database.auth_db as auth_db

    monkeypatch.setattr(
        auth_db, "get_broker_credentials", lambda *a, **k: ("pk_1", "sahi_sk_1:::OLDSEEDOLDSEED22")
    )
    assert ba._pack_totp_seed("sahi", None, SEED, account_id="a") == (f"sahi_sk_1:::{SEED}", None)
