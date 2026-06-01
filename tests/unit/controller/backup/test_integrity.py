"""V-Task 2 — backup integrity smoke sweep."""
from __future__ import annotations

import hashlib
import random

import pytest

from mthydra.controller.backup.integrity import BackupIntegritySweep
from mthydra.controller.state.backup_log import record_pushed, record_started
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    c = connect(p)
    apply_schema(c)
    c.close()
    return p


class _FakeDest:
    """In-memory S3Destination stand-in. Tests inject the bytes-to-return
    per generation; missing key raises KeyError to simulate AccessDenied
    or NoSuchKey from S3."""

    def __init__(self, blobs: dict[int, bytes]) -> None:
        self._blobs = blobs

    def get_blob(self, generation: int) -> bytes:
        if generation not in self._blobs:
            raise KeyError(f"no blob for gen {generation}")
        return self._blobs[generation]


def _seed_gen(p, gen: int, blob: bytes, at: str = "2026-06-01T00:00:00Z") -> str:
    """Insert a backup_log row with sha256 matching `blob`. Returns the sha."""
    sha = hashlib.sha256(blob).hexdigest()
    conn = connect(p)
    record_started(conn, generation=gen, created_at=at, trigger="floor")
    record_pushed(conn, generation=gen, sha256=sha,
                  size_bytes=len(blob), pushed_at=at)
    conn.close()
    return sha


def test_integrity_pass_stamps_proven_and_clears_prior_fail(db):
    """V-2: a matching sha256 stamps backup_integrity_proven (singleton)
    and clears any prior backup_integrity_failed::<gen> for the same gen."""
    blob = b"encrypted-stream-bytes-x" * 10
    _seed_gen(db, gen=7, blob=blob)
    # Plant a stale failure row that should clear on pass.
    conn = connect(db)
    from mthydra.controller.state.obligations import set_obligation
    set_obligation(conn, obligation_id="backup_integrity_failed::7",
                   last_proven_at="2026-05-25T00:00:00Z",
                   proven_by="x", next_due_at="2026-05-25T00:00:00Z",
                   details="stale")
    conn.commit(); conn.close()

    sweep = BackupIntegritySweep(
        db_path=db, destination=_FakeDest({7: blob}),
        mode="offline", clock=lambda: "2026-06-01T01:00:00Z",
        rng=random.Random(0),
    )
    result = sweep.run_once()
    assert result == {"checked": 7, "ok": True, "reason": "ok"}
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "backup_integrity_proven" in obs
    assert "backup_integrity_failed::7" not in obs
    conn.close()


def test_integrity_mismatch_raises_per_gen_anti(db):
    """V-2: when the downloaded blob's sha256 doesn't match what we
    recorded at write time, raise backup_integrity_failed::<gen> with
    the mismatch in details_json. NO proven obligation stamped — pool
    of one gen isn't trustworthy if that gen is corrupt."""
    real = b"original-blob"
    _seed_gen(db, gen=42, blob=real)
    corrupted = b"DIFFERENT-bytes-from-original"
    sweep = BackupIntegritySweep(
        db_path=db, destination=_FakeDest({42: corrupted}),
        mode="offline", clock=lambda: "2026-06-01T01:00:00Z",
        rng=random.Random(0),
    )
    result = sweep.run_once()
    assert result["checked"] == 42
    assert result["ok"] is False
    assert "sha256 mismatch" in result["reason"]
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "backup_integrity_failed::42" in obs
    assert "sha256 mismatch" in (obs["backup_integrity_failed::42"].details or "")
    assert "backup_integrity_proven" not in obs
    conn.close()


def test_integrity_download_failure_raises_anti(db):
    """V-2: a get_blob exception (AccessDenied, NoSuchKey, network)
    must NOT crash the sweep — it lands as the per-gen anti-obligation
    with the exception class + message."""
    blob = b"ok-blob"
    _seed_gen(db, gen=99, blob=blob)
    sweep = BackupIntegritySweep(
        db_path=db, destination=_FakeDest({}),  # no blob → KeyError
        mode="offline", clock=lambda: "2026-06-01T01:00:00Z",
        rng=random.Random(0),
    )
    result = sweep.run_once()
    assert result["ok"] is False
    assert "download-failed" in result["reason"]
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "backup_integrity_failed::99" in obs
    conn.close()


def test_integrity_empty_pool_is_noop(db):
    """V-2: no backups pushed yet (fresh install) → don't stamp the
    proven obligation. An empty fleet isn't proven, it's untestable."""
    sweep = BackupIntegritySweep(
        db_path=db, destination=_FakeDest({}),
        mode="offline", clock=lambda: "2026-06-01T01:00:00Z",
    )
    result = sweep.run_once()
    assert result == {"checked": None, "ok": False, "reason": "no-backups"}
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "backup_integrity_proven" not in obs
    conn.close()


def test_integrity_picks_from_recent_window(db):
    """V-2: with multiple recent gens, the sweep picks one (via injected
    rng); over many runs all recent gens get coverage."""
    blobs = {}
    for gen in range(1, 11):
        blob = f"blob-{gen}".encode() * 100
        blobs[gen] = blob
        _seed_gen(db, gen=gen, blob=blob, at="2026-06-01T00:00:00Z")
    # Fixed seed → deterministic choice for the test.
    sweep = BackupIntegritySweep(
        db_path=db, destination=_FakeDest(blobs),
        recent_window=10, mode="offline",
        clock=lambda: "2026-06-01T01:00:00Z",
        rng=random.Random(12345),
    )
    chosen_gens = set()
    for _ in range(20):
        r = sweep.run_once()
        chosen_gens.add(r["checked"])
    assert chosen_gens.issubset(set(range(1, 11)))
    assert len(chosen_gens) > 1  # not stuck on one gen
