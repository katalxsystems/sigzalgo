"""Per-account Flow: users see and manage only their active account's
workflows; administrators see every workflow and every execution log."""

import types
from unittest import mock

import pytest
from flask import Flask

from blueprints.flow import flow_bp
from database import flow_db
from database.flow_db import (
    FlowWorkflow,
    create_execution,
    create_workflow,
    db_session,
    get_workflow,
    init_db,
    update_execution_status,
)

ALICE, ALICE_ACCT = "alice_t", "alice_t_angel_1"
BOB, BOB_ACCT = "bob_t", "bob_t_zerodha_1"
ADMIN, ADMIN_ACCT = "admin_t", "admin_t_angel_1"
OWNERS = {ALICE_ACCT: ALICE, BOB_ACCT: BOB, ADMIN_ACCT: ADMIN}
KEYS = {ALICE_ACCT: "KEY-ALICE", BOB_ACCT: "KEY-BOB", ADMIN_ACCT: "KEY-ADMIN"}


@pytest.fixture
def app():
    init_db()
    application = Flask(__name__)
    application.secret_key = "test"
    application.register_blueprint(flow_bp)

    users = {ADMIN: True, ALICE: False, BOB: False}
    patches = [
        mock.patch(
            "database.user_db.find_user_by_exact_username",
            lambda name: types.SimpleNamespace(is_admin=users[name]) if name in users else None,
        ),
        mock.patch("database.auth_db.get_owner_username", lambda acct: OWNERS.get(acct)),
        mock.patch("blueprints.flow.get_api_key_for_tradingview", lambda acct: KEYS.get(acct)),
    ]
    for p in patches:
        p.start()
    yield application
    for p in patches:
        p.stop()
    FlowWorkflow.query.filter(FlowWorkflow.name.like("own-test-%")).delete(
        synchronize_session=False
    )
    db_session.commit()
    db_session.remove()


def client_for(app, user, account):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = user
        sess["active_account_id"] = account
    return client


def _names(resp):
    return {w["name"] for w in resp.get_json() if w["name"].startswith("own-test-")}


@pytest.fixture
def flows(app):
    a = client_for(app, ALICE, ALICE_ACCT).post("/flow/api/workflows", json={"name": "own-test-a"})
    b = client_for(app, BOB, BOB_ACCT).post("/flow/api/workflows", json={"name": "own-test-b"})
    legacy = create_workflow("own-test-legacy")  # pre-per-account, never activated
    return a.get_json()["id"], b.get_json()["id"], legacy.id


def test_create_records_the_current_account(app, flows):
    a_id, b_id, _ = flows
    assert (get_workflow(a_id).account_id, get_workflow(a_id).owner_username) == (ALICE_ACCT, ALICE)
    assert (get_workflow(b_id).account_id, get_workflow(b_id).owner_username) == (BOB_ACCT, BOB)


def test_users_list_only_their_account(app, flows):
    assert _names(client_for(app, ALICE, ALICE_ACCT).get("/flow/api/workflows")) == {"own-test-a"}
    assert _names(client_for(app, BOB, BOB_ACCT).get("/flow/api/workflows")) == {"own-test-b"}


def test_admin_lists_everything_with_owners(app, flows):
    items = client_for(app, ADMIN, ADMIN_ACCT).get("/flow/api/workflows").get_json()
    mine = {w["name"]: w for w in items if w["name"].startswith("own-test-")}
    assert set(mine) == {"own-test-a", "own-test-b", "own-test-legacy"}
    assert mine["own-test-a"]["owner_username"] == ALICE
    assert mine["own-test-legacy"]["account_id"] is None


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/flow/api/workflows/{id}"),
        ("put", "/flow/api/workflows/{id}"),
        ("delete", "/flow/api/workflows/{id}"),
        ("post", "/flow/api/workflows/{id}/activate"),
        ("post", "/flow/api/workflows/{id}/execute"),
        ("get", "/flow/api/workflows/{id}/executions"),
        ("get", "/flow/api/workflows/{id}/webhook"),
        ("post", "/flow/api/workflows/{id}/webhook/regenerate"),
        ("get", "/flow/api/workflows/{id}/export"),
    ],
)
def test_other_accounts_workflows_are_not_found(app, flows, method, path):
    _, b_id, legacy_id = flows
    alice = client_for(app, ALICE, ALICE_ACCT)
    for wid in (b_id, legacy_id):
        resp = getattr(alice, method)(path.format(id=wid), json={"name": "x"})
        assert resp.status_code == 404, (path, wid, resp.status_code)
    assert get_workflow(b_id) is not None  # the DELETE did nothing


