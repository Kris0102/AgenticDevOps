"""Fernet encryption for settings secrets (provider API keys, GitHub token).

Threat model (self-hosted, single-user — ADR 0020/0021): the win is that DB dumps,
`scripts/snapshot_db.sh` snapshots, and pgAdmin browsing never expose raw tokens.
It does NOT defend against an attacker who owns the machine — the key lives beside
the data. That trade-off is deliberate for a localhost tool.

Key resolution, in order:
1. `SETTINGS_ENCRYPTION_KEY` env var (urlsafe-base64 Fernet key) — set this to keep
   the key out of the repo directory entirely.
2. `.settings_key` file in the project root — auto-generated on first use (0600,
   gitignored). Deleting it orphans every stored secret (they decrypt to None and
   must be re-entered in the UI).

Values are stored as `enc:<fernet-token>`; `decrypt` passes through legacy plaintext
values untouched so pre-encryption rows keep working until the next save re-encrypts.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from observability import Category, get_logger

_log = get_logger(Category.DB)

_PREFIX = "enc:"
# project root = src/db/secrets.py -> ../../
_KEY_FILE = Path(__file__).resolve().parents[2] / ".settings_key"

_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    env_key = os.getenv("SETTINGS_ENCRYPTION_KEY")
    if env_key:
        _fernet = Fernet(env_key.encode())
        return _fernet
    if _KEY_FILE.exists():
        _fernet = Fernet(_KEY_FILE.read_bytes().strip())
        return _fernet
    key = Fernet.generate_key()
    _KEY_FILE.touch(mode=0o600)
    _KEY_FILE.write_bytes(key)
    _log.info("secrets.key_generated", path=str(_KEY_FILE))
    _fernet = Fernet(key)
    return _fernet


def encrypt(value: Optional[str]) -> Optional[str]:
    """None/empty pass through; already-encrypted values are not double-wrapped."""
    if not value or value.startswith(_PREFIX):
        return value
    return _PREFIX + _get_fernet().encrypt(value.encode()).decode()


def decrypt(value: Optional[str]) -> Optional[str]:
    """`enc:` values are decrypted; anything else (legacy plaintext, None) passes
    through. A token that fails to decrypt (rotated/lost key) becomes None so the
    caller treats the secret as unset instead of using ciphertext as a key."""
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        return _get_fernet().decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        _log.warning("secrets.decrypt_failed", hint="key rotated or .settings_key lost — re-enter the secret in Settings")
        return None
