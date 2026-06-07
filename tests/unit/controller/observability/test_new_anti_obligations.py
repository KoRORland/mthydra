from __future__ import annotations

from mthydra.controller.observability.remediation import remediation_for
from mthydra.controller.observability.snapshot import _ANTI_PREFIXES, _classify_obligation


def test_prefixes_registered():
    assert "tls_fingerprint_stale" in _ANTI_PREFIXES
    assert "eu_exit_handshake_degraded" in _ANTI_PREFIXES


def test_classify_per_target():
    assert _classify_obligation("tls_fingerprint_stale::chrome") == (
        "tls_fingerprint_stale", "per_target", "chrome",
    )
    assert _classify_obligation("eu_exit_handshake_degraded::eu-node-1") == (
        "eu_exit_handshake_degraded", "per_target", "eu-node-1",
    )


def test_remediation_present():
    assert remediation_for("tls_fingerprint_stale::chrome")
    assert remediation_for("eu_exit_handshake_degraded::eu-node-1")
