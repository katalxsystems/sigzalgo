"""
Portfolio Basket API: auth resolution, ownership 404s, and request routing.

Loaded the same isolated way as test_portfolio_api.py -- a bare Flask app with
just this namespace mounted, service functions monkeypatched -- so this tests
the API layer's own logic (auth resolution, error shape, kwarg wiring) without
a real database or broker session. CRUD correctness against a real database is
test_basket_db.py's job; engine correctness is test_basket_engine.py's.
"""

import importlib.util
from pathlib import Path

import pytest
from flask import Flask
from flask_restx import Api

from limiter import limiter

_MODULE_PATH = Path(__file__).parents[1] / "restx_api" / "basket.py"
_SPEC = importlib.util.spec_from_file_location("basket_api_under_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
basket_api = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(basket_api)


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
    monkeypatch.setattr(limiter, "enabled", False)
    rest_api = Api(app)
    rest_api.add_namespace(basket_api.api, path="/basket")
    return app.test_client()


def holdings_body(**overrides):
    body = {
        "apikey": "key",
        "name": "Core",
        "holdings": [{"symbol": "INFY", "exchange": "NSE", "weight": 100}],
        "effective_date": "2024-01-01",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/basket/create", holdings_body()),
        ("/basket/list", {"apikey": "key"}),
        ("/basket/get", {"apikey": "key", "basket_id": 1}),
        (
            "/basket/rebalance",
            {
                "apikey": "key",
                "basket_id": 1,
                "holdings": [{"symbol": "INFY", "exchange": "NSE", "weight": 100}],
                "effective_date": "2024-02-01",
            },
        ),
        ("/basket/update", {"apikey": "key", "basket_id": 1, "name": "New"}),
        ("/basket/delete", {"apikey": "key", "basket_id": 1}),
        ("/basket/backtest", {"apikey": "key", "basket_id": 1}),
    ],
)
def test_every_endpoint_rejects_an_invalid_api_key(client, monkeypatch, path, body):
    monkeypatch.setattr(basket_api, "verify_api_key", lambda _key: None)

    response = client.post(path, json=body)

    assert response.status_code == 403
    assert response.get_json()["message"] == "Invalid openalgo apikey"


def test_orphaned_key_with_no_owner_is_refused(client, monkeypatch):
    # A valid, non-revoked key whose account_id nonetheless has no matching
    # Auth row -- e.g. a pre-multi-account key -- must not silently scope to
    # nothing, or (worse) to an empty-string owner shared by every such key.
    monkeypatch.setattr(basket_api, "verify_api_key", lambda _key: "orphan_account")
    monkeypatch.setattr(basket_api, "get_owner_username", lambda _account_id: None)

    response = client.post("/basket/list", json={"apikey": "key"})

    assert response.status_code == 403
    assert "account owner" in response.get_json()["message"]


def test_create_resolves_owner_and_passes_through_holdings(client, monkeypatch):
    monkeypatch.setattr(basket_api, "verify_api_key", lambda _key: "alice_fivepaisa_abc")
    monkeypatch.setattr(basket_api, "get_owner_username", lambda account_id: "alice")

    captured = {}

    def create(user_id, name, holdings, effective_date, **kwargs):
        captured["user_id"] = user_id
        captured["name"] = name
        captured["holdings"] = holdings
        captured["effective_date"] = effective_date
        return True, {"status": "success", "basket": {"id": 1}}, 201

    monkeypatch.setattr(basket_api, "create_basket", create)

    response = client.post("/basket/create", json=holdings_body())

    assert response.status_code == 201
    assert captured["user_id"] == "alice"
    assert captured["name"] == "Core"
    assert captured["effective_date"] == "2024-01-01"
    assert captured["holdings"] == [{"symbol": "INFY", "exchange": "NSE", "weight": 100.0}]


def test_get_missing_basket_is_a_404(client, monkeypatch):
    monkeypatch.setattr(basket_api, "verify_api_key", lambda _key: "alice_fivepaisa_abc")
    monkeypatch.setattr(basket_api, "get_owner_username", lambda account_id: "alice")
    monkeypatch.setattr(
        basket_api,
        "get_basket",
        lambda _user_id, _basket_id: (
            False,
            {"status": "error", "message": "Basket not found"},
            404,
        ),
    )

    response = client.post("/basket/get", json={"apikey": "key", "basket_id": 999})

    assert response.status_code == 404
    assert response.get_json()["message"] == "Basket not found"


def test_backtest_requires_broker_session_for_source_api(client, monkeypatch):
    monkeypatch.setattr(basket_api, "verify_api_key", lambda _key: "alice_fivepaisa_abc")
    monkeypatch.setattr(basket_api, "get_owner_username", lambda account_id: "alice")
    monkeypatch.setattr(basket_api, "get_auth_token_broker", lambda *_a, **_k: (None, None, None))

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("backtest ran without a broker session")

    monkeypatch.setattr(basket_api, "run_basket_backtest", should_not_run)

    response = client.post(
        "/basket/backtest", json={"apikey": "key", "basket_id": 1, "source": "api"}
    )

    assert response.status_code == 403
    assert "No broker session" in response.get_json()["message"]


def test_backtest_propagates_feed_token_and_dates(client, monkeypatch):
    monkeypatch.setattr(basket_api, "verify_api_key", lambda _key: "alice_fivepaisa_abc")
    monkeypatch.setattr(basket_api, "get_owner_username", lambda account_id: "alice")
    monkeypatch.setattr(
        basket_api,
        "get_auth_token_broker",
        lambda *_a, **_k: ("auth", "feed", "xts-broker"),
    )

    captured = {}

    def run(user_id, basket_id, **kwargs):
        captured["user_id"] = user_id
        captured["basket_id"] = basket_id
        captured.update(kwargs)
        return True, {"status": "success"}, 200

    monkeypatch.setattr(basket_api, "run_basket_backtest", run)

    response = client.post(
        "/basket/backtest",
        json={
            "apikey": "key",
            "basket_id": 7,
            "source": "api",
            "end_date": "2024-12-31",
            "risk_free_rate": 0.05,
        },
    )

    assert response.status_code == 200
    assert captured["user_id"] == "alice"
    assert captured["basket_id"] == 7
    assert captured["auth_token"] == "auth"
    assert captured["feed_token"] == "feed"
    assert captured["broker"] == "xts-broker"
    assert captured["end_date"] == "2024-12-31"
    assert captured["risk_free_rate"] == 0.05


def test_update_rejects_empty_body(client, monkeypatch):
    monkeypatch.setattr(basket_api, "verify_api_key", lambda _key: "alice_fivepaisa_abc")
    monkeypatch.setattr(basket_api, "get_owner_username", lambda account_id: "alice")

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("update ran with nothing to update")

    monkeypatch.setattr(basket_api, "update_basket", should_not_run)

    response = client.post("/basket/update", json={"apikey": "key", "basket_id": 1})

    assert response.status_code == 400
    assert "no fields to update" in response.get_json()["message"]
