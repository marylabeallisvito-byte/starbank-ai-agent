"""Server-owned investigation snapshots for one-time human review."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


STORE_PATH = Path(__file__).resolve().parent / "data" / "investigations.sqlite3"
REVIEW_WINDOW = timedelta(hours=24)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS investigations (
        id TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        reviewed_at TEXT
    )""")
    return conn


def create_investigation(payload: dict[str, Any], path: Path = STORE_PATH) -> str:
    """Persist the decision and its evidence IDs before exposing a review handle."""
    investigation_id = uuid.uuid4().hex
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO investigations (id, payload, created_at) VALUES (?, ?, ?)",
            (investigation_id, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
    return investigation_id


@contextmanager
def pending_investigation(investigation_id: str, path: Path = STORE_PATH) -> Iterator[dict[str, Any] | None]:
    """Lock a pending result while feedback is recorded; commit only on success."""
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload, created_at FROM investigations WHERE id = ? AND reviewed_at IS NULL",
            (investigation_id,),
        ).fetchone()
        expired = row is not None and datetime.now(timezone.utc) - datetime.fromisoformat(row[1]) > REVIEW_WINDOW
        if row is None or expired:
            yield None
        else:
            yield json.loads(row[0])
            conn.execute(
                "UPDATE investigations SET reviewed_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), investigation_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
