from __future__ import annotations

import hashlib
import json
import tarfile
from datetime import UTC, datetime, timedelta
from io import BytesIO

from mthydra.ops import agent_ops


def test_package_agent_includes_ru_agent_and_init(tmp_path):
    src = tmp_path / "src"
    (src / "mthydra" / "ru_agent").mkdir(parents=True)
    (src / "mthydra" / "__init__.py").write_text("# mthydra root pkg\n")
    (src / "mthydra" / "ru_agent" / "__init__.py").write_text("")
    (src / "mthydra" / "ru_agent" / "__main__.py").write_text("# agent main\n")
    # Junk that MUST be excluded:
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
    monkeypatch.setattr(agent_ops, "_make_s3_client", lambda cfg: fake)
    monkeypatch.setattr(agent_ops, "_get_s3_credentials",
                        lambda cfg: ("AKIA", "SECRET"))
    monkeypatch.setattr(agent_ops, "AGENT_MANIFEST_PATH",
                        tmp_path / "agent.json")

    m = agent_ops.publish_agent(_FakeCfg(), tar_bytes=b"hello",
                                sha="0123456789abcdef" * 4, ttl_days=7)
    assert m.url.startswith("https://fake.example/agent/")
    assert m.sha256 == "0123456789abcdef" * 4
    assert "agent/mthydra-ru-agent-0123456789ab.tar.gz" in fake.put_calls[0]["Key"]
    assert fake.presign_calls[0][2] == 7 * 86400
    on_disk = json.loads((tmp_path / "agent.json").read_text())
    assert on_disk["sha256"] == m.sha256


def test_publish_agent_skips_when_manifest_fresh_and_sha_matches(
        monkeypatch, tmp_path):
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

    def _boom_s3(cfg):
        raise AssertionError("should not call S3")

    monkeypatch.setattr(agent_ops, "_make_s3_client", _boom_s3)
    m = agent_ops.publish_agent(_FakeCfg(), tar_bytes=b"x",
                                sha=sha, ttl_days=7)
    assert m.url == "https://existing.example/agent.tar.gz"
