# User Onboarding + Deep-Link Enrollment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the five-command user setup with a single `user-onboard` command and a tap-link/tap-Start enrollment flow for non-technical users, while binding boxes to a default shard at provisioning so the live-box-without-shard invariant (check 33) can't be violated.

**Architecture:** A schema migration (v15→v16) adds a `pending_enrollments` table and a `bot_offsets` table, and seeds a `default_shard`. A new enrollment token service mints one-time hashed tokens. The distribution Telegram sink gains receive methods (`get_me`, `get_updates`); a new active-only `EnrollmentPoller` long-polls for `/start <token>`, captures the user's `chat_id`, and triggers first delivery. `provision_box` binds boxes to `default_shard` (or `--shard`); `mark_live` guards against NULL shard. `user-onboard` wraps user creation + shard assignment + token mint + deep-link printing.

**Tech Stack:** Python 3.12, SQLite (stdlib `sqlite3`), APScheduler `BackgroundScheduler`, stdlib `secrets`/`hashlib`/`urllib`, pytest with injected fakes (no live network).

**Spec:** `docs/superpowers/specs/2026-06-03-user-onboarding-design.md`

---

## File structure

| File | Responsibility |
|---|---|
| `src/mthydra/controller/state/schema.py` | v16 migration: `pending_enrollments` + `bot_offsets` tables, seed `default_shard` |
| `src/mthydra/controller/distribution/enrollment.py` | NEW — token mint/match/deep-link (pure, DB-only) |
| `src/mthydra/controller/distribution/sinks.py` | extend `TelegramDistributionSink` with `get_me` + `get_updates` |
| `src/mthydra/controller/distribution/enroll_poller.py` | NEW — active-only scheduler: poll updates → capture chat_id → first delivery |
| `src/mthydra/controller/state/ru_boxes.py` | `mark_live` NULL-shard guard |
| `src/mthydra/controller/provisioning/seed.py` | `provision_box(shard_id=...)` binds box to a shard |
| `src/mthydra/controller/config.py` | `default_shard_id`, `enrollment_token_ttl_hours`, `enroll_poll_interval_seconds` |
| `src/mthydra/controller/cli.py` | `user-onboard` command; `--shard` on `provision-seed`; wire `EnrollmentPoller` |
| `doc/quickstart-mvp.md` | Part 8 rewrite |

---

## Task 1: Schema v16 — pending_enrollments + bot_offsets + seed default_shard

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Test: `tests/unit/controller/state/test_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/state/test_schema.py  (add)
def test_v16_tables_and_default_shard_seeded(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema, SCHEMA_VERSION
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    assert SCHEMA_VERSION >= 16
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "pending_enrollments" in tables
    assert "bot_offsets" in tables
    row = c.execute(
        "SELECT shard_id, members_json FROM shards WHERE shard_id='default_shard'"
    ).fetchone()
    assert row is not None and row[1] == "[]"
    c.close()


def test_v16_seed_default_shard_idempotent_on_upgrade(tmp_path):
    # Simulate an existing pre-v16 DB: build schema, delete default_shard,
    # drop version to 15, re-apply -> migration recreates the shard once.
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    c.execute("DELETE FROM shards WHERE shard_id='default_shard'")
    c.execute("UPDATE schema_version SET version=15 WHERE rowid=1")
    c.commit()
    apply_schema(c)  # runs migrate_v15_to_v16
    n = c.execute(
        "SELECT COUNT(*) FROM shards WHERE shard_id='default_shard'").fetchone()[0]
    assert n == 1
    c.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/controller/state/test_schema.py::test_v16_tables_and_default_shard_seeded -v`
Expected: FAIL (table missing / SCHEMA_VERSION < 16).

- [ ] **Step 3: Implement**

In `schema.py`, bump the version constant:
```python
SCHEMA_VERSION = 16
```

Add these CREATE statements to the `_STATEMENTS` list (near the existing
`user_channels` / `distribution_log` block around line 479) so fresh installs
get them:
```python
    """
    CREATE TABLE IF NOT EXISTS pending_enrollments (
      user_id      TEXT PRIMARY KEY REFERENCES users(user_id),
      token_hash   TEXT NOT NULL,
      created_at   TEXT NOT NULL,
      expires_at   TEXT NOT NULL,
      consumed_at  TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_pending_enrollments_expires
      ON pending_enrollments(expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_offsets (
      bot_purpose  TEXT PRIMARY KEY,
      last_offset  INTEGER NOT NULL,
      updated_at   TEXT NOT NULL
    )
    """,
```

Add a seed helper (module level, near `apply_schema`):
```python
def _seed_default_shard(conn: sqlite3.Connection) -> None:
    """Seed the always-present 'default_shard' (empty membership). Idempotent."""
    conn.execute(
        "INSERT OR IGNORE INTO shards "
        "(shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('default_shard', '[]', 2, ?, ?)",
        (_now(), _now()),
    )
```

