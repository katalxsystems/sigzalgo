"""Read-only audit: which accounts have per-account broker credentials set,
and which are silently relying on the instance-wide fallback. Never prints
decrypted secret values -- only presence/absence and format validity."""

from app import app
from database.auth_db import Auth, decrypt_token
from utils.config import COMPOUND_BROKER_API_KEY_FORMATS, get_broker_api_key

with app.app_context():
    accounts = Auth.query.order_by(Auth.owner_username.asc(), Auth.broker.asc()).all()

    print(f"{'owner_username':<20} {'account_id':<30} {'broker':<15} {'per-account key?':<18} {'format ok?':<10} {'revoked'}")
    print("-" * 110)

    fallback_bad_brokers = set()

    for a in accounts:
        has_own_key = bool(a.broker_api_key)
        key_to_check = None
        source = "none"

        if has_own_key:
            try:
                key_to_check = decrypt_token(a.broker_api_key)
                source = "account"
            except Exception:
                key_to_check = None
        else:
            resolved = get_broker_api_key(a.name)
            if resolved:
                key_to_check = resolved
                source = "instance-fallback"

        fmt_ok = "n/a"
        spec = COMPOUND_BROKER_API_KEY_FORMATS.get((a.broker or "").strip().lower())
        if spec and key_to_check:
            required_seps, _example = spec
            fmt_ok = "OK" if key_to_check.count(":::") == required_seps else "BAD"
            if fmt_ok == "BAD" and source == "instance-fallback":
                fallback_bad_brokers.add(a.broker)
        elif spec and not key_to_check:
            fmt_ok = "MISSING"

        print(
            f"{(a.owner_username or ''):<20} {a.name:<30} {a.broker:<15} "
            f"{source:<18} {fmt_ok:<10} {a.is_revoked}"
        )

    print()
    if fallback_bad_brokers:
        print(f"Brokers where the instance-wide fallback key is WRONGLY FORMATTED: {fallback_bad_brokers}")
    else:
        print("No broker currently has a malformed instance-wide fallback key in use (for accounts with no per-account key).")

    # Also show what the raw instance-wide key looks like format-wise, without
    # printing the actual secret.
    print()
    print("Instance-wide fallback key (used when an account has none of its own):")
    instance_key = get_broker_api_key(None)
    if instance_key:
        print(f"  present, length={len(instance_key)}, colon-groups={instance_key.count(':::')}")
    else:
        print("  not set")
