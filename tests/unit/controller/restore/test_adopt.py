"""Tests for adopt-restored-state (spec A §7.2)."""
import pytest

from mthydra.controller.restore.adopt import AdoptError, adopt_restored_state
from mthydra.controller.state.authority import insert_authority, list_authorities
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.schema import apply_schema


def _seed(path):
    conn = connect(path)
    apply_schema(conn)
    insert_authority(conn, 1, "P1", "K1", "2026-05-18T00:00:00Z")
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    conn.close()


def test_adopt_replaces_existing_and_preserves_old(tmp_path):
    live = tmp_path / "state.sqlite"
    _seed(live)
    restored = tmp_path / "restored.sqlite"
    _seed(restored)
    adopt_restored_state(
        live_path=live,
        restored_path=restored,
        case=None,
        rotate_published_subset=False,
        at="2026-05-18T05:00:00Z",
    )
    assert live.exists()
    assert not restored.exists()
    preadopt = list(tmp_path.glob("state.sqlite.preadopt.*"))
    assert len(preadopt) == 1


def test_adopt_fsyncs_live_file_and_parent_dir(tmp_path, monkeypatch):
    """L4: adoption fsyncs the new live DB and its directory for crash durability."""
    import mthydra.controller.restore.adopt as adopt_mod
    live = tmp_path / "state.sqlite"
    _seed(live)
    restored = tmp_path / "restored.sqlite"
    _seed(restored)

    fsynced: list[int] = []
    real_fsync = adopt_mod.os.fsync
    monkeypatch.setattr(adopt_mod.os, "fsync",
                        lambda fd: (fsynced.append(fd), real_fsync(fd))[0])

    adopt_restored_state(
        live_path=live, restored_path=restored, case=None,
        rotate_published_subset=False, at="2026-05-18T05:00:00Z",
    )
    # At least two fsyncs attempted: the live DB file and the parent directory.
    assert len(fsynced) >= 2
    assert live.exists()


def test_adopt_case_b_inserts_new_authority(tmp_path):
    live = tmp_path / "state.sqlite"
    _seed(live)
    restored = tmp_path / "restored.sqlite"
    _seed(restored)
    adopt_restored_state(
        live_path=live,
        restored_path=restored,
        case="B",
        rotate_published_subset=False,
        at="2026-05-18T05:00:00Z",
    )
    conn = connect(live)
    rows = list_authorities(conn)
    assert len(rows) == 2
    assert rows[0].retired_at is not None   # original retired
    assert rows[1].retired_at is None       # new one is active


def test_adopt_case_a_leaves_authority_unchanged(tmp_path):
    live = tmp_path / "state.sqlite"
    _seed(live)
    restored = tmp_path / "restored.sqlite"
    _seed(restored)
    adopt_restored_state(
        live_path=live,
        restored_path=restored,
        case="A",
        rotate_published_subset=False,
        at="2026-05-18T05:00:00Z",
    )
    conn = connect(live)
    rows = list_authorities(conn)
    assert len(rows) == 1
    assert rows[0].retired_at is None


def test_adopt_rotate_published_subset_inserts_marker(tmp_path):
    live = tmp_path / "state.sqlite"
    _seed(live)
    restored = tmp_path / "restored.sqlite"
    _seed(restored)
    adopt_restored_state(
        live_path=live,
        restored_path=restored,
        case=None,
        rotate_published_subset=True,
        at="2026-05-18T05:00:00Z",
    )
    conn = connect(live)
    row = conn.execute(
        "SELECT payload_json FROM published_subsets WHERE payload_json LIKE '%_pending_rotation%'"
    ).fetchone()
    assert row is not None


def test_adopt_refuses_invalid_case(tmp_path):
    live = tmp_path / "state.sqlite"
    _seed(live)
    restored = tmp_path / "restored.sqlite"
    _seed(restored)
    with pytest.raises(AdoptError, match="case"):
        adopt_restored_state(
            live_path=live,
            restored_path=restored,
            case="C",
            rotate_published_subset=False,
            at="2026-05-18T05:00:00Z",
        )


def test_adopt_refuses_missing_restored(tmp_path):
    with pytest.raises(AdoptError, match="not found"):
        adopt_restored_state(
            live_path=tmp_path / "state.sqlite",
            restored_path=tmp_path / "missing.sqlite",
            case=None,
            rotate_published_subset=False,
            at="2026-05-18T05:00:00Z",
        )


def test_adopt_no_live_db_works(tmp_path):
    """Adoption when there's no existing live DB (fresh host) should succeed."""
    live = tmp_path / "state.sqlite"
    restored = tmp_path / "restored.sqlite"
    _seed(restored)
    adopt_restored_state(
        live_path=live,
        restored_path=restored,
        case=None,
        rotate_published_subset=False,
        at="2026-05-18T05:00:00Z",
    )
    assert live.exists()
    preadopt = list(tmp_path.glob("state.sqlite.preadopt.*"))
    assert len(preadopt) == 0  # nothing to preserve
