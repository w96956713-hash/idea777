"""Accounts and sessions.

Each user belongs to exactly one organisation; every API call is scoped to it. Passwords are
hashed with scrypt; sessions are random tokens stored only as SHA-256 hashes and sent as an
HttpOnly, SameSite=Strict cookie.
"""

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from ..store import Store, utcnow

COOKIE = "agentgen_session"
SESSION_DAYS = 7
MIN_PASSWORD = 10


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(store: Store, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (utcnow() + timedelta(days=SESSION_DAYS)).isoformat()
    store.create_session(_token_hash(token), user_id, expires)
    return token


def session_user(store: Store, token: str | None):
    if not token:
        return None
    return store.session_user(_token_hash(token))


def end_session(store: Store, token: str | None) -> None:
    if token:
        store.delete_session(_token_hash(token))


class LoginThrottle:
    """At most `limit` failed logins per email per `window` seconds (in memory, per process)."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit, self.window = limit, window
        self.failures: dict[str, deque] = defaultdict(deque)

    def blocked(self, email: str) -> bool:
        q = self.failures[email.lower()]
        while q and q[0] < time.monotonic() - self.window:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, email: str) -> None:
        self.failures[email.lower()].append(time.monotonic())

    def clear(self, email: str) -> None:
        self.failures.pop(email.lower(), None)
