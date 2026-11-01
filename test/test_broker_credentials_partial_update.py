"""set_broker_credentials must not clear the credential it was not given.

POST /api/accounts/<id>/credentials (and the Profile > Accounts edit dialog,
which sends only the field the user changed) passes None for the other one.
Writing None used to wipe it: rotating just the app key deleted the secret,
and the broker login then failed with a missing-secret error.
"""

import atexit
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Env must be set before importing auth_db (engine + PEPPER bind at import time).
TEST_DB = Path(__file__).resolve().parents[1] / "tmp" / "test_broker_credentials_partial.db"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_DB.as_posix()}")
os.environ.setdefault("API_KEY_PEPPER", "a" * 64)
atexit.register(lambda: TEST_DB.unlink(missing_ok=True))

import database.auth_db as auth_db  # noqa: E402


@pytest.fixture()
def account():
    auth_db.init_db()
    auth_db.Auth.query.delete()
    auth_db.db_session.commit()
    account_id = auth_db.create_broker_account(
        "creds-user", "angel", broker_api_key="key-1", broker_api_secret="secret-1"
    )
    yield account_id
    auth_db.Auth.query.delete()
    auth_db.db_session.commit()
    auth_db.db_session.remove()


def test_updating_only_the_key_keeps_the_secret(account):
    assert auth_db.set_broker_credentials(account, "key-2", None)

    assert auth_db.get_broker_credentials(account) == ("key-2", "secret-1")


def test_updating_only_the_secret_keeps_the_key(account):
    assert auth_db.set_broker_credentials(account, None, "secret-2")

    assert auth_db.get_broker_credentials(account) == ("key-1", "secret-2")


def test_unknown_account_is_reported(account):
    assert auth_db.set_broker_credentials("no-such-account", "key-2", None) is False