def test_same_user_other_account_is_separate(app, flows):
    a_id, _, _ = flows
    other = client_for(app, ALICE, "alice_t_fivepaisa_2")
    assert other.get(f"/flow/api/workflows/{a_id}").status_code == 404


def test_admin_sees_any_execution_log(app, flows):
    _, b_id, _ = flows
    ex = create_execution(b_id, status="running")
    update_execution_status(ex.id, "completed", logs=[{"message": "bob ran"}])
    admin = client_for(app, ADMIN, ADMIN_ACCT)
    logs = admin.get(f"/flow/api/workflows/{b_id}/executions").get_json()
    assert logs and logs[0]["logs"][0]["message"] == "bob ran"
    assert client_for(app, BOB, BOB_ACCT).get(f"/flow/api/workflows/{b_id}/executions").status_code == 200


def test_update_cannot_change_ownership_or_activation(app, flows):
    a_id, _, _ = flows
    alice = client_for(app, ALICE, ALICE_ACCT)
    resp = alice.put(
        f"/flow/api/workflows/{a_id}",
        json={"name": "own-test-a2", "account_id": BOB_ACCT, "is_active": True, "api_key": "KEY-BOB"},
    )
    assert resp.status_code == 200
    wf = get_workflow(a_id)
    db_session.refresh(wf)
    assert (wf.name, wf.account_id, wf.is_active, wf.api_key) == ("own-test-a2", ALICE_ACCT, False, None)


def test_run_uses_the_workflows_own_account_key(app, flows):
    _, b_id, _ = flows
    admin = client_for(app, ADMIN, ADMIN_ACCT)
    with (
        mock.patch("blueprints.flow._execution_blocked", return_value=None),
        mock.patch(
            "services.flow_executor_service.execute_workflow",
            lambda wid, api_key=None: {"status": "success", "used": api_key},
        ),
    ):
        resp = admin.post(f"/flow/api/workflows/{b_id}/execute")
    assert resp.get_json()["used"] == "KEY-BOB"  # Bob's account, not the admin's


def test_unassigned_workflow_is_claimed_on_run(app, flows):
    _, _, legacy_id = flows
    admin = client_for(app, ADMIN, ADMIN_ACCT)
    with (
        mock.patch("blueprints.flow._execution_blocked", return_value=None),
        mock.patch(
            "services.flow_executor_service.execute_workflow",
            lambda wid, api_key=None: {"status": "success", "used": api_key},
        ),
    ):
        resp = admin.post(f"/flow/api/workflows/{legacy_id}/execute")
    assert resp.get_json()["used"] == "KEY-ADMIN"
    wf = get_workflow(legacy_id)
    db_session.refresh(wf)
    assert (wf.account_id, wf.owner_username) == (ADMIN_ACCT, ADMIN)


def test_backfill_assigns_from_stored_api_key(app):
    wf = create_workflow("own-test-backfill")
    wf.api_key = flow_db._encrypt_api_key("KEY-BOB")
    db_session.commit()
    fake_keys = [types.SimpleNamespace(api_key_encrypted="enc-bob", account_id=BOB_ACCT, user_id=BOB)]
    with (
        mock.patch("database.auth_db.ApiKeys") as api_keys,
        mock.patch("database.auth_db.decrypt_token", lambda enc: {"enc-bob": "KEY-BOB"}.get(enc)),
        mock.patch.object(flow_db, "get_workflow_api_key", lambda w: "KEY-BOB"),
    ):
        api_keys.query.all.return_value = fake_keys
        flow_db._migrate_add_ownership_columns()
    wf = get_workflow(wf.id)
    db_session.refresh(wf)
    assert (wf.account_id, wf.owner_username) == (BOB_ACCT, BOB)