Add the migration function (next to `migrate_v14_to_v15`):
```python
def migrate_v15_to_v16(conn: sqlite3.Connection) -> None:
    """Idempotent v15 → v16: pending_enrollments + bot_offsets + default_shard."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pending_enrollments ("
        "user_id TEXT PRIMARY KEY REFERENCES users(user_id), "
        "token_hash TEXT NOT NULL, created_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, consumed_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_pending_enrollments_expires "
        "ON pending_enrollments(expires_at)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bot_offsets ("
        "bot_purpose TEXT PRIMARY KEY, last_offset INTEGER NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    _seed_default_shard(conn)
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (16, _now()),
    )
    conn.commit()
```

Wire it into `apply_schema`: in the fresh-install branch (after the `node_state`
seed, inside `if existing == 0:`) add `_seed_default_shard(conn)`; in the
migration chain add after the `current < 15` block:
```python
        if current < 16:
            migrate_v15_to_v16(conn)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/controller/state/test_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/schema.py tests/unit/controller/state/test_schema.py
git commit -m "feat(schema): v16 — pending_enrollments + bot_offsets + seed default_shard"
```

---

## Task 2: Enrollment token service

**Files:**
- Create: `src/mthydra/controller/distribution/enrollment.py`
- Test: `tests/unit/controller/distribution/test_enrollment.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/distribution/test_enrollment.py
from __future__ import annotations

import pytest

from mthydra.controller.distribution import enrollment
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.users_shards import add_user


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    add_user(c, "granny", "Granny", "phone:+0", "2026-06-03T00:00:00Z")
    yield c
    c.close()


def test_mint_then_match_happy_path(conn):
    tok = enrollment.mint(conn, "granny", ttl_seconds=3600,
                          now="2026-06-03T10:00:00Z")
    assert tok and isinstance(tok, str)
    assert enrollment.match(conn, tok, now="2026-06-03T10:30:00Z") == "granny"


def test_match_single_use(conn):
    tok = enrollment.mint(conn, "granny", ttl_seconds=3600,
                          now="2026-06-03T10:00:00Z")
    assert enrollment.match(conn, tok, now="2026-06-03T10:30:00Z") == "granny"
    # Second use is rejected (consumed).
    assert enrollment.match(conn, tok, now="2026-06-03T10:31:00Z") is None


def test_match_expired_rejected(conn):
    tok = enrollment.mint(conn, "granny", ttl_seconds=3600,
                          now="2026-06-03T10:00:00Z")
    assert enrollment.match(conn, tok, now="2026-06-03T11:00:01Z") is None


def test_match_unknown_token(conn):
    enrollment.mint(conn, "granny", ttl_seconds=3600, now="2026-06-03T10:00:00Z")
    assert enrollment.match(conn, "bogus", now="2026-06-03T10:30:00Z") is None


def test_reissue_replaces_prior(conn):
    t1 = enrollment.mint(conn, "granny", ttl_seconds=3600,
                         now="2026-06-03T10:00:00Z")
    t2 = enrollment.mint(conn, "granny", ttl_seconds=3600,
                         now="2026-06-03T10:05:00Z")
    assert t1 != t2
    assert enrollment.match(conn, t1, now="2026-06-03T10:06:00Z") is None
    assert enrollment.match(conn, t2, now="2026-06-03T10:06:00Z") == "granny"


def test_deep_link_format():
    assert enrollment.deep_link("myfam_bot", "ABC") == \
        "https://t.me/myfam_bot?start=ABC"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/controller/distribution/test_enrollment.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# src/mthydra/controller/distribution/enrollment.py
"""One-time enrollment tokens for deep-link user onboarding (spec O O-D3).

Operator-issued, single-use, expiring, stored hashed. A token authenticates an
incoming Telegram /start so the controller can bind a chat_id to a user without
open self-service (preserves spec K K-D4).
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3

from mthydra.controller.state import audit


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _add_seconds_iso(iso: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).astimezone(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mint(conn: sqlite3.Connection, user_id: str, *, ttl_seconds: int,
         now: str) -> str:
    """Mint (or reissue) a token for user_id. Returns the plaintext token once."""
    token = secrets.token_urlsafe(9)  # ~72 bits entropy
    expires_at = _add_seconds_iso(now, ttl_seconds)
    conn.execute(
        "INSERT INTO pending_enrollments "
        "(user_id, token_hash, created_at, expires_at, consumed_at) "
        "VALUES (?, ?, ?, ?, NULL) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "token_hash=excluded.token_hash, created_at=excluded.created_at, "
        "expires_at=excluded.expires_at, consumed_at=NULL",
        (user_id, _hash(token), now, expires_at),
    )
    audit.log_event(conn, ts=now, actor="operator", action="enrollment_mint",
                    target=user_id, details_json=None)
    return token


def match(conn: sqlite3.Connection, token: str, *, now: str) -> str | None:
    """Return the user_id for a valid, unexpired, unconsumed token; else None.

    On a hit, marks the token consumed (single-use). ISO timestamps in the
    'YYYY-MM-DDTHH:MM:SSZ' form compare correctly lexicographically.
    """
    row = conn.execute(
        "SELECT user_id FROM pending_enrollments "
        "WHERE token_hash=? AND consumed_at IS NULL AND expires_at > ?",
        (_hash(token), now),
    ).fetchone()
    if row is None:
        return None
    user_id = row[0]
    conn.execute(
        "UPDATE pending_enrollments SET consumed_at=? WHERE user_id=?",
        (now, user_id),
    )
    audit.log_event(conn, ts=now, actor="enroll_poller",
                    action="enrollment_consumed", target=user_id,
                    details_json=None)
    conn.commit()
    return user_id


def deep_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"
```

