"""Spec G — atomic RU-box provisioning + seed bundle assembly.

provision_box() is the single atomic operation that:
  1. Picks a candidate_verified cover domain (oldest-first by added_at).
  2. Mints a box_id.
  3. Inserts ru_boxes row (state='provisioning').
  4. Calls cover_pool.assign_to_box (state -> in_use).
  5. Signs an Ed25519 onward credential.
  6. Inserts an onward_credentials row.
  All inside a single SQLite transaction.
  7. Mints a B2 presigned URL (post-commit; documented honest residual).
  8. Reads descriptor_signing_key trust anchors + latest descriptor.
  9. Returns a SeedBundle ready to render as JSON or cloud-init YAML.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import struct
import uuid
from dataclasses import dataclass

from mthydra.controller.state import authority as authority_repo
from mthydra.controller.state import cover_pool, ru_images
from mthydra.descriptor.authority import sign_onward_credential


class ProvisionError(RuntimeError):
    """Raised by provision_box when prerequisites are missing."""


_SEED_SCHEMA = "mthydra.ru_seed.v2"
_TRANSPORT_ROLE = "ru_relay"


@dataclass(frozen=True)
class SeedBundle:
    schema: str
    box_id: str
    sni: str
    transport_role: str
    reality_uuid: str
    onward_credential_b64: str
    authority_pubkey_pem: str
    descriptor_trust_anchors: tuple[str, ...]
    initial_descriptor_b64: str
    image: dict
    descriptor_refresh_url: str
    agent_source_url: str
    agent_source_sha256: str
    telegram_dcs: dict  # {"v4": [...], "v6": [...]}
    issued_at: str
    issued_by_authority_generation: int

    def _payload(self) -> dict:
        return {
            "schema": self.schema,
            "box_id": self.box_id,
            "sni": self.sni,
            "transport_role": self.transport_role,
            "reality_uuid": self.reality_uuid,
            "onward_credential": self.onward_credential_b64,
            "authority_pubkey_pem": self.authority_pubkey_pem,
            "descriptor_trust_anchors": list(self.descriptor_trust_anchors),
            "initial_descriptor": self.initial_descriptor_b64,
            "image": self.image,
            "descriptor_refresh_url": self.descriptor_refresh_url,
            "agent_source_url": self.agent_source_url,
            "agent_source_sha256": self.agent_source_sha256,
            "telegram_dcs": self.telegram_dcs,
            "issued_at": self.issued_at,
            "issued_by_authority_generation": self.issued_by_authority_generation,
        }

    def to_dict(self) -> dict:
        return self._payload()

    def to_json(self) -> bytes:
        """Canonical JSON (sorted keys, no whitespace)."""
        return json.dumps(
            self._payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def to_json_pretty(self) -> bytes:
        return json.dumps(self._payload(), sort_keys=True, indent=2).encode("utf-8")

    def to_cloud_init(self) -> bytes:
        """Returns a #cloud-config YAML wrapping the JSON in write_files: with
        the bootcmd hardening block, apt install of runtime deps, agent
        tarball download + sha256 verify, and systemd-run of the RU agent."""
        payload_indented = "\n".join(
            "      " + line for line in self.to_json_pretty().decode("utf-8").splitlines()
        )
        yaml = (
            "#cloud-config\n"
            "bootcmd:\n"
            "  - swapoff -a\n"
            "  - sysctl -w kernel.core_pattern='|/bin/false'\n"
            "  - mkdir -p /var/log /run/mthydra\n"
            "  - mount -t tmpfs tmpfs /var/log\n"
            "  - mkdir -p /etc/systemd/journald.conf.d\n"
            "  - printf '[Journal]\\nStorage=volatile\\n' > /etc/systemd/journald.conf.d/99-mthydra.conf\n"
            "  - systemctl restart systemd-journald\n"
            "write_files:\n"
            "  - path: /run/mthydra/seed.json\n"
            "    permissions: '0600'\n"
            "    owner: root:root\n"
            "    content: |\n"
            f"{payload_indented}\n"
            "runcmd:\n"
            "  - chmod 0700 /run/mthydra\n"
            "  - DEBIAN_FRONTEND=noninteractive apt-get update -y\n"
            "  - DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-cryptography iptables\n"
            f"  - curl -fsSL '{self.agent_source_url}' -o /run/mthydra/agent.tar.gz\n"
            f"  - echo '{self.agent_source_sha256}  /run/mthydra/agent.tar.gz' | sha256sum -c -\n"
            "  - mkdir -p /run/mthydra/agent\n"
            "  - tar -xzf /run/mthydra/agent.tar.gz -C /run/mthydra/agent\n"
            "  - systemd-run --unit mthydra-agent --description='mthydra RU agent' python3 -m mthydra.ru_agent\n"
        )
        return yaml.encode("utf-8")


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
) -> SeedBundle:
    # 1. Authority must be real Ed25519 (not placeholder).
    try:
        auth = authority_repo.current_authority(conn)
    except LookupError as e:
        raise ProvisionError(f"no active credential_authority: {e}") from e
    if auth.privkey_pem.startswith("PRIV-BOOTSTRAP-"):
        raise ProvisionError(
            "authority is still a placeholder; "
            "run mthydra-controller authority-migrate-placeholder first"
        )

    # 2. A promoted image must exist.
    image = ru_images.current_promoted(conn)
    if image is None:
        raise ProvisionError(
            "no promoted ru_image; run mthydra-controller image-promote first"
        )

    # 3. Pick a candidate_verified cover domain (oldest-first by added_at).
    candidates = cover_pool.list_by_state(conn, "candidate_verified")
    if not candidates:
        raise ProvisionError(
            "no candidate_verified cover_domain available; "
            "run mthydra-controller cover-add + cover-attest-verified first"
        )
    candidates_sorted = sorted(candidates, key=lambda c: (c.added_at, c.domain))
    picked = candidates_sorted[0]

    # 4. There must be at least one signed descriptor.
    desc_row = conn.execute(
        "SELECT payload, signature FROM descriptor_history "
        "ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if desc_row is None:
        raise ProvisionError(
            "no signed descriptor in descriptor_history; "
            "run mthydra-controller descriptor-sign-now first"
        )
    desc_payload_text, desc_sig = desc_row[0], bytes(desc_row[1])

    # 5. Collect descriptor trust anchors (current + outgoing).
    pubkey_rows = conn.execute(
        "SELECT pubkey FROM descriptor_signing_key WHERE retired_at IS NULL"
    ).fetchall()
    if not pubkey_rows:
        raise ProvisionError("no non-retired descriptor_signing_key rows")
    trust_anchors_b64 = tuple(
        base64.b64encode(bytes(r[0])).decode("ascii") for r in pubkey_rows
    )

    # 6. Reconstruct the descriptor wire format (length-prefixed JSON + sig).
    payload_bytes = desc_payload_text.encode("utf-8")
    descriptor_blob = struct.pack(">H", len(payload_bytes)) + payload_bytes + desc_sig
    initial_descriptor_b64 = base64.b64encode(descriptor_blob).decode("ascii")

    # 7. Atomic transaction — inlined SQL to avoid repo helpers' conn.commit() calls
    # breaking our transaction boundary.  All DML is in a single BEGIN/COMMIT block.
    box_id = str(uuid.uuid4())
    cred_id = str(uuid.uuid4())
    reality_uuid = str(uuid.uuid4())
    credential_blob = sign_onward_credential(
        auth.privkey_pem,
        box_id=box_id,
        issued_at=now,
        authority_generation=auth.generation,
    )
    audit_details = json.dumps({
        "sni": picked.domain,
        "image_version": image.image_version,
        "authority_generation": auth.generation,
    }, separators=(",", ":"))
    try:
        conn.execute("BEGIN")
        # ru_boxes insert (state='provisioning')
        conn.execute(
            "INSERT INTO ru_boxes "
            "(box_id, provider, region, public_ip, sni, state, image_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'provisioning', ?, ?)",
            (box_id, provider, region, None, picked.domain, image.image_version, now),
        )
        # Write reality_uuid (Spec E: per-box Reality UUID, embedded in seed).
        conn.execute(
            "UPDATE ru_boxes SET reality_uuid=? WHERE box_id=?",
            (reality_uuid, box_id),
        )
        # cover_domain_pool: candidate_verified → in_use
        cur = conn.execute(
            "UPDATE cover_domain_pool SET state='in_use', assigned_box_id=?, "
            "entered_in_use_at=? WHERE domain=? AND state='candidate_verified'",
            (box_id, now, picked.domain),
        )
        if cur.rowcount == 0:
            raise ProvisionError(
                f"cover domain {picked.domain!r} was no longer candidate_verified"
            )
        # onward_credentials insert
        conn.execute(
            "INSERT INTO onward_credentials "
            "(cred_id, box_id, credential, issued_at, authority_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            (cred_id, box_id, credential_blob, now, auth.generation),
        )
        # audit_log
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, target, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, actor, "box_provisioned", box_id, audit_details),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # 8. Mint signed B2 URL (post-commit).
    image_url, image_url_expires_at = b2_destination.presigned_image_url(
        image_version=image.image_version,
        ttl_seconds=image_signed_url_ttl_seconds,
    )

    return SeedBundle(
        schema=_SEED_SCHEMA,
        box_id=box_id,
        sni=picked.domain,
        transport_role=_TRANSPORT_ROLE,
        reality_uuid=reality_uuid,
        onward_credential_b64=base64.b64encode(credential_blob).decode("ascii"),
        authority_pubkey_pem=auth.pubkey_pem,
        descriptor_trust_anchors=trust_anchors_b64,
        initial_descriptor_b64=initial_descriptor_b64,
        image={
            "version": image.image_version,
            "url": image_url,
            "url_expires_at": image_url_expires_at,
            "sha256": image.binary_sha256,
            "size_bytes": image.binary_size_bytes,
        },
        descriptor_refresh_url=descriptor_refresh_url,
        agent_source_url=agent_source_url,
        agent_source_sha256=agent_source_sha256,
        telegram_dcs={
            "v4": list(telegram_dcs_v4),
            "v6": list(telegram_dcs_v6),
        },
        issued_at=now,
        issued_by_authority_generation=auth.generation,
    )
