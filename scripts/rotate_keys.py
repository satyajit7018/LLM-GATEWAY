#!/usr/bin/env python3
"""CLI utility to re-encrypt user provider API keys when rotating APP_ENCRYPTION_KEY.

Usage:
  python scripts/rotate_keys.py --old-key <OLD_FERNET_KEY> --new-key <NEW_FERNET_KEY> [--db-path app_data.db] [--dry-run]
"""
import argparse
import os
import sqlite3
import sys

from cryptography.fernet import Fernet, InvalidToken


def rotate_user_keys(old_key: str, new_key: str, db_path: str = "app_data.db", dry_run: bool = False) -> int:
    try:
        f_old = Fernet(old_key.encode() if isinstance(old_key, str) else old_key)
    except Exception as exc:
        raise ValueError(f"Invalid old Fernet key: {exc}") from exc

    try:
        f_new = Fernet(new_key.encode() if isinstance(new_key, str) else new_key)
    except Exception as exc:
        raise ValueError(f"Invalid new Fernet key: {exc}") from exc

    if not os.path.exists(db_path) and db_path != ":memory:":
        raise FileNotFoundError(f"Database not found at {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    rows = conn.execute("SELECT user_id, provider, ciphertext FROM user_keys").fetchall()
    if not rows:
        print(f"No user keys found in {db_path}.")
        return 0

    re_encrypted = []
    for user_id, provider, ciphertext in rows:
        try:
            plaintext = f_old.decrypt(ciphertext)
        except InvalidToken:
            print(f"WARNING: Could not decrypt key for user_id={user_id}, provider={provider} with old key. Skipping.", file=sys.stderr)
            continue
        new_ciphertext = f_new.encrypt(plaintext)
        re_encrypted.append((new_ciphertext, user_id, provider))

    if dry_run:
        print(f"[DRY-RUN] Successfully verified and re-encrypted {len(re_encrypted)} of {len(rows)} key(s). No changes written.")
        return len(re_encrypted)

    conn.execute("BEGIN IMMEDIATE")
    try:
        for new_ct, uid, prov in re_encrypted:
            conn.execute(
                "UPDATE user_keys SET ciphertext = ? WHERE user_id = ? AND provider = ?",
                (new_ct, uid, prov)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"Successfully rotated {len(re_encrypted)} of {len(rows)} user key(s) in {db_path}.")
    return len(re_encrypted)


def main():
    parser = argparse.ArgumentParser(description="Rotate Fernet encryption keys for stored BYO user API keys.")
    parser.add_argument("--old-key", default=os.getenv("OLD_ENCRYPTION_KEY") or os.getenv("APP_ENCRYPTION_KEY"),
                        help="Previous Fernet encryption key")
    parser.add_argument("--new-key", default=os.getenv("NEW_ENCRYPTION_KEY"),
                        help="New Fernet encryption key to migrate to")
    parser.add_argument("--db-path", default=os.getenv("AUTH_DB_PATH", "app_data.db"),
                        help="Path to SQLite database (default: app_data.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Verify decryption and preview migration without committing changes")

    args = parser.parse_args()
    if not args.old_key:
        parser.error("Must provide --old-key or set OLD_ENCRYPTION_KEY / APP_ENCRYPTION_KEY in environment.")
    if not args.new_key:
        parser.error("Must provide --new-key or set NEW_ENCRYPTION_KEY in environment.")

    rotate_user_keys(args.old_key, args.new_key, args.db_path, args.dry_run)


if __name__ == "__main__":
    main()
