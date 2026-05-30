from __future__ import annotations

import hashlib
import tarfile
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
