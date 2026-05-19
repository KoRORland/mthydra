"""Audit log — append-only by convention.

Spec A §4.7: writes are mirrored to a plain-text file (one JSON line per event)
so operators can grep without opening SQLite.  Call set_audit_mirror() once at
startup to enable the mirror; by default (tests, CLI subcommands other than
serve) no file is written.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Module-level mirror path; None means mirroring is disabled.
_mirror_path: Path | None = None


def set_audit_mirror(path: Path | str | None) -> None:
    """Configure (or disable) the append-only audit mirror file.

    Call once at daemon startup before any log_event() invocations.
    Thread-safe for single-writer use (the controller daemon is single-process).
    """
    global _mirror_path
    _mirror_path = Path(path) if path is not None else None


@dataclass(frozen=True)
class AuditEvent:
    id: int
    ts: str
    actor: str
    action: str
    target: str | None
    details_json: str | None


def log_event(
    conn: sqlite3.Connection,
    ts: str,
    actor: str,
    action: str,
    target: str | None,
    details_json: str | None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, target, details_json) VALUES (?, ?, ?, ?, ?)",
        (ts, actor, action, target, details_json),
    )
    conn.commit()
    _mirror_event(ts=ts, actor=actor, action=action, target=target, details_json=details_json)


def _mirror_event(
    ts: str,
    actor: str,
    action: str,
    target: str | None,
    details_json: str | None,
) -> None:
    """Append one JSON line to the mirror file if configured.  Best-effort; never raises."""
    if _mirror_path is None:
        return
    try:
        _mirror_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": ts, "actor": actor, "action": action, "target": target,
             "details_json": details_json},
            separators=(",", ":"),
        )
        with open(_mirror_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
    except Exception:
        pass  # never let a mirror failure mask the primary SQLite write


def recent_events(conn: sqlite3.Connection, limit: int = 100) -> list[AuditEvent]:
    rows = conn.execute(
        "SELECT id, ts, actor, action, target, details_json FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [AuditEvent(*r) for r in rows]
