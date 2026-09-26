"""Copy Flow workflows from another OpenAlgo database into this one.

For moving flows built on an install running ``multiacc-peraccsession-backtester``
(typically its SQLite ``db/openalgo.db``) into the database this checkout
uses (``DATABASE_URL`` in ``.env`` -- SQLite or CockroachDB). The Flow schema
is identical on both branches, so this is a data copy, not a schema change.

    # Preview (default): prints what would be copied, writes nothing
    uv run python scripts/migrate_flows.py --source sqlite:///path/to/old/openalgo.db \\
        --source-env /path/to/old/.env

    # Copy
    uv run python scripts/migrate_flows.py --source ... --source-env ... --apply

What it copies, per workflow: name, description, nodes, edges, webhook token /
secret / settings, active flag and the API key it runs with; plus the
price_breach_calls linking workflows (ids remapped), and with
``--with-executions`` the execution history.

API keys. A workflow stores the OpenAlgo API key it runs with, Fernet-encrypted
with a key derived from that install's API_KEY_PEPPER and FERNET_SALT. The
key is decrypted with the SOURCE install's values (``--source-env``; defaults
to this install's own, i.e. same install/secrets), checked against THIS
database's API keys, and re-encrypted with this install's values:

- valid here      -> kept; the workflow keeps its active state
- valid here      -> the workflow belongs to that key's account (per-account Flow)
- not valid here  -> dropped, and the workflow is copied inactive and unassigned
  (visible to administrators); re-activate
  it in /flow (which stores the current key), or pass ``--api-key`` to assign
  one of this install's keys to every workflow instead

Schedules. ``schedule_job_id`` is not copied. On the next app start,
services/flow_scheduler_service.py re-registers the job of every active
scheduled workflow.

Idempotent: a workflow whose webhook token already exists here is skipped, so
the script can be re-run safely, and webhook URLs already configured in
TradingView/Chartink keep working.

Deliberately does not import ``app.py`` (see scripts/flow_diagnose.py); stop
the app while applying if the target is SQLite, to avoid write locks.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys

# Runnable from anywhere, including scripts/ itself.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _source_fernet(env_path: str | None):
    """Fernet for the SOURCE install, derived like database.auth_db.get_encryption_key."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from dotenv import dotenv_values

    values = dotenv_values(env_path) if env_path else os.environ
    pepper = values.get("API_KEY_PEPPER")
    if not pepper:
        sys.exit(f"API_KEY_PEPPER not found in {env_path or 'this environment'}")
    raw_salt = (values.get("FERNET_SALT") or "").strip()
    salt = b"openalgo_static_salt"
    if len(raw_salt) >= 32:
        try:
            salt = bytes.fromhex(raw_salt)
        except ValueError:
            pass
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(pepper.encode())))


def _decrypt_source_key(fernet, stored: str | None) -> str | None:
    """Plaintext API key from a source row (encrypted, or legacy plaintext)."""
    if not stored:
        return None
    try:
        return fernet.decrypt(stored.encode()).decode()
    except Exception:
        # Pre-encryption rows stored the key in plaintext; Fernet tokens start
        # with "gAAAAA", so anything else is a legacy plaintext key.
        return None if stored.startswith("gAAAAA") else stored


def _key_account(api_key: str | None) -> str | None:
    """The account an API key belongs to in THIS database, or None.

    Read-only on purpose: database.auth_db.verify_api_key records an unknown
    key as an invalid attempt from 127.0.0.1 (outside a request), which
    counts toward the IP auto-ban -- a migration full of old keys must not
    trip that.
    """
    if not api_key:
        return None
    from argon2.exceptions import VerificationError, VerifyMismatchError

    from database.auth_db import PEPPER, ApiKeys, ph

    peppered = api_key + PEPPER
    for row in ApiKeys.query.all():
        try:
            ph.verify(row.api_key_hash, peppered)
            return row.account_id or row.user_id
        except (VerifyMismatchError, VerificationError):
            continue
    return None


