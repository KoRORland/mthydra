"""SQLite connection management for the controller state."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with the project's standard PRAGMAs.

    Creates parent directories for read-write opens. Read-only opens require
    the file to already exist.
    """
    db_path = Path(db_path)
    if read_only:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