Create `tests/unit/controller/distribution/__init__.py` if it does not exist
(empty file).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/controller/distribution/test_enrollment.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/distribution/enrollment.py tests/unit/controller/distribution/test_enrollment.py
git commit -m "feat(distribution): one-time hashed enrollment token service"
```

---

## Task 3: Distribution bot receive methods (get_me + get_updates)

**Files:**
- Modify: `src/mthydra/controller/distribution/sinks.py` (extend `TelegramDistributionSink`)
- Test: `tests/unit/controller/distribution/test_sinks_receive.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/distribution/test_sinks_receive.py
from __future__ import annotations

import json

from mthydra.controller.distribution.sinks import TelegramDistributionSink


def test_get_me_returns_username():
    def fake_get(url, params):
        assert "getMe" in url
        return 200, json.dumps({"ok": True, "result": {"username": "myfam_bot"}})
    s = TelegramDistributionSink(bot_token="t", http_get=fake_get)
    assert s.get_me() == "myfam_bot"


def test_get_updates_passes_offset_and_parses():
    seen = {}
    def fake_get(url, params):
        seen.update(params)
        return 200, json.dumps({"ok": True, "result": [
            {"update_id": 41, "message": {"chat": {"id": 99}, "text": "/start AB"}},
        ]})
    s = TelegramDistributionSink(bot_token="t", http_get=fake_get)
    updates = s.get_updates(offset=41)
    assert seen["offset"] == 41
    assert updates == [{"update_id": 41, "chat_id": "99", "text": "/start AB"}]


