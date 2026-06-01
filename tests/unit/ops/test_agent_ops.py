"""Tests for mthydra.ops.agent_ops — package + publish ru_agent."""
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch

from mthydra.ops import agent_ops


def test_package_agent_includes_ru_agent_and_init(tmp_path):
    src = tmp_path / "src"
    (src / "mthydra" / "ru_agent").mkdir(parents=True)
    (src / "mthydra" / "__init__.py").write_text("# mthydra root pkg\n")
    (src / "mthydra" / "ru_agent" / "__init__.py").write_text("")
    (src / "mthydra" / "ru_agent" / "__main__.py").write_text("# agent main\n")
    (src / "mthydra" / "ru_agent" / "__pycache__").mkdir()
    (src / "mthydra" / "ru_agent" / "__pycache__" / "x.pyc").write_bytes(b"x")
    (src / "mthydra" / "ru_agent" / "stale.pyc").write_bytes(b"y")

    tar_bytes, sha = agent_ops.package_agent(src)
    assert len(sha) == 64
    assert sha == hashlib.sha256(tar_bytes).hexdigest()
    with tarfile.open(fileobj=BytesIO(tar_bytes), mode="r:gz") as tf:
        names = sorted(m.name for m in tf.getmembers())
    assert "mthydra/__init__.py" in names
    assert "mthydra/ru_agent/__init__.py" in names
    assert "mthydra/ru_agent/__main__.py" in names
    assert not any("__pycache__" in n for n in names)
    assert not any(n.endswith(".pyc") for n in names)


def test_package_agent_is_deterministic(tmp_path):
    src = tmp_path / "src"
    (src / "mthydra" / "ru_agent").mkdir(parents=True)
    (src / "mthydra" / "__init__.py").write_text("hi\n")
    (src / "mthydra" / "ru_agent" / "__init__.py").write_text("")
    t1, s1 = agent_ops.package_agent(src)
    t2, s2 = agent_ops.package_agent(src)
    assert s1 == s2


class _FakeS3Client:
    def __init__(self):
        self.put_calls = []
        self.presign_calls = []

    def put_object(self, **kw):
        self.put_calls.append(kw)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presign_calls.append((op, Params, ExpiresIn))
        return f"https://fake.example/{Params['Key']}?sig=stub"


class _FakeCfg:
    class backup:
        endpoint = "https://s3.eu-west-1.amazonaws.com"
        bucket = "mthydra-prod"
        region = "eu-west-1"


def test_publish_agent_uploads_and_writes_manifest(monkeypatch, tmp_path):
    fake = _FakeS3Client()
    monkeypatch.setattr(agent_ops, "_make_s3_client", lambda cfg, db_path: fake)
    monkeypatch.setattr(agent_ops, "_get_s3_credentials",
                        lambda cfg, db_path: ("AKIA", "SECRET"))
    monkeypatch.setattr(agent_ops, "AGENT_MANIFEST_PATH", tmp_path / "agent.json")

    m = agent_ops.publish_agent(_FakeCfg(), tar_bytes=b"hello",
                                sha="0123456789abcdef" * 4,
                                db_path=str(tmp_path / "stub.sqlite"),
                                ttl_days=7)
    assert m.url.startswith("https://fake.example/agent/")
    assert m.sha256 == "0123456789abcdef" * 4
    assert "agent/mthydra-ru-agent-0123456789ab.tar.gz" in fake.put_calls[0]["Key"]
    assert fake.presign_calls[0][2] == 7 * 86400
    on_disk = json.loads((tmp_path / "agent.json").read_text())
    assert on_disk["sha256"] == m.sha256


