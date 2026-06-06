from __future__ import annotations

from mthydra.controller.observability.remediation import remediation_for


def test_box_eu_tunnel_unseen_has_remediation():
    text = remediation_for("box_eu_tunnel_unseen::box-1")
    assert text is not None
    assert "tunnel" in text.lower() or "exit" in text.lower()


def test_unknown_obligation_returns_none():
    assert remediation_for("totally_unknown_obligation::x") is None
