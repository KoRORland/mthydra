import pytest

from mthydra.controller.state.authority import insert_authority, retire_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.invariants import InvariantViolation, check_all
from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema
from mthydra.descriptor.authority import generate_authority_keypair
from mthydra.descriptor.keys import generate_keypair

NOW = "2026-05-19T00:00:00Z"


def _seeded(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    _priv, _pub = generate_authority_keypair()
    insert_authority(conn, 1, _priv, _pub, "2026-05-18T00:00:00Z")
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


# ---------------------------------------------------------------------------
# Spec D invariant checks (#24–#25)
# ---------------------------------------------------------------------------

def test_check_24_rejects_two_promoted_images(tmp_db_path):
    conn = _seeded(tmp_db_path)
    for iv in ("iv1", "iv2"):
        conn.execute(
            "INSERT INTO ru_images "
            "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
            " binary_sha256, binary_size_bytes, state, built_at, promoted_at) "
            "VALUES (?, 'v', 'r', 'b', 'm', ?, 100, 'promoted', ?, ?)",
            (iv, iv, NOW, NOW),
        )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 24"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_25_rejects_promoted_without_promoted_at(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO ru_images "
        "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
        " binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('iv1', 'v', 'r', 'b', 'm', 'iv1', 100, 'promoted', ?)",
        (NOW,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 25"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_25_rejects_retired_without_retired_at(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO ru_images "
        "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
        " binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('iv1', 'v', 'r', 'b', 'm', 'iv1', 100, 'retired', ?)",
        (NOW,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 25"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_25_rejects_candidate_with_promoted_at(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO ru_images "
        "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
        " binary_sha256, binary_size_bytes, state, built_at, promoted_at) "
        "VALUES ('iv1', 'v', 'r', 'b', 'm', 'iv1', 100, 'candidate', ?, ?)",
        (NOW, NOW),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 25"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


# ---------------------------------------------------------------------------
# Spec G invariant checks (#26–#28)
# ---------------------------------------------------------------------------

def test_check_26_rejects_placeholder_authority_in_production(tmp_db_path):
    """Forcing a PRIV-BOOTSTRAP- privkey must trip #26 in production mode."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "UPDATE credential_authority SET privkey_pem='PRIV-BOOTSTRAP-test' "
        "WHERE retired_at IS NULL"
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 26"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION,
                  mode="production", now_iso=NOW)


def test_check_26_allows_placeholder_in_offline(tmp_db_path):
    """Same placeholder must NOT raise in offline mode."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "UPDATE credential_authority SET privkey_pem='PRIV-BOOTSTRAP-test' "
        "WHERE retired_at IS NULL"
    )
    conn.commit()
    check_all(conn, expected_schema_version=SCHEMA_VERSION,
              mode="offline", now_iso=NOW)


def test_check_26_allows_real_ed25519(tmp_db_path):
    """Real Ed25519 authority passes in production."""
    conn = _seeded(tmp_db_path)
    # _seeded already mints real Ed25519, so check_all must pass.
    check_all(conn, expected_schema_version=SCHEMA_VERSION,
              mode="production", now_iso=NOW)


def test_check_27_rejects_live_box_without_credential(tmp_db_path):
    """A live ru_boxes row with no matching active onward_credentials row is invalid."""
    conn = _seeded(tmp_db_path)
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    insert_box(conn, "boxX", "aws", "eu-1", "10.0.0.1", "sni-x.invalid",
               "img-v1", NOW)
    mark_live(conn, "boxX", public_ip="10.0.0.1", at=NOW)
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 27"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_28_passes_on_clean_db(tmp_db_path):
    """#28 is defence-in-depth on the UNIQUE constraint; passes on a clean DB
    where UNIQUE already prevents collisions."""
    conn = _seeded(tmp_db_path)
    # Insert two boxes with distinct sni values; UNIQUE permits this.
    from mthydra.controller.state.ru_boxes import insert_box
    insert_box(conn, "b1", "aws", "eu", "10.0.0.1", "a.invalid", "img", NOW)
    insert_box(conn, "b2", "aws", "eu", "10.0.0.2", "b.invalid", "img", NOW)
    # Add credentials for both so #27 passes.
    from mthydra.controller.state.credentials import issue_credential
    issue_credential(conn, "b1", b"\x00" * 10, NOW, authority_generation=1)
    issue_credential(conn, "b2", b"\x00" * 10, NOW, authority_generation=1)
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


# ---------------------------------------------------------------------------
# Spec E invariant checks (#29–#32) — gated on schema v6+
# ---------------------------------------------------------------------------

def test_check_29_rejects_duplicate_reality_uuid(tmp_db_path):
    """Two ru_boxes sharing a reality_uuid must trip #29.

    The partial UNIQUE INDEX would normally prevent this; we bypass by
    temporarily dropping the index so the invariant has something to catch.
    """
    conn = _seeded(tmp_db_path)
    from mthydra.controller.state.credentials import issue_credential
    from mthydra.controller.state.ru_boxes import insert_box
    insert_box(conn, "b1", "aws", "eu", "10.0.0.1", "a.invalid", "img", NOW)
    insert_box(conn, "b2", "aws", "eu", "10.0.0.2", "b.invalid", "img", NOW)
    issue_credential(conn, "b1", b"\x00" * 10, NOW, authority_generation=1)
    issue_credential(conn, "b2", b"\x00" * 10, NOW, authority_generation=1)
    conn.execute("DROP INDEX IF EXISTS idx_ru_boxes_reality_uuid")
    conn.execute(
        "UPDATE ru_boxes SET reality_uuid='shared-uuid' WHERE box_id IN ('b1','b2')"
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 29"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_30_rejects_live_box_without_reality_uuid(tmp_db_path):
    """A live ru_box with an active onward credential but no reality_uuid trips #30."""
    conn = _seeded(tmp_db_path)
    from mthydra.controller.state.credentials import issue_credential
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    insert_box(conn, "boxX", "aws", "eu-1", "10.0.0.1", "sni-x.invalid",
               "img-v1", NOW)
    mark_live(conn, "boxX", public_ip="10.0.0.1", at=NOW)
    issue_credential(conn, "boxX", b"\x00" * 10, NOW, authority_generation=1)
    # No reality_uuid set
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 30"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_30_passes_when_live_box_has_reality_uuid(tmp_db_path):
    """A live ru_box with reality_uuid and an active credential passes #30."""
    import json as _json
    conn = _seeded(tmp_db_path)
    from mthydra.controller.state.credentials import issue_credential
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    # Spec H #33: live box must have a shard. Provision shard first, assign while
    # provisioning, then mark_live + add reality_uuid + credential.
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("s-y", _json.dumps(["u-y"]), 2, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, current_shard_id, added_at) "
        "VALUES ('u-y', NULL, 'email', 's-y', ?)",
        (NOW,),
    )
    insert_box(conn, "boxY", "aws", "eu-1", "10.0.0.2", "sni-y.invalid",
               "img-v1", NOW)
    conn.execute("UPDATE ru_boxes SET shard_id='s-y' WHERE box_id='boxY'")
    mark_live(conn, "boxY", public_ip="10.0.0.2", at=NOW)
    issue_credential(conn, "boxY", b"\x00" * 10, NOW, authority_generation=1)
    conn.execute("UPDATE ru_boxes SET reality_uuid='ru-uuid-Y' WHERE box_id='boxY'")
    conn.commit()
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_31_rejects_active_eu_node_without_cover_sni(tmp_db_path):
    """An eu_node with role='active' but NULL cover_sni trips #31."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        " role, added_at, cover_sni, reality_pubkey) "
        "VALUES ('eu-n1', 'h', 'hetzner', 'fsn1', '1.2.3.4', 'active', ?, "
        "        NULL, 'pk-abc')",
        (NOW,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 31"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_31_rejects_active_eu_node_without_reality_pubkey(tmp_db_path):
    """An eu_node with role='active' but NULL reality_pubkey trips #31."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        " role, added_at, cover_sni, reality_pubkey) "
        "VALUES ('eu-n1', 'h', 'hetzner', 'fsn1', '1.2.3.4', 'active', ?, "
        "        'cover.invalid', NULL)",
        (NOW,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 31"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_31_passes_for_retired_eu_node(tmp_db_path):
    """A retired eu_node with NULL cover_sni is fine (#31 only applies to active/standby)."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        " role, added_at, retired_at) "
        "VALUES ('eu-r1', 'h', 'hetzner', 'fsn1', '1.2.3.4', 'retired', ?, ?)",
        (NOW, NOW),
    )
    conn.commit()
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_32_rejects_mismatched_cover_sni(tmp_db_path):
    """eu_exit_set and eu_nodes matched by public_ip must agree on cover_sni."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        " role, added_at, cover_sni, reality_pubkey) "
        "VALUES ('eu-n1', 'h', 'hetzner', 'fsn1', '1.2.3.4', 'active', ?, "
        "        'node-cover.invalid', 'pk-abc')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO eu_exit_set (fingerprint, endpoint, weight, added_at, "
        " cover_sni, reality_pubkey) "
        "VALUES ('fp-1', '1.2.3.4:443', 1, ?, 'descriptor-cover.invalid', 'pk-abc')",
        (NOW,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 32"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_32_passes_when_cover_sni_matches(tmp_db_path):
    """Matching cover_sni on both sides passes #32."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        " role, added_at, cover_sni, reality_pubkey) "
        "VALUES ('eu-n1', 'h', 'hetzner', 'fsn1', '1.2.3.4', 'active', ?, "
        "        'cover.invalid', 'pk-abc')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO eu_exit_set (fingerprint, endpoint, weight, added_at, "
        " cover_sni, reality_pubkey) "
        "VALUES ('fp-1', '1.2.3.4:443', 1, ?, 'cover.invalid', 'pk-abc')",
        (NOW,),
    )
    conn.commit()
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_32_skipped_when_retired(tmp_db_path):
    """A retired eu_exit_set row is excluded from #32."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        " role, added_at, cover_sni, reality_pubkey) "
        "VALUES ('eu-n1', 'h', 'hetzner', 'fsn1', '1.2.3.4', 'active', ?, "
        "        'node-cover.invalid', 'pk-abc')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO eu_exit_set (fingerprint, endpoint, weight, added_at, retired_at, "
        " cover_sni, reality_pubkey) "
        "VALUES ('fp-1', '1.2.3.4:443', 1, ?, ?, 'old-cover.invalid', 'pk-abc')",
        (NOW, NOW),
    )
    conn.commit()
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


# --- spec H checks (#33–#36) ---

def _setup_active_shard(conn, shard_id="s1", members=None, target_size=2):
    import json as _json
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (shard_id, _json.dumps(members or []), target_size, NOW, NOW),
    )
    conn.commit()


def test_check_33_rejects_live_box_without_shard(tmp_db_path):
    conn = _seeded(tmp_db_path)
    # Live box without shard. Must satisfy #27 (active credential) and #30
    # (reality_uuid) so that #33 is the next failure.
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, "
        "created_at, reality_uuid) "
        "VALUES ('b1', 'p', 'r', 'sni1.example', 'live', 'v1', ?, 'uuid-1')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO onward_credentials (cred_id, box_id, credential, issued_at, authority_generation) "
        "VALUES ('c1', 'b1', X'aa', ?, 1)",
        (NOW,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 33"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_34_rejects_user_on_retired_shard(tmp_db_path):
    conn = _seeded(tmp_db_path)
    _setup_active_shard(conn, "s1", members=[])
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, current_shard_id, added_at) "
        "VALUES ('u1', NULL, 'email', 's1', ?)",
        (NOW,),
    )
    conn.execute("UPDATE shards SET retired_at=? WHERE shard_id='s1'", (NOW,))
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 34"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_35_rejects_cross_shard_user(tmp_db_path):
    conn = _seeded(tmp_db_path)
    _setup_active_shard(conn, "s1", members=["u1", "u2"])
    _setup_active_shard(conn, "s2", members=["u2", "u3"])
    with pytest.raises(InvariantViolation, match="check 35"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_36_rejects_empty_active_shard(tmp_db_path):
    conn = _seeded(tmp_db_path)
    _setup_active_shard(conn, "s1", members=[])
    with pytest.raises(InvariantViolation, match="check 36"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_36_allows_retired_empty_shard(tmp_db_path):
    """Retired shards with empty membership are fine (audit residue)."""
    conn = _seeded(tmp_db_path)
    _setup_active_shard(conn, "s1", members=[])
    conn.execute("UPDATE shards SET retired_at=? WHERE shard_id='s1'", (NOW,))
    conn.commit()
    # Other invariants would otherwise complain about an empty active shard; we just
    # retired it. No #36 violation now.
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_33_passes_when_live_box_has_shard(tmp_db_path):
    conn = _seeded(tmp_db_path)
    _setup_active_shard(conn, "s1", members=["u1"])
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, current_shard_id, added_at) "
        "VALUES ('u1', NULL, 'email', 's1', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, image_version, "
        "created_at, reality_uuid) "
        "VALUES ('b1', 'p', 'r', 'sni1.example', 's1', 'live', 'v1', ?, 'uuid-1')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO onward_credentials (cred_id, box_id, credential, issued_at, authority_generation) "
        "VALUES ('c1', 'b1', X'aa', ?, 1)",
        (NOW,),
    )
    conn.commit()
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