def test_get_updates_skips_non_message_updates():
    def fake_get(url, params):
        return 200, json.dumps({"ok": True, "result": [
            {"update_id": 7, "edited_message": {"chat": {"id": 1}, "text": "x"}},
        ]})
    s = TelegramDistributionSink(bot_token="t", http_get=fake_get)
    assert s.get_updates(offset=0) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/controller/distribution/test_sinks_receive.py -v`
Expected: FAIL (`TypeError: unexpected keyword 'http_get'`).

- [ ] **Step 3: Implement**

In `sinks.py`, modify `TelegramDistributionSink.__init__` to accept an
optional `http_get`, and add the two methods:

```python
    def __init__(
        self,
        bot_token: str,
        http_post: Callable[[str, dict], tuple[int, str]] | None = None,
        http_get: Callable[[str, dict], tuple[int, str]] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._http_post = http_post or self._default_http_post
        self._http_get = http_get or self._default_http_get
```

```python
    @staticmethod
    def _default_http_get(url: str, params: dict) -> tuple[int, str]:
        import urllib.error
        import urllib.parse
        import urllib.request

        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            with urllib.request.urlopen(full, timeout=35) as resp:
                return int(resp.status), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return int(e.code), e.read().decode("utf-8", errors="replace")
        except Exception as e:
            return 0, str(e)

    def get_me(self) -> str | None:
        url = f"https://api.telegram.org/bot{self._bot_token}/getMe"
        status, text = self._http_get(url, {})
        if not (200 <= status < 300):
            return None
        data = json.loads(text)
        if not data.get("ok"):
            return None
        return data["result"].get("username")

    def get_updates(self, *, offset: int) -> list[dict]:
        """Return normalised message updates: {update_id, chat_id, text}.

        Only plain `message` updates are returned (edited/callbacks ignored).
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
        status, text = self._http_get(url, {"offset": offset, "timeout": 0})
        if not (200 <= status < 300):
            return []
        data = json.loads(text)
        if not data.get("ok"):
            return []
        out: list[dict] = []
        for u in data.get("result", []):
            msg = u.get("message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            out.append({
                "update_id": u["update_id"],
                "chat_id": str(chat.get("id")),
                "text": msg.get("text", ""),
            })
        return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/controller/distribution/test_sinks_receive.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/distribution/sinks.py tests/unit/controller/distribution/test_sinks_receive.py
git commit -m "feat(distribution): bot receive methods get_me + get_updates"
```

---

## Task 4: Enrollment poller

**Files:**
- Create: `src/mthydra/controller/distribution/enroll_poller.py`
- Test: `tests/unit/controller/distribution/test_enroll_poller.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/distribution/test_enroll_poller.py
from __future__ import annotations

import pytest

from mthydra.controller.distribution import enrollment
from mthydra.controller.distribution.enroll_poller import EnrollmentPoller
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.user_channels import get_channels
from mthydra.controller.state.users_shards import add_user


class FakeReceive:
    def __init__(self, batches):
        self._batches = list(batches)
    def get_updates(self, *, offset):
        return self._batches.pop(0) if self._batches else []


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "s.sqlite"
    c = connect(p)
    apply_schema(c)
    add_user(c, "granny", "Granny", "phone:+0", "2026-06-03T00:00:00Z")
    c.commit()
    c.close()
    return p


def _poller(db, recv, enrolled):
    return EnrollmentPoller(
        db_path=db, receive_client=recv,
        poll_interval_seconds=30, mode="offline",
        on_enrolled=lambda uid: enrolled.append(uid),
        clock=lambda: "2026-06-03T10:30:00Z",
    )


def test_valid_start_captures_chat_id_and_triggers_delivery(db):
    c = connect(db)
    tok = enrollment.mint(c, "granny", ttl_seconds=3600, now="2026-06-03T10:00:00Z")
    c.commit(); c.close()
    enrolled = []
    recv = FakeReceive([[{"update_id": 5, "chat_id": "12345",
                          "text": f"/start {tok}"}]])
    p = _poller(db, recv, enrolled)
    p.run_once()
    c = connect(db)
    ch = get_channels(c, "granny")
    assert ch is not None and ch.telegram_chat_id == "12345"
    assert c.execute("SELECT last_offset FROM bot_offsets WHERE bot_purpose='distribution'"
                     ).fetchone()[0] == 6
    c.close()
    assert enrolled == ["granny"]


def test_unknown_token_no_capture(db):
    enrolled = []
    recv = FakeReceive([[{"update_id": 9, "chat_id": "1", "text": "/start nope"}]])
    p = _poller(db, recv, enrolled)
    p.run_once()
    c = connect(db)
    assert get_channels(c, "granny") is None
    assert c.execute("SELECT last_offset FROM bot_offsets WHERE bot_purpose='distribution'"
                     ).fetchone()[0] == 10  # offset still advances past the update
    c.close()
    assert enrolled == []


def test_consumed_token_replay_ignored(db):
    c = connect(db)
    tok = enrollment.mint(c, "granny", ttl_seconds=3600, now="2026-06-03T10:00:00Z")
    c.commit(); c.close()
    enrolled = []
    recv = FakeReceive([
        [{"update_id": 1, "chat_id": "12345", "text": f"/start {tok}"}],
        [{"update_id": 2, "chat_id": "99999", "text": f"/start {tok}"}],
    ])
    p = _poller(db, recv, enrolled)
    p.run_once()
    p.run_once()
    c = connect(db)
    assert get_channels(c, "granny").telegram_chat_id == "12345"  # not overwritten
    c.close()
    assert enrolled == ["granny"]


def test_offline_mode_does_not_arm(db):
    p = _poller(db, FakeReceive([]), [])
    p.arm()
    assert p._scheduler is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/controller/distribution/test_enroll_poller.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# src/mthydra/controller/distribution/enroll_poller.py
"""Enrollment poller (spec O O-D2): long-poll the distribution bot for
/start <token> and capture the user's chat_id. Active-only scheduler.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.distribution import enrollment
from mthydra.controller.state import user_channels as _uc
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect

_BOT_PURPOSE = "distribution"


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EnrollmentPoller:
    def __init__(
        self,
        *,
        db_path: Path | str,
        receive_client,
        poll_interval_seconds: float,
        on_enrolled: Callable[[str], None] | None = None,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.recv = receive_client
        self.poll_interval_seconds = poll_interval_seconds
        self.on_enrolled = on_enrolled
        self.mode = mode
        self._clock = clock or _default_clock
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        if self.mode == "offline":
            return
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self.run_once,
            trigger=IntervalTrigger(seconds=self.poll_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def _offset(self, conn) -> int:
        row = conn.execute(
            "SELECT last_offset FROM bot_offsets WHERE bot_purpose=?",
            (_BOT_PURPOSE,),
        ).fetchone()
        return int(row[0]) if row else 0

    def _save_offset(self, conn, offset: int, now: str) -> None:
        conn.execute(
            "INSERT INTO bot_offsets (bot_purpose, last_offset, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(bot_purpose) DO UPDATE SET "
            "last_offset=excluded.last_offset, updated_at=excluded.updated_at",
            (_BOT_PURPOSE, offset, now),
        )

    def run_once(self) -> list[str]:
        """Process one batch of updates. Returns user_ids newly enrolled."""
        now = self._clock()
        conn = connect(self.db_path)
        enrolled: list[str] = []
        try:
            offset = self._offset(conn)
            updates = self.recv.get_updates(offset=offset)
            max_update_id = offset - 1
            for u in updates:
                max_update_id = max(max_update_id, int(u["update_id"]))
                text = u.get("text", "") or ""
                if not text.startswith("/start "):
                    continue
                token = text.split(maxsplit=1)[1].strip()
                user_id = enrollment.match(conn, token, now=now)
                if user_id is None:
                    log_event(conn, ts=now, actor="enroll_poller",
                              action="enrollment_rejected", target=None,
                              details_json=None)
                    continue
                existing = _uc.get_channels(conn, user_id)
                email = existing.email_addr if existing else None
                _uc.set_channels(conn, user_id,
                                 telegram_chat_id=u["chat_id"],
                                 email_addr=email, at=now)
                enrolled.append(user_id)
            if updates:
                self._save_offset(conn, max_update_id + 1, now)
            conn.commit()
        finally:
            conn.close()
        # Trigger first delivery outside the capture transaction.
        if self.on_enrolled:
            for uid in enrolled:
                self.on_enrolled(uid)
        return enrolled
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/controller/distribution/test_enroll_poller.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/distribution/enroll_poller.py tests/unit/controller/distribution/test_enroll_poller.py
git commit -m "feat(distribution): EnrollmentPoller captures chat_id from /start token"
```

---

## Task 5: mark_live NULL-shard guard

**Files:**
- Modify: `src/mthydra/controller/state/ru_boxes.py` (`mark_live`)
- Test: `tests/unit/controller/state/test_ru_boxes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/state/test_ru_boxes.py  (add)
def test_mark_live_refuses_box_with_null_shard(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.ru_boxes import mark_live
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, "
        "image_version, created_at) "
        "VALUES ('b1', 'tw', 'ru', 'x.example', 'provisioning', 'v1', "
        "'2026-06-03T00:00:00Z')"
    )
    c.commit()
    with pytest.raises(ValueError, match="no shard"):
        mark_live(c, "b1", public_ip="1.2.3.4", at="2026-06-03T01:00:00Z")
    c.close()


def test_mark_live_succeeds_with_shard(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.ru_boxes import mark_live
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, "
        "image_version, created_at, shard_id) "
        "VALUES ('b1', 'tw', 'ru', 'x.example', 'provisioning', 'v1', "
        "'2026-06-03T00:00:00Z', 'default_shard')"
    )
    c.commit()
    mark_live(c, "b1", public_ip="1.2.3.4", at="2026-06-03T01:00:00Z")
    assert c.execute("SELECT state FROM ru_boxes WHERE box_id='b1'").fetchone()[0] == "live"
    c.close()
```

Ensure `import pytest` is present at the top of the test file.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/controller/state/test_ru_boxes.py::test_mark_live_refuses_box_with_null_shard -v`
Expected: FAIL (no exception raised).

- [ ] **Step 3: Implement**

Modify `mark_live` in `ru_boxes.py`:
```python
def mark_live(conn: sqlite3.Connection, box_id: str, *, public_ip: str, at: str) -> None:
    row = conn.execute(
        "SELECT state, shard_id FROM ru_boxes WHERE box_id=?", (box_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"box {box_id!r} is not in provisioning state")
    if row[1] is None:
        raise ValueError(
            f"box {box_id!r} has no shard — assign one before going live "
            f"(boxes bind to a shard at provisioning; see provision-seed --shard)"
        )
    cur = conn.execute(
        "UPDATE ru_boxes SET state='live', public_ip=?, went_live_at=? "
        "WHERE box_id=? AND state='provisioning'",
        (public_ip, at, box_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"box {box_id!r} is not in provisioning state")
    conn.commit()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/controller/state/test_ru_boxes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/ru_boxes.py tests/unit/controller/state/test_ru_boxes.py
git commit -m "feat(ru-boxes): mark_live refuses a box with no shard (check 33 defense)"
```

---

## Task 6: provision_box binds box to a shard

**Files:**
- Modify: `src/mthydra/controller/provisioning/seed.py` (`provision_box`)
- Test: `tests/unit/controller/provisioning/test_seed.py` (add two tests, reusing
  its existing `conn` fixture + `_seed_authority`/`_seed_descriptor`/`_seed_image`/
  `_seed_cover` helpers, `_b2_mock()`, `_V2_KWARGS`, `NOW`)

- [ ] **Step 1: Write the failing test**

Add to `test_seed.py` (the `conn` fixture runs `apply_schema`, which seeds
`default_shard` per Task 1):

```python
def test_provision_binds_default_shard(conn):
    _seed_authority(conn); _seed_descriptor(conn); _seed_image(conn)
    _seed_cover(conn, "ds.cover")
    provision_box(conn=conn, b2_destination=_b2_mock(),
                  provider="hetzner", region="fsn1",
                  image_signed_url_ttl_seconds=3600, now=NOW, **_V2_KWARGS)
    sid = conn.execute("SELECT shard_id FROM ru_boxes").fetchone()[0]
    assert sid == "default_shard"


def test_provision_honors_explicit_shard(conn):
    _seed_authority(conn); _seed_descriptor(conn); _seed_image(conn)
    _seed_cover(conn, "es.cover")
    conn.execute("INSERT OR IGNORE INTO shards (shard_id, members_json, "
                 "target_size, last_reshuffled_at, created_at) "
                 "VALUES ('s-hi','[]',2,'t','t')")
    provision_box(conn=conn, b2_destination=_b2_mock(),
                  provider="hetzner", region="fsn1",
                  image_signed_url_ttl_seconds=3600, now=NOW,
                  shard_id="s-hi", **_V2_KWARGS)
    sid = conn.execute("SELECT shard_id FROM ru_boxes").fetchone()[0]
    assert sid == "s-hi"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/controller/provisioning/test_seed.py::test_provision_binds_default_shard -v`
Expected: FAIL (`shard_id` is NULL / unexpected keyword `shard_id`).

- [ ] **Step 3: Implement**

Add `shard_id` param to `provision_box` (default None → `default_shard`):
```python
def provision_box(
    *,
    conn: sqlite3.Connection,
    b2_destination,
    provider: str,
    region: str,
    image_signed_url_ttl_seconds: int,
    now: str,
    descriptor_refresh_url: str,
    agent_source_url: str,
    agent_source_sha256: str,
    telegram_dcs_v4: tuple[str, ...],
    telegram_dcs_v6: tuple[str, ...],
    actor: str = "operator",
    is_canary: bool = False,
    shard_id: str | None = None,
) -> SeedBundle:
    shard_id = shard_id or "default_shard"
```

Add a guard right after resolving `shard_id` (so provisioning fails loudly if
the shard is missing rather than creating an orphan box):
```python
    if conn.execute(
        "SELECT 1 FROM shards WHERE shard_id=? AND retired_at IS NULL",
        (shard_id,),
    ).fetchone() is None:
        raise ProvisionError(
            f"shard {shard_id!r} does not exist or is retired; create it first"
        )
```

Update the `ru_boxes` INSERT (around line 256) to set `shard_id`:
```python
        conn.execute(
            "INSERT INTO ru_boxes "
            "(box_id, provider, region, public_ip, sni, state, image_version, "
            "created_at, is_canary, shard_id) "
            "VALUES (?, ?, ?, ?, ?, 'provisioning', ?, ?, ?, ?)",
            (box_id, provider, region, None, picked.domain, image.image_version,
             now, 1 if is_canary else 0, shard_id),
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/controller/provisioning/test_seed.py -v`
Expected: PASS (existing happy-path test still green — `default_shard` exists).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/provisioning/seed.py tests/unit/controller/provisioning/test_seed.py
git commit -m "feat(provisioning): bind box to default_shard (or --shard) at provision time"
```

---

## Task 7: Config additions

**Files:**
- Modify: `src/mthydra/controller/config.py`
- Test: `tests/unit/controller/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_config.py  (add)
def test_default_shard_id_default_and_override(tmp_path):
    from mthydra.controller.config import load_config
    base = tmp_path / "c.toml"
    base.write_text(_minimal_toml())  # existing helper in this test file
    cfg = load_config(str(base))
    assert cfg.shard_manager.default_shard_id == "default_shard"
    assert cfg.distribution.enrollment_token_ttl_hours == 24
    assert cfg.distribution.enroll_poll_interval_seconds > 0
```

> The defaults apply even when `_minimal_toml()` omits `[shard_manager]`/
> `[distribution]` keys, because the parse uses `sec.get(..., default)`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/controller/test_config.py::test_default_shard_id_default_and_override -v`
Expected: FAIL (`AttributeError: default_shard_id`).

- [ ] **Step 3: Implement**

Add field to `ShardManagerConfig`:
```python
@dataclass(frozen=True)
class ShardManagerConfig:
    target_size: int
    max_size: int
    reshuffle_interval_days: int
    reshuffle_sweep_interval_seconds: int
    default_shard_id: str
```

In the shard-manager parse block (around line 318) add to the constructor:
```python
        default_shard_id=str(sec.get("default_shard_id", "default_shard")),
```

Add fields to `DistributionConfig`:
```python
@dataclass(frozen=True)
class DistributionConfig:
    publish_sweep_interval_seconds: int
    user_heartbeat_interval_seconds: int
    heartbeat_breach_threshold: int
    enrollment_token_ttl_hours: int
    enroll_poll_interval_seconds: int
    telegram: DistributionTelegramConfig | None
    email: DistributionEmailConfig | None
```

In `_load_distribution`'s `return DistributionConfig(...)` add:
```python
        enrollment_token_ttl_hours=_require_positive(
            "distribution.enrollment_token_ttl_hours",
            sec.get("enrollment_token_ttl_hours", 24),
        ),
        enroll_poll_interval_seconds=_parse_interval_seconds(
            "distribution.enroll_poll_interval",
            sec.get("enroll_poll_interval", 30),
        ),
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/controller/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/config.py tests/unit/controller/test_config.py
git commit -m "feat(config): default_shard_id + enrollment ttl + enroll poll interval"
```

---

## Task 8: `user-onboard` CLI command

**Files:**
- Modify: `src/mthydra/controller/cli.py` (parser + `_cmd_user_onboard`)
- Test: `tests/unit/controller/test_cli_user_onboard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_cli_user_onboard.py
from __future__ import annotations

import subprocess
import sys


def _run(args, env=None):
    return subprocess.run([sys.executable, "-m", "mthydra.controller.cli", *args],
                          capture_output=True, text=True, env=env)


def test_user_onboard_creates_user_assigns_default_shard_prints_link(tmp_path):
    db = str(tmp_path / "s.sqlite")
    # init the schema via the existing init path used by other CLI tests.
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    c = connect(db); apply_schema(c); c.close()

    r = _run(["user-onboard", "granny", "--display-name", "Granny",
              "--db-path", db])
    assert r.returncode == 0, r.stderr
    assert "https://t.me/" not in r.stdout or "?start=" in r.stdout  # link printed when bot username resolvable; token always printed
    assert "?start=" in r.stdout

    c = connect(db)
    assert c.execute("SELECT current_shard_id FROM users WHERE user_id='granny'"
                     ).fetchone()[0] == "default_shard"
    assert c.execute("SELECT COUNT(*) FROM pending_enrollments WHERE user_id='granny'"
                     ).fetchone()[0] == 1
    c.close()
```

> Implementer: match the invocation style other CLI tests in this repo use
> (some call a `main([...])` entrypoint directly instead of `subprocess`). Prefer
> the in-process style if that's the established pattern — it's faster and lets
> you inject a fake bot username. The behavioral assertions above are the
> contract regardless of invocation style.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/controller/test_cli_user_onboard.py -v`
Expected: FAIL (`invalid choice: 'user-onboard'`).

- [ ] **Step 3: Implement**

Add the subparser (near the other user/shard parsers, ~line 494):
```python
    uo = sub.add_parser("user-onboard",
                        help="one-shot: create user + assign shard + enrollment link")
    uo.add_argument("user_id")
    uo.add_argument("--shard", dest="shard_id", default=None,
                    help="shard to assign (default: configured default_shard_id)")
    uo.add_argument("--email", default=None)
    uo.add_argument("--display-name", dest="display_name", default=None)
    uo.add_argument("--out-of-band-channel", dest="oob", default="unspecified")
    uo.add_argument("--ttl-hours", dest="ttl_hours", type=int, default=None)
    uo.add_argument("--db-path", default=DEFAULT_DB)
    uo.add_argument("--config", default="/etc/mthydra/controller.toml")
```

Add the dispatch line with the other handlers (~line 1146):
```python
    if args.cmd == "user-onboard":
        return _cmd_user_onboard(args)
```

Add the handler:
```python
def _cmd_user_onboard(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.distribution import enrollment
    from mthydra.controller.distribution.sinks import TelegramDistributionSink
    from mthydra.controller.state.db import connect
    from mthydra.controller.state import user_channels as _uc
    from mthydra.controller.state.shards import create_shard
    from mthydra.controller.state.users_shards import add_user, assign_user_to_shard

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"user-onboard: config error: {e}", file=sys.stderr)
        return 2

    shard_id = args.shard_id or cfg.shard_manager.default_shard_id
    ttl_seconds = (args.ttl_hours or cfg.distribution.enrollment_token_ttl_hours) * 3600
    now = _now()
    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "user-onboard")
        if rc is not None:
            return rc
        # 1. user (idempotent: ignore "already exists")
        try:
            add_user(conn, args.user_id, args.display_name, args.oob, now)
        except sqlite3.IntegrityError:
            pass  # user already exists — onboarding is re-runnable
        # 2. shard (auto-create if missing)
        shard_exists = conn.execute(
            "SELECT 1 FROM shards WHERE shard_id=?", (shard_id,)
        ).fetchone() is not None
        if not shard_exists:
            create_shard(conn, shard_id=shard_id, members=[],
                         target_size=cfg.shard_manager.target_size, at=now)
        # 3. assign user to shard (idempotent)
        assign_user_to_shard(conn, args.user_id, shard_id, at=now,
                             max_size=cfg.shard_manager.max_size)
        # 4. email channel if provided (chat_id arrives later via enrollment)
        if args.email:
            _uc.set_channels(conn, args.user_id, telegram_chat_id=None,
                             email_addr=args.email, at=now)
        else:
            print("user-onboard: no --email; Telegram-only (allowed, less robust)",
                  file=sys.stderr)
        # 5. mint token + deep link
        token = enrollment.mint(conn, args.user_id, ttl_seconds=ttl_seconds, now=now)
        conn.commit()
    except (LookupError, ValueError) as e:
        print(f"user-onboard: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    bot_username = None
    if cfg.distribution.telegram is not None:
        try:
            bot_username = TelegramDistributionSink(
                cfg.distribution.telegram.bot_token).get_me()
        except Exception:
            bot_username = None
    print(f"user-onboard: {args.user_id} -> shard {shard_id}")
    if bot_username:
        print("Send this link to the user (they tap it, then tap Start):")
        print("  " + enrollment.deep_link(bot_username, token))
    else:
        print("Enrollment token (build the link as "
              "https://t.me/<distbot>?start=<token>):")
        print("  " + token)
    return 0
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/controller/test_cli_user_onboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli_user_onboard.py
git commit -m "feat(cli): user-onboard one-command user setup + enrollment link"
```

---

## Task 9: Wire `--shard` into provision-seed + arm EnrollmentPoller in serve

**Files:**
- Modify: `src/mthydra/controller/cli.py` (provision-seed parser + handler; serve loop)
- Test: `tests/unit/controller/test_cli_provision_shard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_cli_provision_shard.py
from __future__ import annotations

import argparse


def test_provision_seed_parser_has_shard_flag():
    from mthydra.controller.cli import build_parser  # the module's parser builder
    p = build_parser()
    ns = p.parse_args(["provision-seed", "--provider", "tw", "--region", "ru",
                       "--shard", "s-hi"])
    assert ns.shard_id == "s-hi"
```

> `build_parser()` is the real parser-builder in `cli.py` (confirmed). If
> `provision-seed` has required args beyond `--provider/--region`, include their
> minimal values so `parse_args` succeeds.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/controller/test_cli_provision_shard.py -v`
Expected: FAIL (`unrecognized arguments: --shard`).

- [ ] **Step 3: Implement**

On the `provision-seed` subparser (~line 445) add:
```python
    ps.add_argument("--shard", dest="shard_id", default=None,
                    help="shard to bind this box to (default: default_shard)")
```

In `_cmd_provision_seed`, pass it through to `provision_box(...)`:
```python
            seed = provision_box(
                ...,
                shard_id=args.shard_id,
            )
```
(Add `shard_id=args.shard_id` to the existing `provision_box(...)` call's kwargs.)

Wire the `EnrollmentPoller` into the active serve loop. Near where
`dist_publisher` is constructed (~line 1939) and the schedulers are armed
(~line 1951-1964) and disarmed (~line 1979), add:
```python
    # ---- enrollment poller (deep-link onboarding) ----
    from mthydra.controller.distribution.enroll_poller import EnrollmentPoller
    from mthydra.controller.distribution.sinks import TelegramDistributionSink
    enroll_poller = None
    if cfg.distribution.telegram is not None:
        recv = TelegramDistributionSink(cfg.distribution.telegram.bot_token)
        enroll_poller = EnrollmentPoller(
            db_path=args.db_path,
            receive_client=recv,
            poll_interval_seconds=cfg.distribution.enroll_poll_interval_seconds,
            on_enrolled=lambda uid: dist_publisher.run_once(),
            mode=mode,
        )
```
In the arm block:
```python
        if enroll_poller is not None:
            enroll_poller.arm()
```
In the disarm block:
```python
        if enroll_poller is not None:
            enroll_poller.disarm()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/controller/test_cli_provision_shard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli_provision_shard.py
git commit -m "feat(cli): provision-seed --shard + arm EnrollmentPoller in serve"
```

---

## Task 10: Rewrite quickstart Part 8

**Files:**
- Modify: `doc/quickstart-mvp.md` (Part 8; note in Part 7)

- [ ] **Step 1: Rewrite Part 8.1 and 8.2**

Replace the 8.1 command block with:
```bash
mthydra-controller user-onboard me \
    --display-name "Me (test)" \
    --email youremail@gmail.com \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
```
Followed by prose: "Copy the printed `https://t.me/<bot>?start=…` link, open it
on your phone, tap **Start**. Within ~30s the controller captures your chat and
sends the first proxy delta to Telegram + email."

Replace 8.2 step 4's command block with:
```bash
mthydra-controller user-onboard <their-name> \
    --display-name "Their Name" \
    --email theiremail@gmail.com \
    --out-of-band-channel "signal:<their phone>" \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml
# (add --shard s-hi-risk to isolate a higher-risk contact in their own shard)
```
Followed by: "Send them the printed link out-of-band. They tap it, tap **Start**
— that's the whole job. Delete any mention of bot creation / getUpdates / chat
IDs." Remove the old `shard-create` and the broken
`shard-assign-box <their-name> --auto` lines entirely.

In Part 7, add a one-line note: "The box auto-binds to `default_shard` at
provisioning; pass `provision-seed --shard <id>` to use a dedicated shard."

- [ ] **Step 2: Verify by reading**

Run: `grep -n "user-onboard\|shard-assign-box .*--auto\|getUpdates" doc/quickstart-mvp.md`
Expected: `user-onboard` present; no `--auto` line; no per-user `getUpdates` step.

- [ ] **Step 3: Commit**

```bash
git add doc/quickstart-mvp.md
git commit -m "docs(quickstart): Part 8 uses user-onboard + deep-link; drop broken --auto"
```

---

## Final verification

- [ ] **Run the full unit suite**

Run: `python -m pytest tests/unit -q`
Expected: all green (baseline was 1286 passed + the new tests).

- [ ] **Ruff on touched files**

Run: `ruff check src/mthydra/controller/distribution/ src/mthydra/controller/state/schema.py src/mthydra/controller/cli.py src/mthydra/controller/config.py src/mthydra/controller/provisioning/seed.py`
Expected: no *new* violations (pre-existing UP017/SIM debt may remain in untouched code).

- [ ] **Push**

```bash
git push origin main
```
