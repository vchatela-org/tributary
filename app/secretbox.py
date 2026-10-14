"""Authenticated encryption for the few secrets Tributary must keep at rest:
an OAuth connector's tokens (app/oauth.py), never a measurement.

The threat is a copy of the data folder (a volume backup, a NAS copy, `docker
cp`): a refresh token there would be a standing grant to someone's data. The
key lives elsewhere -- the environment, or a file mounted from a secret store
-- so a copy of the folder alone is useless. A process that holds the key (the
running app) is out of scope.

  - AES-256-GCM (`cryptography`'s AESGCM), a random 12-byte nonce per write.
  - A version byte first, so the format can change without guessing.
  - Associated data: the version and a `context` (the connector's key), so a
    sealed value moved to another connector's row does not open.
  - The key: TRIBUTARY_SECRET_KEY, the base64 of 32 random bytes
    (`openssl rand -base64 32`), or TRIBUTARY_SECRET_KEY_FILE, a file holding
    it. TRIBUTARY_SECRET_KEY_PREVIOUS, during a rotation: what it sealed still
    opens, and `open_` says so, so the caller seals it again with the current
    key.
  - **No key, no secret stored.** There is no plain-text fallback: `seal`
    raises, and whatever needs a stored secret refuses to start (a connector
    asks for the key before it connects). A wrong or lost key means the stored
    value can't be read: reconnect, nothing worse.

Sealed values are text (URL-safe base64), for a TEXT column.
"""
import base64
import binascii
import os
from pathlib import Path
from typing import List, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

VERSION = 1
NONCE_BYTES = 12
KEY_BYTES = 32
KEY_ENV = "TRIBUTARY_SECRET_KEY"
HOW_TO_MAKE_ONE = "the base64 of 32 random bytes: openssl rand -base64 32"


class SecretKeyError(Exception):
    """No usable key: none set, or not 32 bytes of base64. The message names
    the variable, never a value."""


class Unreadable(Exception):
    """A sealed value that does not open: another key sealed it (changed,
    lost), it belongs to another context, or it was altered."""


def _decode(raw: str, name: str) -> bytes:
    # Standard base64 (openssl's), or its URL-safe alphabet; padding optional.
    text = raw.strip().replace("-", "+").replace("_", "/").rstrip("=")
    try:
        key = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise SecretKeyError(f"{name} is not base64: it must be {HOW_TO_MAKE_ONE}.") from None
    if len(key) != KEY_BYTES:
        raise SecretKeyError(f"{name} holds {len(key)} bytes, not {KEY_BYTES}: it must be {HOW_TO_MAKE_ONE}.")
    return key


def _secret(value) -> str:
    """A SecretStr or a str setting, '' when unset."""
    if value is None:
        return ""
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)


def _current() -> bytes:
    inline = _secret(settings.secret_key).strip()
    path = (settings.secret_key_file or "").strip()
    if inline and path:
        raise SecretKeyError(f"Both {KEY_ENV} and {KEY_ENV}_FILE are set: keep one of them.")
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SecretKeyError(f"{KEY_ENV}_FILE can't be read ({type(exc).__name__}).") from None
        return _decode(raw, f"{KEY_ENV}_FILE")
    if not inline:
        raise SecretKeyError(f"{KEY_ENV} is not set: it must be {HOW_TO_MAKE_ONE}.")
    return _decode(inline, KEY_ENV)


def _keys() -> List[bytes]:
    """The current key, then the previous one (a rotation), if set."""
    keys = [_current()]
    previous = _secret(settings.secret_key_previous).strip()
    if previous:
        keys.append(_decode(previous, f"{KEY_ENV}_PREVIOUS"))
    return keys


def problem() -> Optional[str]:
    """Why nothing can be sealed now (for the UI), or None when a key is set."""
    try:
        _keys()
    except SecretKeyError as exc:
        return str(exc)
    return None


def _aad(context: str) -> bytes:
    return bytes([VERSION]) + context.encode("utf-8")


def seal(plaintext: bytes, context: str) -> str:
    """`plaintext` encrypted with the current key, bound to `context`."""
    key = _current()
    nonce = os.urandom(NONCE_BYTES)
    sealed = bytes([VERSION]) + nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(context))
    return base64.urlsafe_b64encode(sealed).decode("ascii")


def open_(sealed: str, context: str) -> Tuple[bytes, bool]:
    """(plaintext, whether it was sealed with the previous key: seal it again
    with the current one). Raises SecretKeyError without a key, Unreadable when
    no key opens it for this context."""
    keys = _keys()
    try:
        raw = base64.urlsafe_b64decode(sealed.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise Unreadable("not a sealed value") from None
    if len(raw) < 1 + NONCE_BYTES + 16 or raw[0] != VERSION:
        raise Unreadable("unknown format")
    nonce, body = raw[1:1 + NONCE_BYTES], raw[1 + NONCE_BYTES:]
    for index, key in enumerate(keys):
        try:
            return AESGCM(key).decrypt(nonce, body, _aad(context)), index > 0
        except InvalidTag:
            continue
    raise Unreadable("no key opens it")


def readable(sealed: str, context: str) -> bool:
    """Whether a sealed value opens with the keys set now (for a page: never
    raises, never returns the value)."""
    try:
        open_(sealed, context)
    except (SecretKeyError, Unreadable):
        return False
    return True