def _same_database(a: str, b: str) -> bool:
    def norm(url: str) -> str:
        if url.startswith("sqlite:///"):
            return "sqlite:///" + os.path.normcase(os.path.abspath(url[len("sqlite:///") :]))
        return url

    return norm(a) == norm(b)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", required=True, help="SQLAlchemy URL of the source database")
    parser.add_argument(
        "--source-env",
        help="The source install's .env (for its API_KEY_PEPPER/FERNET_SALT); "
        "defaults to this install's own values",
    )
    parser.add_argument(
        "--api-key",
        help="An OpenAlgo API key of THIS install to assign to every copied workflow",
    )
    parser.add_argument(
        "--with-executions", action="store_true", help="Also copy execution history"
    )
    parser.add_argument("--apply", action="store_true", help="Write (default: preview only)")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from database.auth_db import get_owner_username
    from database.flow_db import (
        FlowWorkflow,
        FlowWorkflowExecution,
        PriceBreachCall,
        _encrypt_api_key,
        db_session,
        init_db,
    )

    target_url = os.getenv("DATABASE_URL", "")
    if _same_database(args.source, target_url):
        sys.exit("Source and target are the same database; nothing to migrate.")

    if args.api_key and not _key_account(args.api_key):
        sys.exit("--api-key is not a valid API key in this install's database.")

    fernet = _source_fernet(args.source_env)
    source = Session(bind=create_engine(args.source))
    init_db()  # make sure the Flow tables exist in the target

    existing_tokens = {
        t for (t,) in db_session.query(FlowWorkflow.webhook_token).all() if t
    }
    id_map: dict[int, int] = {}
    selected: set[int] = set()  # source ids copied (or, in preview, that would be)
    copied = skipped = deactivated = 0

    try:
        for wf in source.query(FlowWorkflow).order_by(FlowWorkflow.id).all():
            if wf.webhook_token and wf.webhook_token in existing_tokens:
                print(f"skip   #{wf.id} {wf.name!r}: webhook token already present here")
                skipped += 1
                continue

            api_key = args.api_key or _decrypt_source_key(fernet, wf.api_key)
            account_id = _key_account(api_key)
            key_ok = bool(account_id)
            is_active = bool(wf.is_active) and key_ok
            note = ""
            if wf.api_key and not key_ok:
                note = " (API key not valid here: dropped, copied inactive)"
                deactivated += 1 if wf.is_active else 0

            print(f"copy   #{wf.id} {wf.name!r} active={is_active}{note}")
            copied += 1
            selected.add(wf.id)
            if not args.apply:
                continue

            new = FlowWorkflow(
                name=wf.name,
                description=wf.description,
                nodes=wf.nodes or [],
                edges=wf.edges or [],
                is_active=is_active,
                schedule_job_id=None,
                webhook_token=wf.webhook_token,
                webhook_secret=wf.webhook_secret,
                webhook_enabled=wf.webhook_enabled,
                webhook_auth_type=wf.webhook_auth_type,
                api_key=_encrypt_api_key(api_key) if key_ok else None,
                # Per-account Flow: owned by the account behind the key; with no
                # valid key it stays unassigned (administrators only) until
                # someone activates or runs it.
                account_id=account_id,
                owner_username=get_owner_username(account_id) if account_id else None,
                created_at=wf.created_at,
                updated_at=wf.updated_at,
            )
            db_session.add(new)
            db_session.flush()
            id_map[wf.id] = new.id

            if args.with_executions:
                for ex in wf.executions:
                    db_session.add(
                        FlowWorkflowExecution(
                            workflow_id=new.id,
                            status=ex.status,
                            started_at=ex.started_at,
                            completed_at=ex.completed_at,
                            logs=ex.logs,
                            error=ex.error,
                        )
                    )

        links = 0
        for call in source.query(PriceBreachCall).all():
            if call.sl_target_workflow_id not in selected:
                continue
            sl = id_map.get(call.sl_target_workflow_id)
            entry = call.entry_recross_workflow_id
            links += 1
            if args.apply:
                db_session.add(
                    PriceBreachCall(
                        call_id=call.call_id,
                        sl_target_workflow_id=sl,
                        entry_recross_workflow_id=id_map.get(entry) if entry else None,
                        created_at=call.created_at,
                    )
                )

        if args.apply:
            db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    finally:
        source.close()
        db_session.remove()

    mode = "Copied" if args.apply else "Would copy"
    print(
        f"\n{mode} {copied} workflow(s), {links} price-breach link(s); skipped {skipped}; "
        f"{deactivated} active workflow(s) need re-activating in /flow."
    )
    if not args.apply:
        print("Preview only. Re-run with --apply to write.")
    elif copied:
        print("Restart the app so active scheduled workflows get their jobs registered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
