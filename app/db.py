"""Tiny SQLite layer. One connection, guarded by a lock; plenty for a few users."""

import sqlite3
import threading
import time

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    cpd_email TEXT,
    cpd_password_enc TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    play_date TEXT NOT NULL,
    starts TEXT NOT NULL,          -- JSON list of start hours, in preference order
    hours INTEGER NOT NULL,
    courts TEXT NOT NULL,          -- JSON list of court numbers, in preference order
    event_name TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    detail TEXT,
    court INTEGER,
    start_hour INTEGER,
    total TEXT,
    hold_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS bookings_status ON bookings(status);
CREATE TABLE IF NOT EXISTS push_subs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL
);
"""


def init(path) -> None:
    global _conn
    _conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql: str, args: tuple = ()) -> int:
    with _lock:
        return _conn.execute(sql, args).lastrowid


def set_booking(booking_id: int, **fields) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    x(f"UPDATE bookings SET {cols} WHERE id = ?", (*fields.values(), booking_id))
