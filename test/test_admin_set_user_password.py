"""Tests for POST /admin/api/users/<username>/password (admin password reset)."""

import atexit
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Env must be set before importing the DB modules (engine + pepper bind at import time).
TEST_DB = Path(__file__).resolve().parents[1] / "tmp" / "test_admin_set_user_password.db"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_DB.as_posix()}")
os.environ.setdefault("API_KEY_PEPPER", "a" * 64)
atexit.register(lambda: TEST_DB.unlink(missing_ok=True))

from flask import Flask  # noqa: E402

import database.auth_db as auth_db  # noqa: E402
import database.user_db as user_db  # noqa: E402
from blueprints.admin import admin_bp  # noqa: E402
from limiter import limiter  # noqa: E402

ADMIN = "pw-admin"
TARGET = "pw-target"
OTHER = "pw-other"
NEW_PASSWORD = "NewPassw0rd!x"
URL = f"/admin/api/users/{TARGET}/password"


@pytest.fixture()
def client():
    user_db.init_db()
    auth_db.init_db()
    user_db.User.query.delete()
    user_db.db_session.commit()
    user_db.add_user(ADMIN, "a@example.com", "AdminPassw0rd!", is_admin=True)
    user_db.add_user(TARGET, "t@example.com", "OldPassw0rd!x")
    user_db.add_user(OTHER, "o@example.com", "OtherPassw0rd!")

    app = Flask(__name__)
    app.secret_key = "test"
    app.config["RATELIMIT_ENABLED"] = False
    limiter.init_app(app)
    app.register_blueprint(admin_bp)
    yield app.test_client()

    user_db.User.query.delete()
    user_db.db_session.commit()
    user_db.username_cache.clear()


def login_as(client, username):
    with client.session_transaction() as sess:
        sess["user"] = username


def test_admin_sets_another_users_password(client):
    login_as(client, ADMIN)
    # Prime the 30-second login cache with the old password, then end that
    # "request" the way app.py's teardown does, so the cached User is a
    # detached copy holding the old hash rather than the object the reset
    # will modify.
    assert user_db.authenticate_user(TARGET, "OldPassw0rd!x")
    user_db.db_session.remove()

    resp = client.post(URL, json={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})

    assert resp.status_code == 200, resp.get_json()
    assert user_db.authenticate_user(TARGET, NEW_PASSWORD)
    assert not user_db.authenticate_user(TARGET, "OldPassw0rd!x")


def test_the_users_sessions_are_cleared(client, monkeypatch):
    login_as(client, ADMIN)
    cleared = []
    monkeypatch.setattr(auth_db, "clear_user_sessions", cleared.append)

    client.post(URL, json={"new_password": NEW_PASSWORD})

    assert cleared == [TARGET]


def test_non_admin_is_refused(client):
    login_as(client, OTHER)

    resp = client.post(URL, json={"new_password": NEW_PASSWORD})

    assert resp.status_code == 403
    assert user_db.authenticate_user(TARGET, "OldPassw0rd!x")


def test_admin_cannot_reset_their_own_password_here(client):
    login_as(client, ADMIN)

    resp = client.post(f"/admin/api/users/{ADMIN}/password", json={"new_password": NEW_PASSWORD})

    assert resp.status_code == 400
    assert user_db.authenticate_user(ADMIN, "AdminPassw0rd!")


def test_unknown_user_is_404(client):
    login_as(client, ADMIN)

    resp = client.post("/admin/api/users/nobody/password", json={"new_password": NEW_PASSWORD})

    assert resp.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"new_password": "weak"},
        {"new_password": NEW_PASSWORD, "confirm_password": "different"},
    ],
)
def test_bad_input_leaves_the_password_alone(client, body):
    login_as(client, ADMIN)

    resp = client.post(URL, json=body)

    assert resp.status_code == 400
    assert user_db.authenticate_user(TARGET, "OldPassw0rd!x")


def test_form_body_is_accepted(client):
    login_as(client, ADMIN)

    resp = client.post(
        URL, data={"new_password": NEW_PASSWORD}, headers={"Accept": "application/json"}
    )

    assert resp.status_code == 200, resp.get_json()
    assert user_db.authenticate_user(TARGET, NEW_PASSWORD)
