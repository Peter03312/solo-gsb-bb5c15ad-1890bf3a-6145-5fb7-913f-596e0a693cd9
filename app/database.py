"""SQLite 持久化：连接管理与启动建表。"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request

from . import config

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS showcases (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    location TEXT
);

CREATE TABLE IF NOT EXISTS exhibits (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    showcase_id            INTEGER NOT NULL REFERENCES showcases(id) ON DELETE CASCADE,
    name                   TEXT NOT NULL,
    sensitivity            TEXT NOT NULL CHECK (sensitivity IN ('high', 'medium', 'low')),
    annual_limit_lux_hours REAL,
    UNIQUE (showcase_id, name)
);

CREATE TABLE IF NOT EXISTS light_intervals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    showcase_id INTEGER NOT NULL REFERENCES showcases(id) ON DELETE CASCADE,
    start_ts    TEXT NOT NULL,
    end_ts      TEXT NOT NULL,
    lux         REAL NOT NULL CHECK (lux >= 0)
);

CREATE INDEX IF NOT EXISTS idx_intervals_case_time
    ON light_intervals (showcase_id, start_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_exhibits_case ON exhibits (showcase_id);
"""


def _dict_factory(cursor: sqlite3.Cursor, row: tuple):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        config.DB_PATH, timeout=30.0, check_same_thread=False
    )
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    """启动时自动初始化数据库（幂等）。"""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """请求级事务：正常结束提交，抛异常回滚。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db(request: Request) -> sqlite3.Connection:
    """FastAPI 依赖：返回 app.state 中由中间件管理的连接。"""
    return request.state.db
