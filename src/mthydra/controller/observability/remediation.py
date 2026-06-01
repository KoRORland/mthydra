"""W-3: per-obligation remediation hints for heartbeat + alert bodies.

The operator should never have to leave the alert email/Telegram message
to know what to do. The body lists each overdue obligation + the one-line
operator action.

Per-target obligations (with `::`) match on prefix. Unknown obligations
fall back to a generic message — operator will recognize the id and look
it up in the runbook, but the alert never crashes for an unknown key.
"""
from __future__ import annotations

from collections.abc import Iterable


# Keyed by obligation_id prefix (for per-target ones) or whole id (singletons).
# Each value is the operator action — one short line, no shell wrapping.
_REMEDIATIONS: dict[str, str] = {
    # Backup + restore obligations.
    "backup_restore_dryrun": (
        "run a restore dry-run: mthydra-controller restore --from <gen> "
        "--identity ~/.age/operator.key --into /tmp/restored.sqlite --summary-only"
    ),
    "t2_dryrun_caseA": (
        "operator restore drill (runbook §10.1)"
    ),
    "t2_dryrun_caseB": (
        "standby promote drill (runbook §10.2)"
    ),
    "backup_integrity_proven": (
        "V-2 automatic weekly. Overdue = no recent backups OR sweep wedged. "
        "Force one: mthydra-controller backup-integrity-now"
    ),
    # Cover-domain obligations.
    "cover_pool_reverify_pass_proven": (
        "U-D1 automatic hourly. Overdue = no verified domains OR daemon wedged. "
        "Force: mthydra-controller cover-reverify-now"
    ),
    "cover_pool_replenishment_proven": (
        "add a fresh cover domain: mthydra-controller cover-add <domain> "
        "+ cover-attest-verified <domain> --vantage <ru-vps-id>"
    ),
    "t5_pool_revalidation": (
        "re-attest a verified cover domain: "
        "mthydra-controller cover-attest-verified <domain> --vantage <ru-vps-id>"
    ),
    # Probe obligations.
    "probe_coverage_proven": (
        "automatic via probe runner; overdue = all vantages unreachable for >2h. "
        "Check vantage SSH connectivity."
    ),
    "probe_vantage_rotation_proven": (
        "rotate a vantage: mthydra-controller vantage-burn <old> "
        "+ vantage-add <new> + vantage-attest-active <new>"
    ),
    "probe_audit_sweep_ran": (
        "internal sweep; overdue means controller is wedged — restart"
    ),
    # Shard obligations.
    "shard_reshuffle_proven": (
        "automatic via shard wheel; overdue = wheel wedged — check journalctl "
        "for the shard_reshuffle_sweep_ran heartbeat"
    ),
    "shard_disjointness_check_proven": (
        "automatic invariant check; overdue means startup-check is failing — "
        "run: mthydra-controller startup-check"
    ),
    # Distribution obligations.
    "dist_publish_sweep_ran": (
        "automatic via distribution publisher; overdue means publisher wedged — "
        "check journalctl for dist_publisher errors"
    ),
    # Credential rotation reminders (V-3, per-provider).
    "credential_rotation_proven": (
        "mint a new credential at the provider, then: "
        "mthydra-controller rotate-provider-credential <provider> "
        "--credential-file /tmp/.cred"
    ),
    # Image lifecycle.
    "t4_upstream_check": (
        "mthydra-controller upstream-check (or wait for the next tracker tick)"
    ),
    "t4_image_promoted": (
        "build + soak + promote: mthydra-ops image-prepare --yes"
    ),
    "t3_vantage_revalidation": (
        "re-attest the vantage: mthydra-controller vantage-attest-active <vantage> "
        "--evidence '<what you checked>'"
    ),
    "t3_profile_repin": (
        "rebuild the image with --profile-json pointing at a fresh capture"
    ),
    # Observability self.
    "obs_heartbeat_proven": (
        "if you're reading this, the heartbeat just landed — overdue means a "
        "PREVIOUS one was late; check obs_dead_mans_switch_breach for the SMTP smoke"
    ),
    "obs_alerter_sweep_ran": (
        "internal sweep; overdue means controller is wedged — restart"
    ),
    # EU node / standby.
    "eu_standby_drill_proven": (
        "run the standby promote drill (runbook §10.2)"
    ),
    "g_provision_drill_proven": (
        "provision a test RU box end-to-end and terminate it (runbook §5)"
    ),
    "e_ru_agent_provision_replace_drill_proven": (
        "RU agent provision-replace drill (runbook §11.4)"
    ),
    "e_data_exit_drill_proven": (
        "data-exit drill (runbook §11.5)"
    ),
    # Dormant readiness.
    "t1_dormant_health": (
        "(spec L not yet built; ignore until the dormant subsystem ships)"
    ),
    # Descriptor key.
    "descriptor_signing_key_rotation": (
        "annual rotation: mthydra-controller signing-key-rotate (runbook §11.7)"
    ),
}


def remediation_for(obligation_id: str) -> str | None:
    """Look up the operator remediation for an obligation id.

    Per-target obligations (containing `::`) match on prefix; singletons
    match exactly. Returns None when the id is unknown — caller decides
    whether to fall back to a generic line or omit the entry.
    """
    if obligation_id in _REMEDIATIONS:
        return _REMEDIATIONS[obligation_id]
    if "::" in obligation_id:
        prefix, _, _ = obligation_id.partition("::")
        if prefix in _REMEDIATIONS:
            return _REMEDIATIONS[prefix]
    return None


def format_overdue_block(overdue: Iterable, *, max_age_hint: bool = True) -> str:
    """Build the multi-line "overdue obligations" section for an alert
    body. Empty string when nothing is overdue — caller can use that to
    suppress the section header entirely.

    Each line: "  [sev] obligation_id  (overdue Nh)\\n    → remediation"
    """
    lines: list[str] = []
    for ob in overdue:
        sev = getattr(ob, "severity", "?")
        ob_id = getattr(ob, "obligation_id", "?")
        secs = int(getattr(ob, "overdue_seconds", 0) or 0)
        age = _human_age(secs) if max_age_hint and secs > 0 else ""
        remediation = remediation_for(ob_id) or (
            "(no known remediation; check the runbook by obligation id)"
        )
        head = f"  [{sev}] {ob_id}"
        if age:
            head += f"  (overdue {age})"
        lines.append(head)
        lines.append(f"    → {remediation}")
    return "\n".join(lines)


def _human_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
