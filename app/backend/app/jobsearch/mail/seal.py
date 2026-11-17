"""Sealing the mail login's app password (derived from another project's secret-sealing code, see
app/PROVENANCE.md).

AES-256-GCM with a key worked out (HKDF-SHA256) from a random secret kept in a file in the install's secrets
(`settings.mail_key_file`), never in the database, so a copy of the database holds only ciphertext. Each value gets a
fresh nonce, and the owner it belongs to is bound in as associated data: a ciphertext cut out of one row and pasted into
another fails to open. The key file is made the first time a login is saved (chmod 600, never overwritten).

Stored form: `v1.<urlsafe base64 of nonce + ciphertext + tag>`. Nothing here logs, prints or puts a value into an
exception message."""

from __future__ import annotations

import base64
import contextlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.settings import settings

VERSION = "v1"
KEY_MIN_CHARS = 32
_NONCE_BYTES = 12
_INFO = b"resumebot-mail-login-v1"


class SealUnavailable(Exception):
    """The login can't be kept or opened right now. The message is the plain reason, safe to show."""


def _path() -> Path:
    return Path(settings.mail_key_file)


def _read() -> str:
    try:
        return _path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _make() -> None:
    """Make the key file if it is missing (the first save). Never replaces one that exists."""
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    except OSError:
        raise SealUnavailable(
            "resumebot can't keep a mail login yet: this install's secrets folder can't be written to.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii") + "\n")
    with contextlib.suppress(OSError):  # a file system with no modes (Windows) keeps the folder's own
        path.chmod(0o600)


def _key(*, make: bool) -> bytes:
    raw = _read()
    if not raw and make:
        _make()
        raw = _read()
    if len(raw) < KEY_MIN_CHARS:
        raise SealUnavailable(
            "resumebot can't open the saved mail login: this install's mail key is missing. Save the login again.")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(raw.encode("utf-8"))


def _aad(owner: str) -> bytes:
    return f"mail-login|{owner}".encode()


def seal(value: str, *, owner: str) -> str:
    """The value, encrypted (the key is made if there is none). Raises `SealUnavailable` when it can't be."""
    nonce = os.urandom(_NONCE_BYTES)
    box = AESGCM(_key(make=True)).encrypt(nonce, value.encode("utf-8"), _aad(owner))
    return f"{VERSION}." + base64.urlsafe_b64encode(nonce + box).decode("ascii")


def unseal(token: str, *, owner: str) -> str:
    """The value, decrypted: only for the one read of the mail that needs it."""
    key = _key(make=False)
    try:
        version, _, body = token.partition(".")
        if version != VERSION:
            raise ValueError
        raw = base64.urlsafe_b64decode(body.encode("ascii"))
        plain = AESGCM(key).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _aad(owner))
    except (ValueError, InvalidTag):
        raise SealUnavailable(
            "resumebot can't open the saved mail login (the key may have changed). Save the login again.") from None
    return plain.decode("utf-8")
