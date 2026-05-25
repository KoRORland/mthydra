"""SQLite schema for the controller's runtime state."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 11

_TRIGGER_COVER_POOL_REJECT_BURNED = """
    CREATE TRIGGER IF NOT EXISTS cover_pool_reject_burned
    BEFORE INSERT ON cover_domain_pool
    WHEN EXISTS (SELECT 1 FROM burned_domains WHERE domain = NEW.domain)
    BEGIN
      SELECT RAISE(ABORT, 'cover-pool: domain is in burned_domains; never reuse');
    END
    """

_TRIGGER_BURNED_DOMAINS_NO_DELETE = """
    CREATE TRIGGER IF NOT EXISTS burned_domains_no_delete
    BEFORE DELETE ON burned_domains
    BEGIN
      SELECT RAISE(ABORT, 'cover-pool: burned_domains is append-only');
    END
    """

_TRIGGER_RU_BOXES_NO_CROSS_SHARD_REASSIGN = """
    CREATE TRIGGER IF NOT EXISTS ru_boxes_no_cross_shard_reassign
    BEFORE UPDATE OF shard_id ON ru_boxes
    WHEN OLD.shard_id IS NOT NULL
     AND OLD.shard_id IS NOT NEW.shard_id
     AND OLD.state != 'provisioning'
    BEGIN
      SELECT RAISE(ABORT, 'shard-manager: live/terminated boxes cannot change shard_id');
    END
    """

_TRIGGER_RU_BOXES_TERMINATED_KEEPS_SHARD = """
    CREATE TRIGGER IF NOT EXISTS ru_boxes_terminated_keeps_shard
    BEFORE UPDATE OF state ON ru_boxes
    WHEN NEW.state = 'terminated'
     AND NEW.shard_id IS NULL
     AND OLD.shard_id IS NOT NULL
    BEGIN
      SELECT RAISE(ABORT, 'shard-manager: terminating a box does not clear shard_id (history preservation)');
    END
    """

_TRIGGER_PROBE_VANTAGES_NO_RELABEL_BURNED = """
    CREATE TRIGGER IF NOT EXISTS probe_vantages_no_relabel_burned
    BEFORE INSERT ON probe_vantages
    WHEN EXISTS (SELECT 1 FROM probe_vantages WHERE label = NEW.label AND state='burned')
    BEGIN
      SELECT RAISE(ABORT, 'probe-vantage: label is in burned state; never reuse');
    END
    """

_TRIGGER_PROBE_VANTAGES_BURNED_NO_REVERT = """
    CREATE TRIGGER IF NOT EXISTS probe_vantages_burned_no_revert
    BEFORE UPDATE OF state ON probe_vantages
    WHEN OLD.state='burned' AND NEW.state != 'burned'
    BEGIN
      SELECT RAISE(ABORT, 'probe-vantage: burned state is monotonic');
    END
    """

_TRIGGER_PROBE_RESULTS_NO_UPDATE = """
    CREATE TRIGGER IF NOT EXISTS probe_results_no_update
    BEFORE UPDATE ON probe_results
    BEGIN
      SELECT RAISE(ABORT, 'probe-results: append-only');
    END
    """

_TRIGGER_PROBE_RESULTS_NO_DELETE = """
    CREATE TRIGGER IF NOT EXISTS probe_results_no_delete
    BEFORE DELETE ON probe_results
    BEGIN
      SELECT RAISE(ABORT, 'probe-results: append-only');
    END
    """

_TRIGGER_ALERT_LOG_NO_UPDATE = """
    CREATE TRIGGER IF NOT EXISTS alert_log_no_update
    BEFORE UPDATE ON alert_log
    BEGIN
      SELECT RAISE(ABORT, 'alert-log: append-only');
    END
    """

_TRIGGER_ALERT_LOG_NO_DELETE = """
    CREATE TRIGGER IF NOT EXISTS alert_log_no_delete
    BEFORE DELETE ON alert_log
    BEGIN
      SELECT RAISE(ABORT, 'alert-log: append-only');
    END
    """

_TRIGGER_DISTRIBUTION_LOG_NO_UPDATE = """
    CREATE TRIGGER IF NOT EXISTS distribution_log_no_update
    BEFORE UPDATE ON distribution_log
    BEGIN
      SELECT RAISE(ABORT, 'distribution-log: append-only');
    END
    """

_TRIGGER_DISTRIBUTION_LOG_NO_DELETE = """
    CREATE TRIGGER IF NOT EXISTS distribution_log_no_delete
    BEFORE DELETE ON distribution_log
    BEGIN
      SELECT RAISE(ABORT, 'distribution-log: append-only');
    END
    """

_TRIGGER_IMAGE_PROFILES_NO_UPDATE_FOR_RETIRED = """
    CREATE TRIGGER IF NOT EXISTS image_profiles_no_update_for_retired
    BEFORE UPDATE ON image_profiles
    WHEN EXISTS (
      SELECT 1 FROM ru_images
      WHERE image_version = NEW.image_version AND state = 'retired'
    )
    BEGIN
      SELECT RAISE(ABORT, 'image-profiles: row for retired image is forensic-immutable');
    END
    """

_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
      version    INTEGER NOT NULL,
      applied_at TEXT    NOT NULL,
      CHECK (rowid = 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cover_domain_pool (
      domain                TEXT PRIMARY KEY,
      state                 TEXT NOT NULL CHECK (state IN ('candidate_unverified','candidate_verified','in_use')),
      last_verified_at      TEXT,
      verified_from_vantage TEXT,
      assigned_box_id       TEXT,
      added_at              TEXT NOT NULL,
      notes                 TEXT,
      entered_in_use_at     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS burned_domains (
      domain      TEXT PRIMARY KEY,
      burned_at   TEXT NOT NULL,
      reason      TEXT NOT NULL,
      last_box_id TEXT,
      details     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS credential_authority (
      generation  INTEGER PRIMARY KEY,
      privkey_pem TEXT NOT NULL,
      pubkey_pem  TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      retired_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS descriptor_signing_key (
      generation  INTEGER PRIMARY KEY,
      privkey     BLOB NOT NULL,
      pubkey      BLOB NOT NULL,
      created_at  TEXT NOT NULL,
      retired_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS descriptor_history (
      generation             INTEGER PRIMARY KEY,
      payload                TEXT NOT NULL,
      signed_at              TEXT NOT NULL,
      valid_until            TEXT NOT NULL,
      signing_key_generation INTEGER NOT NULL,
      FOREIGN KEY (signing_key_generation) REFERENCES descriptor_signing_key(generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shards (
      shard_id           TEXT PRIMARY KEY,
      members_json       TEXT NOT NULL,
      last_reshuffled_at TEXT NOT NULL,
      created_at         TEXT NOT NULL,
      retired_at         TEXT,
      target_size        INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ru_boxes (
      box_id             TEXT PRIMARY KEY,
      provider           TEXT NOT NULL,
      region             TEXT NOT NULL,
      public_ip          TEXT,
      sni                TEXT UNIQUE NOT NULL,
      shard_id           TEXT,
      state              TEXT NOT NULL CHECK (state IN ('provisioning','live','terminated')),
      image_version      TEXT NOT NULL,
      created_at         TEXT NOT NULL,
      went_live_at       TEXT,
      terminated_at      TEXT,
      termination_reason TEXT,
      reality_uuid       TEXT,
      is_canary          INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY (shard_id) REFERENCES shards(shard_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_ru_boxes_reality_uuid
      ON ru_boxes(reality_uuid) WHERE reality_uuid IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS onward_credentials (
      cred_id              TEXT PRIMARY KEY,
      box_id               TEXT NOT NULL,
      credential           BLOB NOT NULL,
      issued_at            TEXT NOT NULL,
      revoked_at           TEXT,
      authority_generation INTEGER NOT NULL,
      FOREIGN KEY (box_id) REFERENCES ru_boxes(box_id),
      FOREIGN KEY (authority_generation) REFERENCES credential_authority(generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
      user_id              TEXT PRIMARY KEY,
      display_name         TEXT,
      out_of_band_channel  TEXT NOT NULL,
      current_shard_id     TEXT,
      added_at             TEXT NOT NULL,
      FOREIGN KEY (current_shard_id) REFERENCES shards(shard_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS published_subsets (
      publish_gen  INTEGER PRIMARY KEY AUTOINCREMENT,
      payload_json TEXT NOT NULL,
      published_at TEXT NOT NULL,
      channel      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS publishing_tokens (
      kind       TEXT PRIMARY KEY,
      value      TEXT NOT NULL,
      rotated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_api_credentials (
      provider   TEXT PRIMARY KEY,
      credential TEXT NOT NULL,
      rotated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS obligation_clocks (
      obligation_id  TEXT PRIMARY KEY,
      last_proven_at TEXT NOT NULL,
      proven_by      TEXT NOT NULL,
      details        TEXT,
      next_due_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_log (
      generation       INTEGER PRIMARY KEY,
      created_at       TEXT NOT NULL,
      size_bytes       INTEGER NOT NULL DEFAULT 0,
      sha256           TEXT NOT NULL DEFAULT '',
      pushed_at        TEXT,
      index_updated_at TEXT,
      trigger          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      ts           TEXT NOT NULL,
      actor        TEXT NOT NULL,
      action       TEXT NOT NULL,
      target       TEXT,
      details_json TEXT
    )
    """,
    # --- spec B additions ---
    """
    CREATE TABLE IF NOT EXISTS eu_exit_set (
      fingerprint    TEXT PRIMARY KEY,
      endpoint       TEXT NOT NULL,
      weight         INTEGER NOT NULL DEFAULT 1,
      added_at       TEXT NOT NULL,
      retired_at     TEXT,
      cover_sni      TEXT,
      reality_pubkey TEXT
    )
    """,
    # --- spec C additions: structural enforcement of T5 burned-set rule ---
    _TRIGGER_COVER_POOL_REJECT_BURNED,
    _TRIGGER_BURNED_DOMAINS_NO_DELETE,
    # --- spec F additions: EU node setup (active / standby) ---
    """
    CREATE TABLE IF NOT EXISTS node_state (
      role                        TEXT NOT NULL CHECK (role IN ('active','standby')),
      promoted_at                 TEXT,
      previous_role               TEXT,
      promotion_case              TEXT CHECK (promotion_case IN ('A','B')),
      promotion_backup_generation INTEGER,
      CHECK (rowid = 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eu_nodes (
      node_id                TEXT PRIMARY KEY,
      hostname               TEXT NOT NULL,
      provider               TEXT NOT NULL,
      region                 TEXT NOT NULL,
      public_ip              TEXT,
      role                   TEXT NOT NULL CHECK (role IN ('active','standby','retired')),
      added_at               TEXT NOT NULL,
      promoted_at            TEXT,
      retired_at             TEXT,
      last_heartbeat_at      TEXT,
      last_heartbeat_b2_etag TEXT,
      notes                  TEXT,
      cover_sni              TEXT,
      reality_pubkey         TEXT,
      data_exit_state        TEXT CHECK (data_exit_state IN ('healthy','degraded','stopped')),
      data_exit_started_at   TEXT
    )
    """,
    # --- spec D additions: RU image catalog ---
    """
    CREATE TABLE IF NOT EXISTS ru_images (
      image_version       TEXT    PRIMARY KEY,
      upstream_release    TEXT    NOT NULL,
      upstream_repo       TEXT    NOT NULL,
      binary_url          TEXT    NOT NULL,
      manifest_url        TEXT    NOT NULL,
      binary_sha256       TEXT    NOT NULL,
      binary_size_bytes   INTEGER NOT NULL,
      state               TEXT    NOT NULL CHECK (state IN ('candidate','promoted','retired')),
      built_at            TEXT    NOT NULL,
      promoted_at         TEXT,
      retired_at          TEXT,
      notes               TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_ru_images_state ON ru_images(state)
    """,
    # --- spec H additions: shard disjointness triggers ---
    _TRIGGER_RU_BOXES_NO_CROSS_SHARD_REASSIGN,
    _TRIGGER_RU_BOXES_TERMINATED_KEEPS_SHARD,
    # --- spec I additions: probe vantage harness ---
    """
    CREATE TABLE IF NOT EXISTS probe_vantages (
      vantage_id   TEXT PRIMARY KEY,
      label        TEXT UNIQUE NOT NULL,
      source_kind  TEXT NOT NULL,
      region_hint  TEXT,
      state        TEXT NOT NULL CHECK (state IN ('candidate','active','retired','burned')),
      added_at     TEXT NOT NULL,
      attested_at  TEXT,
      last_used_at TEXT,
      retired_at   TEXT,
      burned_at    TEXT,
      burn_reason  TEXT,
      notes        TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_probe_vantages_state ON probe_vantages(state)
    """,
    """
    CREATE TABLE IF NOT EXISTS probe_results (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      box_id        TEXT NOT NULL,
      vantage_id    TEXT NOT NULL,
      cycle_at      TEXT NOT NULL,
      check_type    TEXT NOT NULL CHECK (check_type IN (
                      'tls_fall_through','cover_domain_consistency','surface_scan',
                      'valid_path_liveness','latency_loss','behavioural_identity'
                    )),
      status        TEXT NOT NULL CHECK (status IN ('pass','soft_fail','hard_fail')),
      evidence_json TEXT,
      image_version TEXT NOT NULL,
      recorded_at   TEXT NOT NULL,
      FOREIGN KEY (box_id) REFERENCES ru_boxes(box_id),
      FOREIGN KEY (vantage_id) REFERENCES probe_vantages(vantage_id),
      FOREIGN KEY (image_version) REFERENCES ru_images(image_version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_probe_results_box_cycle
      ON probe_results(box_id, cycle_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_probe_results_vantage
      ON probe_results(vantage_id, cycle_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS image_profiles (
      image_version TEXT PRIMARY KEY REFERENCES ru_images(image_version),
      profile_json  TEXT NOT NULL,
      recorded_at   TEXT NOT NULL,
      recorded_by   TEXT NOT NULL,
      notes         TEXT
    )
    """,
    _TRIGGER_PROBE_VANTAGES_NO_RELABEL_BURNED,
    _TRIGGER_PROBE_VANTAGES_BURNED_NO_REVERT,
    _TRIGGER_PROBE_RESULTS_NO_UPDATE,
    _TRIGGER_PROBE_RESULTS_NO_DELETE,
    # --- spec J additions: observability alert log ---
    """
    CREATE TABLE IF NOT EXISTS alert_log (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      attempted_at  TEXT NOT NULL,
      delivered_at  TEXT,
      sink          TEXT NOT NULL,
      severity      TEXT NOT NULL CHECK (severity IN ('info','warn','crit','heartbeat')),
      kind          TEXT NOT NULL,
      target        TEXT,
      dedupe_key    TEXT NOT NULL,
      payload       TEXT NOT NULL,
      error         TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_alert_log_dedupe
      ON alert_log(dedupe_key, attempted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_alert_log_attempted
      ON alert_log(attempted_at DESC)
    """,
    _TRIGGER_ALERT_LOG_NO_UPDATE,
    _TRIGGER_ALERT_LOG_NO_DELETE,
    # --- spec K additions: user distribution channel ---
    """
    CREATE TABLE IF NOT EXISTS user_channels (
      user_id          TEXT PRIMARY KEY REFERENCES users(user_id),
      telegram_chat_id TEXT,
      email_addr       TEXT,
      registered_at    TEXT NOT NULL,
      updated_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS distribution_log (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id      TEXT NOT NULL REFERENCES users(user_id),
      channel      TEXT NOT NULL CHECK (channel IN ('telegram','email','dryrun')),
      kind         TEXT NOT NULL,
      attempted_at TEXT NOT NULL,
      delivered_at TEXT,
      subset_hash  TEXT,
      payload_json TEXT NOT NULL,
      error        TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_distribution_log_user_channel
      ON distribution_log(user_id, channel, attempted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_distribution_log_attempted
      ON distribution_log(attempted_at DESC)
    """,
    _TRIGGER_DISTRIBUTION_LOG_NO_UPDATE,
    _TRIGGER_DISTRIBUTION_LOG_NO_DELETE,
    # --- spec D2 additions: image canary + retired profile immutability ---
    _TRIGGER_IMAGE_PROFILES_NO_UPDATE_FOR_RETIRED,
    """
    CREATE INDEX IF NOT EXISTS ix_ru_boxes_canary
      ON ru_boxes(image_version) WHERE is_canary = 1
    """,
]

