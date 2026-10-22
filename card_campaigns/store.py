"""登録状態・取得キャッシュ・手動登録キャンペーンの保存（SQLite）

保存先の優先順位:
  1. 環境変数 CARDS_DB_PATH
  2. /data/cards.db（Railway の Volume を /data にマウントした場合）
  3. ~/.config/jal-shopping-tool/cards.db（ローカル開発用。Railway では再デプロイで消える）
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


def db_path() -> Path:
    env = os.environ.get("CARDS_DB_PATH")
    if env:
        return Path(env)
    data_dir = Path("/data")
    if data_dir.is_dir() and os.access(data_dir, os.W_OK):
        return data_dir / "cards.db"
    return Path.home() / ".config" / "jal-shopping-tool" / "cards.db"


def is_persistent() -> bool:
    p = db_path()
    return bool(os.environ.get("CARDS_DB_PATH")) or str(p).startswith("/data/")


def _connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS status (
            campaign_key TEXT PRIMARY KEY,
            state TEXT NOT NULL,          -- entered | dismissed
            title TEXT,
            source_id TEXT,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fetch_cache (
            source_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manual_campaign (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            deadline TEXT,                -- YYYY-MM-DD
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_check (
            source_id TEXT PRIMARY KEY,
            checked_at REAL NOT NULL
        );
        """
    )
    return conn


def get_statuses() -> dict[str, dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM status").fetchall()
    return {r["campaign_key"]: dict(r) for r in rows}


def set_status(key: str, state: str | None, title: str = "", source_id: str = "") -> None:
    with _LOCK, _connect() as conn:
        if state is None:
            conn.execute("DELETE FROM status WHERE campaign_key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO status(campaign_key, state, title, source_id, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(campaign_key) DO UPDATE SET "
                "state=excluded.state, title=excluded.title, "
                "source_id=excluded.source_id, updated_at=excluded.updated_at",
                (key, state, title, source_id, time.time()),
            )


def load_cache() -> dict[str, dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM fetch_cache").fetchall()
    out = {}
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except ValueError:
            continue
        payload["fetched_at"] = r["fetched_at"]
        out[r["source_id"]] = payload
    return out


def save_cache(source_id: str, payload: dict, fetched_at: float) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO fetch_cache(source_id, payload, fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at",
            (source_id, json.dumps(payload, ensure_ascii=False), fetched_at),
        )


def list_manual() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM manual_campaign ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def add_manual(source_id: str, title: str, url: str, deadline: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO manual_campaign(source_id, title, url, deadline, created_at) "
            "VALUES(?,?,?,?,?)",
            (source_id, title, url or None, deadline or None, time.time()),
        )


def delete_manual(manual_id: int) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM manual_campaign WHERE id = ?", (manual_id,))
        conn.execute("DELETE FROM status WHERE campaign_key = ?", (f"manual:{manual_id}",))


def get_source_checks() -> dict[str, float]:
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM source_check").fetchall()
    return {r["source_id"]: r["checked_at"] for r in rows}


def mark_source_checked(source_id: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO source_check(source_id, checked_at) VALUES(?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET checked_at=excluded.checked_at",
            (source_id, time.time()),
        )