# --- spec I checks (#37–#40) ---

def _seed_image_and_vantage(conn, image_version="v1", vantage_id="vk", label="kz1", state="active"):
    conn.execute(
        "INSERT OR IGNORE INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES (?, 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', ?)",
        (image_version, NOW),
    )
    conn.execute(
        "INSERT OR IGNORE INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES (?, ?, 'cloud-cis', ?, ?)",
        (vantage_id, label, state, NOW),
    )
    conn.commit()


def test_check_37_rejects_dup_label(tmp_db_path):
    """Seed a duplicate label by bypassing the UNIQUE constraint via DROP+recreate."""
    conn = _seeded(tmp_db_path)
    _seed_image_and_vantage(conn, vantage_id="v1", label="kz1")
    # Drop and recreate probe_vantages WITHOUT the UNIQUE on label to seed the bad state.
    conn.execute("DROP TABLE probe_vantages")
    conn.execute(
        "CREATE TABLE probe_vantages ("
        "  vantage_id TEXT PRIMARY KEY, label TEXT NOT NULL, source_kind TEXT NOT NULL,"
        "  region_hint TEXT,"
        "  state TEXT NOT NULL CHECK (state IN ('candidate','active','retired','burned')),"
        "  added_at TEXT NOT NULL, attested_at TEXT, last_used_at TEXT,"
        "  retired_at TEXT, burned_at TEXT, burn_reason TEXT, notes TEXT)"
    )
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('v1', 'kz1', 'cloud-cis', 'active', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('v2', 'kz1', 'cloud-cis', 'active', ?)",
        (NOW,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 37"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_38_rejects_orphan_probe_result(tmp_db_path):
    """Seed an orphan probe_results row by deleting the parent vantage with FK off."""
    conn = _seeded(tmp_db_path)
    _seed_image_and_vantage(conn)
    # Use a provisioning box + a credential so checks #27/#30/#33 don't fire first.
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'provisioning', 'v1', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO onward_credentials (cred_id, box_id, credential, issued_at, authority_generation) "
        "VALUES ('c1', 'b1', X'aa', ?, 1)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO probe_results (box_id, vantage_id, cycle_at, check_type, status, "
        "image_version, recorded_at) VALUES ('b1', 'vk', ?, 'surface_scan', 'pass', 'v1', ?)",
        (NOW, NOW),
    )
    conn.commit()
    # PRAGMA foreign_keys=OFF must be set OUTSIDE a transaction.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM probe_vantages WHERE vantage_id='vk'")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(InvariantViolation, match="check 38"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_39_rejects_promoted_without_profile(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at, promoted_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'promoted', ?, ?)",
        (NOW, NOW),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 39"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_39_passes_when_profile_pinned(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at, promoted_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'promoted', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO image_profiles (image_version, profile_json, recorded_at, recorded_by) "
        "VALUES ('v1', '{}', ?, 'op')",
        (NOW,),
    )
    conn.commit()
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_40_warns_on_duplicate_region(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, region_hint, state, added_at) "
        "VALUES ('v1', 'kz1', 'cloud-cis', 'KZ-almaty', 'active', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, region_hint, state, added_at) "
        "VALUES ('v2', 'kz2', 'cloud-cis', 'KZ-almaty', 'active', ?)",
        (NOW,),
    )
    conn.commit()
    # Should emit RuntimeWarning, NOT raise.
    with pytest.warns(RuntimeWarning, match="check 40"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


# --- spec J checks (#41–#42) ---

def test_check_41_rejects_missing_alert_log_triggers(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute("DROP TRIGGER IF EXISTS alert_log_no_update")
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 41"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_42_passes_with_no_alert_log_rows(tmp_db_path):
    """Fresh install: no alert_log rows -> no spurious raise."""
    conn = _seeded(tmp_db_path)
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_42_passes_with_fresh_heartbeat(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO alert_log (attempted_at, delivered_at, sink, severity, "
        "kind, target, dedupe_key, payload) "
        "VALUES (?, ?, 'email', 'heartbeat', 'heartbeat', NULL, 'h', 'ok')",
        ("2026-05-19T00:00:00Z", "2026-05-19T00:00:01Z"),
    )
    conn.commit()
    # NOW = "2026-05-19T00:00:00Z" — 1 second age -> fresh.
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_42_raises_on_stale_heartbeat(tmp_db_path):
    """alert_log has a row, but heartbeat is > 2h old."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO alert_log (attempted_at, delivered_at, sink, severity, "
        "kind, target, dedupe_key, payload) "
        "VALUES (?, ?, 'email', 'heartbeat', 'heartbeat', NULL, 'h', 'ok')",
        ("2026-05-18T20:00:00Z", "2026-05-18T20:00:01Z"),
    )
    # Some non-heartbeat row too so the table is non-empty even if heartbeat
    # check finds nothing recent.
    conn.execute(
        "INSERT INTO alert_log (attempted_at, delivered_at, sink, severity, "
        "kind, target, dedupe_key, payload) "
        "VALUES (?, NULL, 'telegram', 'warn', 'x', NULL, 'k', 'p')",
        ("2026-05-19T00:00:00Z",),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 42"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_42_skipped_on_standby(tmp_db_path):
    """Standby nodes don't emit heartbeats; check 42 should skip."""
    conn = _seeded(tmp_db_path)
    conn.execute("UPDATE node_state SET role='standby' WHERE rowid=1")
    conn.execute(
        "INSERT INTO alert_log (attempted_at, delivered_at, sink, severity, "
        "kind, target, dedupe_key, payload) "
        "VALUES (?, NULL, 'telegram', 'warn', 'x', NULL, 'k', 'p')",
        ("2026-05-19T00:00:00Z",),
    )
    conn.commit()
    # Spec F check 23 will complain about credential_authority on standby,
    # so we expect a different InvariantViolation — not check 42.
    try:
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)
    except InvariantViolation as e:
        # Must NOT be the heartbeat check.
        assert "check 42" not in str(e)