def test_publish_agent_skips_when_manifest_fresh_and_sha_matches(monkeypatch, tmp_path):
    sha = "abc" + "0" * 61
    manifest_path = tmp_path / "agent.json"
    now = datetime.now(UTC)
    manifest_path.write_text(json.dumps({
        "url": "https://existing.example/agent.tar.gz",
        "sha256": sha,
        "published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    monkeypatch.setattr(agent_ops, "AGENT_MANIFEST_PATH", manifest_path)
    fake = _FakeS3Client()
    monkeypatch.setattr(agent_ops, "_make_s3_client",
                        lambda cfg, db_path: (_ for _ in ()).throw(
                            AssertionError("should not call S3")))
    m = agent_ops.publish_agent(_FakeCfg(), tar_bytes=b"x", sha=sha,
                                db_path=str(tmp_path / "stub.sqlite"), ttl_days=7)
    assert m.url == "https://existing.example/agent.tar.gz"
    assert fake.put_calls == []


def test_cmd_agent_publish_tars_uploads_and_prints_manifest(monkeypatch, tmp_path):
    src = tmp_path / "src"
    (src / "mthydra" / "ru_agent").mkdir(parents=True)
    (src / "mthydra" / "__init__.py").write_text("")
    (src / "mthydra" / "ru_agent" / "__init__.py").write_text("")

    monkeypatch.setattr(agent_ops, "AGENT_MANIFEST_PATH",
                        tmp_path / "agent.json")

    # Patch load_config at the module level where cmd_agent_publish imports it.
    import mthydra.controller.config as _cfg_mod
    monkeypatch.setattr(_cfg_mod, "load_config", lambda path: _FakeCfg())

    captured = {"sha": None, "db_path": None}
    def _fake_publish(cfg, tar_bytes, sha, db_path, *, ttl_days, bucket=None):
        captured["sha"] = sha
        captured["db_path"] = db_path
        return agent_ops.AgentManifest(
            url="https://fake/x", sha256=sha,
            published_at="2026-05-30T00:00:00Z",
            expires_at="2026-06-06T00:00:00Z")
    monkeypatch.setattr(agent_ops, "publish_agent", _fake_publish)

    args = argparse.Namespace(
        ttl_days=7, source_dir=str(src),
        db_path=str(tmp_path / "x.sqlite"),
        config=str(tmp_path / "c.toml"),
        verbose=False, quiet=True,
    )
    rc = agent_ops.cmd_agent_publish(args)
    assert rc == 0
    assert captured["sha"]
    assert captured["db_path"] == str(tmp_path / "x.sqlite")


# ---------------------------------------------------------------------------
# Regression: FrozenInstanceError (T-1)
# ---------------------------------------------------------------------------


def test_publish_agent_no_frozen_instance_error(monkeypatch, tmp_path):
    """publish_agent must not raise FrozenInstanceError.

    The original _load_cfg tried `cfg._db_path = db_path` on a frozen
    dataclass.  The fix threads db_path explicitly so no mutation is needed.
    """
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.tokens import set_provider_credential

    # Set up a real in-memory SQLite DB with the full schema and a seeded
    # B2 credential.
    db_path = str(tmp_path / "state.sqlite")
    with connect(db_path) as conn:
        apply_schema(conn)
        set_provider_credential(conn, "b2", "KEYID:SECRET", "2026-01-01T00:00:00Z")

    # Patch out the boto3 client so no real network calls happen.
    fake_client = MagicMock()
    fake_client.generate_presigned_url.return_value = "https://fake.example/agent.tar.gz?sig=x"

    monkeypatch.setattr(agent_ops, "AGENT_MANIFEST_PATH", tmp_path / "agent.json")

    with patch("boto3.client", return_value=fake_client):
        # This must not raise dataclasses.FrozenInstanceError.
        manifest = agent_ops.publish_agent(
            _FakeCfg(),
            b"fake-tar-data",
            "aabbccdd" * 8,
            db_path,
            ttl_days=7,
        )

    assert manifest.sha256 == "aabbccdd" * 8
    assert fake_client.put_object.called
    assert fake_client.generate_presigned_url.called


# ---------------------------------------------------------------------------
# Credential parsing (R-D1 follow-up; agent_ops was the second consumer that
# didn't get the split-or-fallback fix and broke when operator credentials
# were stored secret-only)
# ---------------------------------------------------------------------------


def _cfg_with_key_id(key_id: str | None):
    from types import SimpleNamespace
    return SimpleNamespace(backup=SimpleNamespace(access_key_id=key_id))


def test_get_s3_credentials_splits_keyid_secret_form(monkeypatch, tmp_path):
    """Canonical install-time format: stored credential is 'KEY:SECRET'."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.tokens import set_provider_credential

    db = tmp_path / "s.sqlite"
    c = connect(db)
    apply_schema(c)
    set_provider_credential(
        c, provider="b2", credential="AKIAEXAMPLE:realsecret",
        at="2026-06-02T00:00:00Z")
    c.close()

    key_id, secret = agent_ops._get_s3_credentials(
        _cfg_with_key_id("fallback-key-id"), str(db))
    assert key_id == "AKIAEXAMPLE"
    assert secret == "realsecret"


def test_get_s3_credentials_uses_config_keyid_when_secret_only(monkeypatch, tmp_path):
    """Regression: agent-publish broke with 'provider credential malformed'
    on hosts where the operator had stored just the secret (R-D1 workaround
    flow). agent_ops now mirrors the split-or-fallback logic in
    controller.cli._build_destination.

    Discovered 2026-06-02 on the user's prod host: their credential was
    rotated to secret-only earlier in the session and agent-publish
    refused with 'expected KEY:SECRET'."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.tokens import set_provider_credential

    db = tmp_path / "s.sqlite"
    c = connect(db)
    apply_schema(c)
    set_provider_credential(
        c, provider="b2", credential="just-the-secret-no-colon",
        at="2026-06-02T00:00:00Z")
    c.close()

    key_id, secret = agent_ops._get_s3_credentials(
        _cfg_with_key_id("AKIAFROMCONFIG"), str(db))
    assert key_id == "AKIAFROMCONFIG"
    assert secret == "just-the-secret-no-colon"


def test_get_s3_credentials_raises_on_empty_secret(monkeypatch, tmp_path):
    """Defensive: 'KEY:' (key with empty secret) is malformed."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.tokens import set_provider_credential
    import pytest

    db = tmp_path / "s.sqlite"
    c = connect(db)
    apply_schema(c)
    set_provider_credential(
        c, provider="b2", credential="AKIA:", at="2026-06-02T00:00:00Z")
    c.close()
    with pytest.raises(RuntimeError, match="empty secret"):
        agent_ops._get_s3_credentials(_cfg_with_key_id("x"), str(db))


def test_get_s3_credentials_raises_when_secret_only_and_no_config_keyid(monkeypatch, tmp_path):
    """Defensive: secret-only credential AND empty config.backup.access_key_id
    leaves us with nothing to use as the AWS access key id — fail clearly."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.tokens import set_provider_credential
    import pytest

    db = tmp_path / "s.sqlite"
    c = connect(db)
    apply_schema(c)
    set_provider_credential(
        c, provider="b2", credential="secret-only",
        at="2026-06-02T00:00:00Z")
    c.close()
    with pytest.raises(RuntimeError, match="access_key_id is unset"):
        agent_ops._get_s3_credentials(_cfg_with_key_id(""), str(db))
