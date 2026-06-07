"""Spec V §5.2 — flag deployed uTLS fingerprints whose probe-captured JA3 no
longer matches the operator-maintained current-browser reference set.

Pure logic + a tolerant JSON loader. The reference set is manual ops data
(same maintenance class as [data_exit.telegram_dcs]); a missing/empty file
yields no findings rather than crashing the snapshot."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StaleFinding:
    fingerprint: str
    observed_ja3: str


def load_reference_set(path: Path | str) -> dict[str, set[str]]:
    """Load {fp: {ja3, ...}} from a JSON file. Missing/invalid -> {}."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
        if not isinstance(raw, dict):
            return {}
        out: dict[str, set[str]] = {}
        for fp, ja3s in raw.items():
            if isinstance(ja3s, (list, set, tuple)):
                out[str(fp)] = {str(j) for j in ja3s}
        return out
    except (json.JSONDecodeError, OSError, AttributeError, TypeError, ValueError):
        return {}


def evaluate_fingerprint_staleness(
    observed_ja3_by_fp: dict[str, str | None],
    reference: dict[str, set[str]],
) -> list[StaleFinding]:
    """A deployed fingerprint is STALE if we captured a JA3 for it and that JA3
    is not among the reference JA3s for that fingerprint (including the case
    where the reference knows nothing about that fingerprint at all).

    No captured JA3 (None) -> skipped (the handshake-health signal covers that
    failure mode separately)."""
    findings: list[StaleFinding] = []
    for fp, observed in sorted(observed_ja3_by_fp.items()):
        if observed is None:
            continue
        known = reference.get(fp, set())
        if observed not in known:
            findings.append(StaleFinding(fingerprint=fp, observed_ja3=observed))
    return findings
