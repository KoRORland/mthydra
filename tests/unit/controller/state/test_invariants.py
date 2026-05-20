import pytest

from mthydra.controller.state.authority import insert_authority, retire_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.invariants import InvariantViolation, check_all
from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema
from mthydra.descriptor.keys import generate_keypair

NOW = "2026-05-19T00:00:00Z"


def _seeded(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    priv, pub = generate_keypair()  # spec B: real keys
    insert_signing_key(conn, 1, priv, pub, "2026-05-18T00:00:00Z")
    return conn


def test_check_all_passes_on_clean_seeded_db(tmp_db_path):
    conn = _seeded(tmp_db_path)
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_all_rejects_unknown_schema_version(tmp_db_path):
    conn = _seeded(tmp_db_path)
    with pytest.raises(InvariantViolation, match="schema_version"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION + 99, now_iso=NOW)


def test_check_all_rejects_overlap_pool_and_burned(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at) VALUES ('x.org', 'in_use', '2026-05-18T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO burned_domains (domain, burned_at, reason) VALUES ('x.org', '2026-05-18T01:00:00Z', 'job2_kill')"
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="overlap"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_all_rejects_no_active_authority(tmp_db_path):
    conn = _seeded(tmp_db_path)
    retire_authority(conn, 1, at="2026-05-19T00:00:00Z")
    with pytest.raises(InvariantViolation, match="credential_authority"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_all_rejects_truly_impossible_state(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO backup_log (generation, created_at, size_bytes, sha256, pushed_at, index_updated_at, trigger) "
        "VALUES (1, '2026-05-18T00:00:00Z', 4096, 'abc', NULL, '2026-05-18T00:00:11Z', 'floor_timer')"
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="impossible"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


# ---------------------------------------------------------------------------
# Spec B invariant checks
# ---------------------------------------------------------------------------

def test_check_13_rejects_more_than_two_active_signing_keys(tmp_db_path):
    conn = _seeded(tmp_db_path)
    p2, k2 = generate_keypair()
    p3, k3 = generate_keypair()
    insert_signing_key(conn, 2, p2, k2, NOW)
    insert_signing_key(conn, 3, p3, k3, NOW)
    with pytest.raises(InvariantViolation, match="check 13"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_14_rejects_orphan_descriptor_fk(tmp_db_path):
    conn = _seeded(tmp_db_path)
    # Temporarily disable FK enforcement to insert an orphan row
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO descriptor_history "
        "(generation, payload, signed_at, valid_until, signing_key_generation, signature) "
        "VALUES (1, '{}', ?, ?, 99, X'')",
        (NOW, NOW),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(InvariantViolation, match="check 14"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_15_rejects_chain_break(tmp_db_path):
    from mthydra.descriptor.keys import sign as ed_sign
    from mthydra.descriptor.payload import DescriptorPayload, EUExit, canonical_bytes, payload_hash
    conn = _seeded(tmp_db_path)
    priv = conn.execute(
        "SELECT privkey FROM descriptor_signing_key WHERE retired_at IS NULL"
    ).fetchone()[0]
    priv = bytes(priv)
    # Insert gen 1 (correct)
    p1 = DescriptorPayload(1, 1, NOW, NOW, (), None, None)
    b1 = canonical_bytes(p1)
    sig1 = ed_sign(priv, b1)
    conn.execute(
        "INSERT INTO descriptor_history "
        "(generation, payload, signed_at, valid_until, signing_key_generation, signature) "
        "VALUES (1, ?, ?, ?, 1, ?)",
        (b1.decode(), NOW, NOW, sig1),
    )
    # Insert gen 2 with WRONG previous hash
    p2 = DescriptorPayload(2, 1, NOW, NOW, (), "bad_hash" * 8, None)
    b2 = canonical_bytes(p2)
    sig2 = ed_sign(priv, b2)
    conn.execute(
        "INSERT INTO descriptor_history "
        "(generation, payload, signed_at, valid_until, signing_key_generation, signature) "
        "VALUES (2, ?, ?, ?, 1, ?)",
        (b2.decode(), NOW, NOW, sig2),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 15"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_16_rejects_placeholder_in_production(tmp_db_path):
    conn = _seeded(tmp_db_path)
    # Replace the real key with a placeholder
    conn.execute(
        "UPDATE descriptor_signing_key SET privkey=? WHERE retired_at IS NULL",
        (b"PRIV-DESC-" + b"\x00" * 22,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 16"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION,
                  mode="production", now_iso=NOW)


def test_check_16_allows_placeholder_in_offline_mode(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "UPDATE descriptor_signing_key SET privkey=? WHERE retired_at IS NULL",
        (b"PRIV-DESC-" + b"\x00" * 22,),
    )
    conn.commit()
    # Should NOT raise in offline mode
    check_all(conn, expected_schema_version=SCHEMA_VERSION,
              mode="offline", now_iso=NOW)


# ---------------------------------------------------------------------------
# Spec C invariant checks (#17–#20)
# ---------------------------------------------------------------------------

def test_check_17_rejects_missing_triggers(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute("DROP TRIGGER IF EXISTS cover_pool_reject_burned")
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 17"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_18_rejects_in_use_without_entered_at(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute("PRAGMA foreign_keys=OFF")  # box FK not relevant here
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, "
        "last_verified_at, verified_from_vantage, assigned_box_id) "
        "VALUES ('x.org', 'in_use', ?, ?, 'ru-vps-01', 'box-x')",
        (NOW, NOW),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 18"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_19_rejects_in_use_without_live_box(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, "
        "last_verified_at, verified_from_vantage, assigned_box_id, entered_in_use_at) "
        "VALUES ('x.org', 'in_use', ?, ?, 'ru-vps-01', 'missing-box', ?)",
        (NOW, NOW, NOW),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 19"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_20_rejects_verified_without_vantage(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, last_verified_at) "
        "VALUES ('x.org', 'candidate_verified', ?, ?)",
        (NOW, NOW),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 20"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


# ---------------------------------------------------------------------------
# Spec F invariant checks (#21–#23)
# ---------------------------------------------------------------------------

def test_check_21_rejects_missing_node_state(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute("DELETE FROM node_state")
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 21"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_22_active_requires_authority(tmp_db_path):
    conn = _seeded(tmp_db_path)
    # _seeded() inserts authority+key; node_state default 'active'. Retire authority.
    conn.execute("UPDATE credential_authority SET retired_at=?", (NOW,))
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 22"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_23_standby_must_be_skeleton(tmp_db_path):
    """A standby with a credential_authority row is structurally invalid."""
    conn = _seeded(tmp_db_path)  # has authority + key
    conn.execute("UPDATE node_state SET role='standby' WHERE rowid=1")
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 23"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_23_standby_with_only_b2_credential_passes(tmp_db_path):
    """The skeleton-DB invariant has one carve-out: B2 provider credential."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.tokens import set_provider_credential
    conn = connect(tmp_db_path)
    apply_schema(conn)
    conn.execute("UPDATE node_state SET role='standby' WHERE rowid=1")
    conn.commit()
    set_provider_credential(conn, provider="b2", credential="id:secret", at=NOW)
    # Must NOT raise.
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_23_standby_with_non_b2_credential_fails(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.tokens import set_provider_credential
    conn = connect(tmp_db_path)
    apply_schema(conn)
    conn.execute("UPDATE node_state SET role='standby' WHERE rowid=1")
    conn.commit()
    set_provider_credential(conn, provider="aws", credential="id:secret", at=NOW)
    with pytest.raises(InvariantViolation, match="check 23"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)
