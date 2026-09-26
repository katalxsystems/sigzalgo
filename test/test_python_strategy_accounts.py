"""Per-account Python strategies: users see and manage only their active
account's strategies, administrators see all; the running script gets its own
account's API key and its params."""

import json
import types
from unittest import mock

import pytest
from flask import Flask

import blueprints.python_strategy as ps

ALICE, ALICE_ACCT = "alice_s", "alice_s_angel_1"
BOB, BOB_ACCT = "bob_s", "bob_s_zerodha_1"
ADMIN, ADMIN_ACCT = "admin_s", "admin_s_angel_1"
KEYS = {ALICE_ACCT: "KEY-ALICE", BOB_ACCT: "KEY-BOB", ADMIN_ACCT: "KEY-ADMIN"}


@pytest.fixture
def app(monkeypatch):
    configs = {
        "alice_strat": {"name": "A", "user_id": ALICE, "account_id": ALICE_ACCT, "params": {"qty": 1}},
        "bob_strat": {"name": "B", "user_id": BOB, "account_id": BOB_ACCT},
        "legacy_strat": {"name": "L"},  # no owner at all
    }
    monkeypatch.setattr(ps, "STRATEGY_CONFIGS", configs)
    monkeypatch.setattr(ps, "save_configs", lambda: None)
    monkeypatch.setattr(ps, "cleanup_dead_processes", lambda: None)
    users = {ADMIN: True, ALICE: False, BOB: False}
    patches = [
        mock.patch("utils.session.is_session_valid", return_value=True),
        mock.patch(
            "database.user_db.find_user_by_exact_username",
            lambda name: types.SimpleNamespace(is_admin=users[name]) if name in users else None,
        ),
    ]
    for p in patches:
        p.start()
    application = Flask(__name__)
    application.secret_key = "test"
    application.register_blueprint(ps.python_strategy_bp)
    yield application
    for p in patches:
        p.stop()


def client_for(app, user, account):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = user
        sess["logged_in"] = True
        sess["active_account_id"] = account
    return client


def _ids(resp):
    return {s["id"] for s in resp.get_json()["strategies"]}


def test_users_list_only_their_account(app):
    assert _ids(client_for(app, ALICE, ALICE_ACCT).get("/python/api/strategies")) == {"alice_strat"}
    assert _ids(client_for(app, BOB, BOB_ACCT).get("/python/api/strategies")) == {"bob_strat"}
    assert _ids(client_for(app, ALICE, "alice_s_other_2").get("/python/api/strategies")) == set()


def test_admin_lists_all_with_owner(app):
    resp = client_for(app, ADMIN, ADMIN_ACCT).get("/python/api/strategies").get_json()["strategies"]
    by_id = {s["id"]: s for s in resp}
    assert set(by_id) == {"alice_strat", "bob_strat", "legacy_strat"}
    assert (by_id["bob_strat"]["account_id"], by_id["bob_strat"]["owner_username"]) == (BOB_ACCT, BOB)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/python/api/strategy/{id}"),
        ("get", "/python/api/strategy/{id}/content"),
        ("get", "/python/api/logs/{id}"),
        ("get", "/python/api/logs/{id}/x.log"),
        ("post", "/python/start/{id}"),
        ("post", "/python/stop/{id}"),
        ("post", "/python/delete/{id}"),
        ("get", "/python/params/{id}"),
        ("post", "/python/params/{id}"),
    ],
)
def test_other_accounts_strategies_are_not_found(app, method, path):
    alice = client_for(app, ALICE, ALICE_ACCT)
    for sid in ("bob_strat", "legacy_strat"):
        resp = getattr(alice, method)(path.format(id=sid), json={"params": {}})
        assert resp.status_code == 404, (path, sid, resp.status_code)
    assert "bob_strat" in ps.STRATEGY_CONFIGS


def test_status_counts_only_visible(app):
    body = client_for(app, ALICE, ALICE_ACCT).get("/python/status").get_json()
    assert body["total"] == 1 and [s["id"] for s in body["strategies"]] == ["alice_strat"]


def test_params_round_trip_and_validation(app):
    alice = client_for(app, ALICE, ALICE_ACCT)
    assert alice.get("/python/params/alice_strat").get_json()["params"] == {"qty": 1}
    resp = alice.post("/python/params/alice_strat", json={"params": {"qty": 5, "symbols": ["SBIN"]}})
    assert resp.status_code == 200
    assert ps.STRATEGY_CONFIGS["alice_strat"]["params"] == {"qty": 5, "symbols": ["SBIN"]}
    assert alice.post("/python/params/alice_strat", json={"params": [1, 2]}).status_code == 400
    too_big = {"blob": "x" * (ps.MAX_PARAMS_BYTES + 1)}
    assert alice.post("/python/params/alice_strat", json={"params": too_big}).status_code == 400


def test_admin_can_read_any_log_and_params(app):
    admin = client_for(app, ADMIN, ADMIN_ACCT)
    assert admin.get("/python/params/bob_strat").status_code == 200
    assert admin.get("/python/api/logs/bob_strat").status_code == 200


def test_start_injects_account_key_and_params(app, monkeypatch, tmp_path):
    script = tmp_path / "alice_strat.py"
    script.write_text("print('hi')\n")
    ps.STRATEGY_CONFIGS["alice_strat"]["file_path"] = str(script)
    captured = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr(ps.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ps, "LOGS_DIR", tmp_path)
    with (
        mock.patch("database.auth_db.get_api_key_for_tradingview", lambda acct: KEYS.get(acct)),
        mock.patch.object(ps, "check_master_contract_ready", return_value=(True, "ready"), create=True),
    ):
        ok, _ = ps.start_strategy_process("alice_strat")
    assert ok, _
    env = captured["env"]
    assert env["OPENALGO_API_KEY"] == "KEY-ALICE"
    assert env["OPENALGO_ACCOUNT_ID"] == ALICE_ACCT
    assert json.loads(env["STRATEGY_PARAMS"]) == {"qty": 1}


def test_backfill_assigns_owner_default_account(app):
    ps.STRATEGY_CONFIGS["old"] = {"name": "O", "user_id": BOB}
    with mock.patch("database.auth_db.get_default_account_id", lambda user: {BOB: BOB_ACCT}.get(user)):
        ps._backfill_strategy_accounts()
    assert ps.STRATEGY_CONFIGS["old"]["account_id"] == BOB_ACCT
    assert ps.STRATEGY_CONFIGS["legacy_strat"].get("account_id") is None
