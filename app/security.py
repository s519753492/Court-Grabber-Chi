"""Password hashing, session tokens and encryption of Park District passwords."""

import hashlib
import secrets
import time

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from . import config, db

SESSION_DAYS = 60
_fernet: Fernet | None = None


def init() -> None:
    global _fernet
    if not config.ENCRYPTION_KEY:
        raise SystemExit("ENCRYPTION_KEY is not set. See README.md, step 'Create your secrets'.")
    if not config.INVITE_CODE:
        raise SystemExit("INVITE_CODE is not set. See README.md, step 'Create your secrets'.")
    try:
        _fernet = Fernet(config.ENCRYPTION_KEY.encode())
    except Exception:
        raise SystemExit("ENCRYPTION_KEY is not a valid key. Generate one as shown in README.md.")


# -- app passwords (hashed, never reversible)

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_password(pw: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), pw_hash.encode())
    except ValueError:
        return False


# -- Park District passwords (encrypted, because the bot must type them in)

def encrypt(secret: str) -> str:
    return _fernet.encrypt(secret.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        raise RuntimeError("Stored Park District password can't be decrypted "
                           "(was ENCRYPTION_KEY changed?). Please re-enter it.")


# -- sessions

def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.x("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
         (_h(token), user_id, time.time() + SESSION_DAYS * 86400))
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    row = db.one("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                 "WHERE s.token_hash = ? AND s.expires_at > ?", (_h(token), time.time()))
    return row


def end_session(token: str | None) -> None:
    if token:
        db.x("DELETE FROM sessions WHERE token_hash = ?", (_h(token),))


# -- simple brute-force guard for the login form

_fails: dict[str, list[float]] = {}


def too_many_attempts(key: str) -> bool:
    now = time.time()
    recent = [t for t in _fails.get(key, []) if now - t < 900]
    _fails[key] = recent
    return len(recent) >= 10


def record_failure(key: str) -> None:
    _fails.setdefault(key, []).append(time.time())
