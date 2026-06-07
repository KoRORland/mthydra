"""EU controller debug-flag persistence + expiry. Stdlib-only.

The flag lives at /var/lib/mthydra/debug.flag (persistent). `debug enable`
(run as root via sudo) writes it; `serve` (run as the mthydra user) reads it
and the auto-expire watcher removes it. The mthydra user can unlink a
root-written flag because it owns the containing directory.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FLAG_PATH = "/var/lib/mthydra/debug.flag"
DEFAULT_TTL_HOURS = 24.0


@dataclass(frozen=True)
class DebugFlag:
    enabled_at: float
    expires_at: float
    ttl_hours: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


def write_flag(path: Path | str, *, ttl_hours: float = DEFAULT_TTL_HOURS,
               now: float | None = None) -> DebugFlag:
    now = time.time() if now is None else now
    flag = DebugFlag(enabled_at=now, expires_at=now + ttl_hours * 3600,
                     ttl_hours=ttl_hours)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "enabled_at": flag.enabled_at,
        "expires_at": flag.expires_at,
        "ttl_hours": flag.ttl_hours,
    }))
    return flag


def read_flag(path: Path | str) -> DebugFlag | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    try:
        return DebugFlag(
            enabled_at=float(data["enabled_at"]),
            expires_at=float(data["expires_at"]),
            ttl_hours=float(data.get("ttl_hours", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_flag(path: Path | str) -> None:
    Path(path).unlink(missing_ok=True)