# Spec C migration triggers (applied by migrate_v2_to_v3)
_V3_MIGRATION_TRIGGERS: list[str] = [
    _TRIGGER_COVER_POOL_REJECT_BURNED,
    _TRIGGER_BURNED_DOMAINS_NO_DELETE,
]

# Spec B migration statements (applied by migrate_v1_to_v2)
_V2_MIGRATION: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS eu_exit_set (
      fingerprint  TEXT PRIMARY KEY,
      endpoint     TEXT NOT NULL,
      weight       INTEGER NOT NULL DEFAULT 1,
      added_at     TEXT NOT NULL,
      retired_at   TEXT
    )
    """,
    # ALTER TABLE is handled separately (may fail if column already exists)
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Idempotent v1 → v2 migration: add eu_exit_set and descriptor_history.signature."""
    for stmt in _V2_MIGRATION:
        conn.execute(stmt)
    # ALTER TABLE fails if column already exists — catch and ignore
    cols = [r[1] for r in conn.execute("PRAGMA table_info(descriptor_history)").fetchall()]
    if "signature" not in cols:
        conn.execute(
            "ALTER TABLE descriptor_history ADD COLUMN signature BLOB NOT NULL DEFAULT X''"
        )
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (2, _now()),
    )
    conn.commit()


def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Idempotent v2 → v3 migration: add entered_in_use_at + spec-C triggers."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cover_domain_pool)").fetchall()]
    if "entered_in_use_at" not in cols:
        conn.execute("ALTER TABLE cover_domain_pool ADD COLUMN entered_in_use_at TEXT")
    for stmt in _V3_MIGRATION_TRIGGERS:
        conn.execute(stmt)
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (3, _now()),
    )
    conn.commit()


def migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Idempotent v3 → v4 migration: add node_state + eu_nodes; seed node_state='active'."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS node_state (
          role                        TEXT NOT NULL CHECK (role IN ('active','standby')),
          promoted_at                 TEXT,
          previous_role               TEXT,
          promotion_case              TEXT CHECK (promotion_case IN ('A','B')),
          promotion_backup_generation INTEGER,
          CHECK (rowid = 1)
        );
        CREATE TABLE IF NOT EXISTS eu_nodes (
          node_id                TEXT PRIMARY KEY,
          hostname               TEXT NOT NULL,
          provider               TEXT NOT NULL,
          region                 TEXT NOT NULL,
          public_ip              TEXT,
          role                   TEXT NOT NULL CHECK (role IN ('active','standby','retired')),
          added_at               TEXT NOT NULL,
          promoted_at            TEXT,
          retired_at             TEXT,
          last_heartbeat_at      TEXT,
          last_heartbeat_b2_etag TEXT,
          notes                  TEXT
        );
        """
    )
    # Seed singleton if absent. Pre-spec-F deployments are implicitly 'active'.
    existing = conn.execute("SELECT COUNT(*) FROM node_state").fetchone()[0]
    if existing == 0:
        conn.execute("INSERT INTO node_state (rowid, role) VALUES (1, 'active')")
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (4, _now()),
    )
    conn.commit()


def migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Idempotent v4 → v5 migration: add ru_images table + state index."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ru_images (
          image_version       TEXT    PRIMARY KEY,
          upstream_release    TEXT    NOT NULL,
          upstream_repo       TEXT    NOT NULL,
          binary_url          TEXT    NOT NULL,
          manifest_url        TEXT    NOT NULL,
          binary_sha256       TEXT    NOT NULL,
          binary_size_bytes   INTEGER NOT NULL,
          state               TEXT    NOT NULL CHECK (state IN ('candidate','promoted','retired')),
          built_at            TEXT    NOT NULL,
          promoted_at         TEXT,
          retired_at          TEXT,
          notes               TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ru_images_state ON ru_images(state);
        """
    )
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (5, _now()),
    )
    conn.commit()


def migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Idempotent v5 → v6 migration: add ru_boxes.reality_uuid + partial unique idx;
    add eu_nodes cover_sni/reality_pubkey/data_exit_state/data_exit_started_at;
    add eu_exit_set cover_sni/reality_pubkey (folded in from Task 5)."""
    cols_ru = [r[1] for r in conn.execute("PRAGMA table_info(ru_boxes)").fetchall()]
    if "reality_uuid" not in cols_ru:
        conn.execute("ALTER TABLE ru_boxes ADD COLUMN reality_uuid TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ru_boxes_reality_uuid "
        "ON ru_boxes(reality_uuid) WHERE reality_uuid IS NOT NULL"
    )

    cols_eu = [r[1] for r in conn.execute("PRAGMA table_info(eu_nodes)").fetchall()]
    if "cover_sni" not in cols_eu:
        conn.execute("ALTER TABLE eu_nodes ADD COLUMN cover_sni TEXT")
    if "reality_pubkey" not in cols_eu:
        conn.execute("ALTER TABLE eu_nodes ADD COLUMN reality_pubkey TEXT")
    if "data_exit_state" not in cols_eu:
        conn.execute(
            "ALTER TABLE eu_nodes ADD COLUMN data_exit_state TEXT "
            "CHECK (data_exit_state IN ('healthy','degraded','stopped'))"
        )
    if "data_exit_started_at" not in cols_eu:
        conn.execute("ALTER TABLE eu_nodes ADD COLUMN data_exit_started_at TEXT")

    cols_ex = [r[1] for r in conn.execute("PRAGMA table_info(eu_exit_set)").fetchall()]
    if "cover_sni" not in cols_ex:
        conn.execute("ALTER TABLE eu_exit_set ADD COLUMN cover_sni TEXT")
    if "reality_pubkey" not in cols_ex:
        conn.execute("ALTER TABLE eu_exit_set ADD COLUMN reality_pubkey TEXT")

    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (6, _now()),
    )
    conn.commit()


def migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    """Idempotent v10 → v11 migration: ru_boxes.is_canary + partial index +
    image_profiles retired-immutability trigger."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ru_boxes)").fetchall()]
    if "is_canary" not in cols:
        conn.execute(
            "ALTER TABLE ru_boxes ADD COLUMN is_canary INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_ru_boxes_canary "
        "ON ru_boxes(image_version) WHERE is_canary = 1"
    )
    conn.execute(_TRIGGER_IMAGE_PROFILES_NO_UPDATE_FOR_RETIRED)
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (11, _now()),
    )
    conn.commit()


def migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """Idempotent v9 → v10 migration: add user_channels + distribution_log
    + two indexes + two triggers."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_channels (
          user_id          TEXT PRIMARY KEY REFERENCES users(user_id),
          telegram_chat_id TEXT,
          email_addr       TEXT,
          registered_at    TEXT NOT NULL,
          updated_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS distribution_log (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id      TEXT NOT NULL REFERENCES users(user_id),
          channel      TEXT NOT NULL CHECK (channel IN ('telegram','email','dryrun')),
          kind         TEXT NOT NULL,
          attempted_at TEXT NOT NULL,
          delivered_at TEXT,
          subset_hash  TEXT,
          payload_json TEXT NOT NULL,
          error        TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_distribution_log_user_channel
          ON distribution_log(user_id, channel, attempted_at DESC);
        CREATE INDEX IF NOT EXISTS ix_distribution_log_attempted
          ON distribution_log(attempted_at DESC);
        """
    )
    conn.execute(_TRIGGER_DISTRIBUTION_LOG_NO_UPDATE)
    conn.execute(_TRIGGER_DISTRIBUTION_LOG_NO_DELETE)
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (10, _now()),
    )
    conn.commit()


def migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    """Idempotent v8 → v9 migration: add alert_log + two indexes + two triggers."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alert_log (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          attempted_at  TEXT NOT NULL,
          delivered_at  TEXT,
          sink          TEXT NOT NULL,
          severity      TEXT NOT NULL CHECK (severity IN ('info','warn','crit','heartbeat')),
          kind          TEXT NOT NULL,
          target        TEXT,
          dedupe_key    TEXT NOT NULL,
          payload       TEXT NOT NULL,
          error         TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_alert_log_dedupe
          ON alert_log(dedupe_key, attempted_at DESC);
        CREATE INDEX IF NOT EXISTS ix_alert_log_attempted
          ON alert_log(attempted_at DESC);
        """
    )
    conn.execute(_TRIGGER_ALERT_LOG_NO_UPDATE)
    conn.execute(_TRIGGER_ALERT_LOG_NO_DELETE)
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (9, _now()),
    )
    conn.commit()


def migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """Idempotent v7 → v8 migration: add probe_vantages, probe_results,
    image_profiles tables + four triggers + two indexes."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS probe_vantages (
          vantage_id   TEXT PRIMARY KEY,
          label        TEXT UNIQUE NOT NULL,
          source_kind  TEXT NOT NULL,
          region_hint  TEXT,
          state        TEXT NOT NULL CHECK (state IN ('candidate','active','retired','burned')),
          added_at     TEXT NOT NULL,
          attested_at  TEXT,
          last_used_at TEXT,
          retired_at   TEXT,
          burned_at    TEXT,
          burn_reason  TEXT,
          notes        TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_probe_vantages_state ON probe_vantages(state);
        CREATE TABLE IF NOT EXISTS probe_results (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          box_id        TEXT NOT NULL,
          vantage_id    TEXT NOT NULL,
          cycle_at      TEXT NOT NULL,
          check_type    TEXT NOT NULL CHECK (check_type IN (
                          'tls_fall_through','cover_domain_consistency','surface_scan',
                          'valid_path_liveness','latency_loss','behavioural_identity'
                        )),
          status        TEXT NOT NULL CHECK (status IN ('pass','soft_fail','hard_fail')),
          evidence_json TEXT,
          image_version TEXT NOT NULL,
          recorded_at   TEXT NOT NULL,
          FOREIGN KEY (box_id) REFERENCES ru_boxes(box_id),
          FOREIGN KEY (vantage_id) REFERENCES probe_vantages(vantage_id),
          FOREIGN KEY (image_version) REFERENCES ru_images(image_version)
        );
        CREATE INDEX IF NOT EXISTS ix_probe_results_box_cycle
          ON probe_results(box_id, cycle_at DESC);
        CREATE INDEX IF NOT EXISTS ix_probe_results_vantage
          ON probe_results(vantage_id, cycle_at DESC);
        CREATE TABLE IF NOT EXISTS image_profiles (
          image_version TEXT PRIMARY KEY REFERENCES ru_images(image_version),
          profile_json  TEXT NOT NULL,
          recorded_at   TEXT NOT NULL,
          recorded_by   TEXT NOT NULL,
          notes         TEXT
        );
        """
    )
    conn.execute(_TRIGGER_PROBE_VANTAGES_NO_RELABEL_BURNED)
    conn.execute(_TRIGGER_PROBE_VANTAGES_BURNED_NO_REVERT)
    conn.execute(_TRIGGER_PROBE_RESULTS_NO_UPDATE)
    conn.execute(_TRIGGER_PROBE_RESULTS_NO_DELETE)
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (8, _now()),
    )
    conn.commit()


def migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Idempotent v6 → v7 migration: add shards.target_size + ru_boxes disjointness triggers."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(shards)").fetchall()]
    if "target_size" not in cols:
        conn.execute("ALTER TABLE shards ADD COLUMN target_size INTEGER")
    conn.execute(_TRIGGER_RU_BOXES_NO_CROSS_SHARD_REASSIGN)
    conn.execute(_TRIGGER_RU_BOXES_TERMINATED_KEEPS_SHARD)
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (7, _now()),
    )
    conn.commit()


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing; insert or migrate schema_version row."""
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    # Ensure descriptor_history.signature column exists (spec B)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(descriptor_history)").fetchall()]
    if "signature" not in cols:
        conn.execute(
            "ALTER TABLE descriptor_history ADD COLUMN signature BLOB NOT NULL DEFAULT X''"
        )
    existing = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, _now()),
        )
        # Spec F: fresh-install also seeds node_state as 'active' by default;
        # bootstrap may overwrite to 'standby' if --role standby is passed.
        n = conn.execute("SELECT COUNT(*) FROM node_state").fetchone()[0]
        if n == 0:
            conn.execute("INSERT INTO node_state (rowid, role) VALUES (1, 'active')")
    else:
        current = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
        if current < 2:
            migrate_v1_to_v2(conn)
        if current < 3:
            migrate_v2_to_v3(conn)
        if current < 4:
            migrate_v3_to_v4(conn)
        if current < 5:
            migrate_v4_to_v5(conn)
        if current < 6:
            migrate_v5_to_v6(conn)
        if current < 7:
            migrate_v6_to_v7(conn)
        if current < 8:
            migrate_v7_to_v8(conn)
        if current < 9:
            migrate_v8_to_v9(conn)
        if current < 10:
            migrate_v9_to_v10(conn)
        if current < 11:
            migrate_v10_to_v11(conn)
    conn.commit()
