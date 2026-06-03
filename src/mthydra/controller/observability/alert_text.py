"""Human-readable rendering for alert subjects + bodies (spec J §6 presentation).

Operators read these in Telegram and email. They must be plain language with a
clear "What to do" line — not raw obligation ids or database column names. The
machine identifiers (dedupe_key, kind) stay on the AlertPayload for routing and
dedupe; only the operator-visible subject + body are humanised here.

Unknown kinds/keys never crash and never leak a raw snake_case token — they
fall back to a de-snaked, sentence-cased form.
"""
from __future__ import annotations

import json

_SEVERITY_WORD = {
    "crit": "CRITICAL",
    "warn": "Warning",
    "info": "Info",
    "heartbeat": "Heartbeat",
}

# Curated, plain-language titles per alert kind. Anything not listed falls back
# to humanize_label() so a brand-new kind still reads as words, not a variable.
_KIND_TITLE: dict[str, str] = {
    "eu_heartbeat_stale": "EU node heartbeat is stale",
    "obligation_overdue": "Scheduled task is overdue",
    "cover_pool_rotation_frozen": "Cover-domain rotation is paused",
    "cover_pool_rotation_pending": "Cover domain is due for rotation",
    "probe_kill_pending": "RU box flagged for termination",
    "probe_evaluate_blocked": "Probe evaluation is blocked",
    "probe_coverage_pending": "Probe coverage gap",
    "probe_vantage_unreachable": "Probe vantage is unreachable",
    "cover_pool_reverify_drift_pending": "Cover domain failed re-verification",
    "shard_overdue_pending": "Shard is overdue for reshuffle",
    "shard_unassigned_pending": "Shard has no assigned box",
    "dist_user_unregistered": "Distribution user is not registered",
    "dist_user_heartbeat_breach": "Distribution user went silent",
    "image_rollback_pending": "RU box still running rolled-back image",
    "backup_integrity_failed": "Backup integrity check failed",
    "obs_dead_mans_switch_breach": "Heartbeat email is not going out",
}


def severity_word(severity: str) -> str:
    """Operator-facing severity label, e.g. 'crit' -> 'CRITICAL'."""
    return _SEVERITY_WORD.get(severity, severity.upper())


def humanize_label(token: str) -> str:
    """snake_case / kebab token -> 'Sentence case' words."""
    return token.replace("_", " ").replace("-", " ").strip().capitalize()


def kind_title(kind: str) -> str:
    """Plain-language title for an alert kind (curated, with de-snake fallback)."""
    return _KIND_TITLE.get(kind, humanize_label(kind))


def human_age(seconds: int | float | None) -> str:
    """Coarse human duration: '45 seconds', '2 minutes', '2 hours', '2 days'."""
    if seconds is None:
        return "unknown"
    s = int(seconds)
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''}"
    if s < 3600:
        n = s // 60
        return f"{n} minute{'s' if n != 1 else ''}"
    if s < 86400:
        n = s // 3600
        return f"{n} hour{'s' if n != 1 else ''}"
    n = s // 86400
    return f"{n} day{'s' if n != 1 else ''}"


def render_details(details_json: str | None) -> str:
    """Render a details JSON object as indented 'Label: value' lines.

    Falls back to the raw string for non-JSON, '' for None — never crashes.
    """
    if details_json is None:
        return ""
    try:
        data = json.loads(details_json)
    except (json.JSONDecodeError, TypeError):
        return f"  {details_json}"
    if isinstance(data, dict):
        return "\n".join(
            f"  {humanize_label(str(k))}: {v}" for k, v in data.items()
        )
    return f"  {details_json}"
