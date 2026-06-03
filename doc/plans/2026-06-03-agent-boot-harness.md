# Agent-boot Container Harness — Implementation Plan

> **For agentic workers:** implement task-by-task; steps use `- [ ]` checkboxes.

**Goal:** A `make agent-harness` target that runs the real RU agent boot end-to-end
in an amd64 container and asserts mtg + sing-box launch and mtg listens on :443,
plus a build-time ELF/arch guard in `build_image`.

**Architecture:** Host `run.sh` caches+extracts the real amd64 mtg, generates a
real seed via controller code, builds an `ubuntu:24.04` `--platform linux/amd64
--privileged` container that mirrors a TimeWeb RU box (sing-box, exec tmpfs,
journald + core_pattern + shutdown shims), runs `python3 -m mthydra.ru_agent`, and
polls for success.

**Tech Stack:** Python 3.12, Docker, bash, sing-box, mtg, pytest/ruff.

Spec: `doc/specs/2026-06-03-agent-boot-harness-design.md`.

---

### Task 1: build_image ELF/arch guard

**Files:**
- Modify: `src/mthydra/controller/image/builder.py`
- Test: `tests/unit/controller/image/test_builder.py`

- [ ] **Step 1: Write failing tests** (append to test_builder.py)

```python
def test_extract_runnable_rejects_non_elf(tmp_path):
    import io, tarfile
    from mthydra.controller.image.builder import _extract_runnable, BuildError
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"not an elf"
        info = tarfile.TarInfo("mtg-x/mtg"); info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    import pytest
    with pytest.raises(BuildError, match="not an ELF"):
        _extract_runnable(buf.getvalue(), "mtg-2.2.8-linux-amd64.tar.gz", member="mtg")


def test_extract_runnable_rejects_wrong_arch():
    import io, tarfile, struct
    from mthydra.controller.image.builder import _extract_runnable, BuildError
    # ELF header with e_machine = 0xB7 (aarch64) but asset says amd64
    elf = b"\x7fELF" + b"\x02\x01\x01" + b"\x00" * 9 + struct.pack("<HH", 2, 0xB7) + b"\x00" * 40
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("mtg-x/mtg"); info.size = len(elf)
        tf.addfile(info, io.BytesIO(elf))
    import pytest
    with pytest.raises(BuildError, match="arch"):
        _extract_runnable(buf.getvalue(), "mtg-2.2.8-linux-amd64.tar.gz", member="mtg")


def test_extract_runnable_accepts_matching_amd64():
    import io, tarfile, struct
    from mthydra.controller.image.builder import _extract_runnable
    elf = b"\x7fELF" + b"\x02\x01\x01" + b"\x00" * 9 + struct.pack("<HH", 2, 0x3E) + b"\x00" * 40
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("mtg-x/mtg"); info.size = len(elf)
        tf.addfile(info, io.BytesIO(elf))
    out = _extract_runnable(buf.getvalue(), "mtg-2.2.8-linux-amd64.tar.gz", member="mtg")
    assert out == elf
```

- [ ] **Step 2: Run, expect FAIL** — `python -m pytest tests/unit/controller/image/test_builder.py -q -k extract_runnable`

- [ ] **Step 3: Implement the guard** — add an ELF/arch check at the end of
`_extract_runnable` (after the bytes are resolved, before return):

```python
_ELF_E_MACHINE = {"amd64": 0x3E, "arm64": 0xB7, "armv7": 0x28,
                  "armv6": 0x28, "386": 0x03}

def _verify_elf(binary: bytes, asset_filename: str) -> None:
    if binary[:4] != b"\x7fELF":
        raise BuildError(
            f"extracted binary from {asset_filename!r} is not an ELF "
            f"(got magic {binary[:4]!r}) — wrong asset or extraction?")
    # e_machine: 2 bytes at offset 18, endianness from EI_DATA (offset 5: 1=LE).
    if len(binary) < 20:
        raise BuildError(f"{asset_filename!r} ELF too short to read e_machine")
    endian = "<" if binary[5] == 1 else ">"
    import struct as _struct
    (e_machine,) = _struct.unpack(endian + "H", binary[18:20])
    token = None
    for part in asset_filename.replace(".tar.gz", "").replace(".tgz", "").split("-"):
        if part in _ELF_E_MACHINE:
            token = part
            break
    if token is None and "linux" in asset_filename:
        # asset like mtg-2.2.8-linux-amd64: arch is the token after 'linux'
        segs = asset_filename.replace(".tar.gz", "").replace(".tgz", "").split("-")
        if "linux" in segs:
            cand = segs[segs.index("linux") + 1] if segs.index("linux") + 1 < len(segs) else None
            if cand in _ELF_E_MACHINE:
                token = cand
    if token is not None and e_machine != _ELF_E_MACHINE[token]:
        raise BuildError(
            f"arch mismatch for {asset_filename!r}: ELF e_machine={e_machine:#x} "
            f"does not match expected {token} ({_ELF_E_MACHINE[token]:#x})")
```

Call `_verify_elf(result, asset_filename)` before each `return` of the extracted
binary in `_extract_runnable` (tarball + bare-gz paths; the raw passthrough path
too — a raw asset should also be a valid ELF).

- [ ] **Step 4: Run, expect PASS** — `python -m pytest tests/unit/controller/image/test_builder.py -q` (all green)

- [ ] **Step 5: ruff + commit**

```bash
ruff check src/mthydra/controller/image/builder.py
git add src/mthydra/controller/image/builder.py tests/unit/controller/image/test_builder.py
git commit -m "feat(image-build): reject non-ELF / wrong-arch artifacts at build time"
```

---

### Task 2: seed generator (`make_seed.py`)

**Files:**
- Create: `harness/agent-boot/make_seed.py`

Writes a real `seed.json` to a path given as argv[1], with `image.url` and
`image.sha256` taken from argv[2] (mtg file path) and argv[3] (sha).

- [ ] **Step 1: Write `make_seed.py`** — reuse controller code against a temp DB:

```python
#!/usr/bin/env python3
"""Generate a real RU seed.json for the agent-boot harness.

Usage: make_seed.py <out_seed.json> <mtg_file_url> <mtg_sha256>
Seeds a throwaway DB with a real authority + signing key + signed descriptor
(carrying one EU exit so config_gen renders) + promoted image + verified cover,
runs provision_box, then rewrites image.url/sha256 to the local mtg file.
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state import cover_pool, eu_exit_set
from mthydra.controller.state.ru_images import insert_candidate, promote
from mthydra.descriptor.authority import generate_authority_keypair
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.sign import sign_new_descriptor
from mthydra.controller.provisioning.seed import provision_box

NOW = "2026-06-03T00:00:00Z"


def main() -> int:
    out_path, mtg_url, mtg_sha = sys.argv[1], sys.argv[2], sys.argv[3]
    db = Path(tempfile.mkdtemp()) / "harness.sqlite"
    conn = connect(str(db))
    apply_schema(conn)

    apriv, apub = generate_authority_keypair()
    insert_authority(conn, 1, apriv, apub, NOW)
    dpriv, dpub = generate_keypair()
    insert_signing_key(conn, 1, dpriv, dpub, NOW)
    # One EU exit so the signed descriptor carries an exit (dummy TEST-NET endpoint).
    eu_exit_set.add_exit(conn, "harness-fp", "192.0.2.1:443", 1, NOW,
                         cover_sni="www.cloudflare.com", reality_pubkey="HARNESSPUB")
    sign_new_descriptor(conn, now_iso=NOW, valid_until_iso="2026-06-04T00:00:00Z")
    insert_candidate(conn, image_version="harnessimg", upstream_release="v0.0.0",
                     upstream_repo="9seconds/mtg", binary_url="images/x/mtg",
                     manifest_url="images/x/manifest.json", binary_sha256="harnessimg",
                     binary_size_bytes=1, built_at=NOW)
    promote(conn, "harnessimg", at=NOW, evidence="harness")
    cover_pool.add_candidate(conn, "www.cloudflare.com", added_at=NOW)
    cover_pool.attest_verified(conn, "www.cloudflare.com", from_vantage="h", at=NOW)

    b2 = MagicMock()
    b2.presigned_image_url.return_value = (mtg_url, "2026-06-04T00:00:00Z")
    seed = provision_box(
        conn=conn, b2_destination=b2, provider="harness", region="local",
        image_signed_url_ttl_seconds=3600, now=NOW,
        descriptor_refresh_url="file:///dev/null",
        agent_source_url="file:///dev/null", agent_source_sha256="0" * 64,
        telegram_dcs_v4=("149.154.160.0/20",), telegram_dcs_v6=(),
    )
    payload = json.loads(seed.to_json())
    payload["image"]["url"] = mtg_url
    payload["image"]["sha256"] = mtg_sha
    Path(out_path).write_text(json.dumps(payload, indent=2))
    print(f"wrote {out_path} (sni={payload['sni']}, exits={len(payload.get('eu_exit_set', []))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke it on the host** — `python harness/agent-boot/make_seed.py /tmp/seed.json file:///opt/harness/mtg deadbeef && python -c "import json;d=json.load(open('/tmp/seed.json'));assert d['image']['url'].startswith('file://');print('ok')"`
Expected: `wrote ... ok`. (Confirms provision_box runs and the descriptor has an exit.)

- [ ] **Step 3: Commit** — `git add harness/agent-boot/make_seed.py && git commit -m "feat(harness): real seed generator for agent-boot harness"`

---

### Task 3: shims

**Files:**
- Create: `harness/agent-boot/shims/journalctl`
- Create: `harness/agent-boot/shims/shutdown`

- [ ] **Step 1: `journalctl` shim** (chmod +x)

```bash
#!/bin/sh
# Harness shim: simulate volatile journald so hardening._journald_volatile passes.
echo "File path: /run/log/journal/deadbeef/system.journal (in-memory volatile)"
```

- [ ] **Step 2: `shutdown` shim** (chmod +x)

```bash
#!/bin/sh
# Harness shim: a power-off attempt is a harness FAILURE, not a no-op.
echo "HARNESS-FAIL: agent invoked shutdown: $*" >&2
touch /run/mthydra/.shutdown-called 2>/dev/null || touch /tmp/.shutdown-called
exit 0
```

- [ ] **Step 3: Commit** — `chmod +x harness/agent-boot/shims/* && git add harness/agent-boot/shims && git commit -m "feat(harness): journalctl + shutdown shims"`

---

### Task 4: Dockerfile + entrypoint

**Files:**
- Create: `harness/agent-boot/Dockerfile`
- Create: `harness/agent-boot/entrypoint.sh`

- [ ] **Step 1: Dockerfile**

```dockerfile
FROM ubuntu:24.04
RUN apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-cryptography iptables curl ca-certificates iproute2 procps \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://sing-box.app/install.sh | sh
COPY shims/journalctl shims/shutdown /usr/local/bin/
RUN chmod +x /usr/local/bin/journalctl /usr/local/bin/shutdown
# agent code (ru_agent + descriptor) — mirrors package_agent closure
COPY _agentsrc/ /opt/agent/
COPY mtg /opt/harness/mtg
COPY seed.json /opt/harness/seed.json
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/harness/mtg
ENV PYTHONPATH=/opt/agent
ENTRYPOINT ["/entrypoint.sh"]
```

- [ ] **Step 2: entrypoint.sh**

```bash
#!/bin/bash
set -u
echo "[harness] host setup"
swapoff -a 2>/dev/null || true
mkdir -p /run/mthydra /var/log
mount -t tmpfs -o nosuid,nodev,mode=0700 tmpfs /run/mthydra
mount -t tmpfs tmpfs /var/log
# core_pattern is host-global; isolate it with a bind-mount.
printf '|/bin/false' > /tmp/core_pattern
mount --bind /tmp/core_pattern /proc/sys/kernel/core_pattern || true
cp /opt/harness/seed.json /run/mthydra/seed.json
chmod 0600 /run/mthydra/seed.json

echo "[harness] launching agent"
python3 -m mthydra.ru_agent >/var/log/agent.log 2>&1 &
AGENT_PID=$!

deadline=$((SECONDS + 45))
ok=""
while [ $SECONDS -lt $deadline ]; do
  if [ -f /run/mthydra/.shutdown-called ] || [ -f /tmp/.shutdown-called ]; then
    echo "[harness] FAIL: agent called shutdown"; sed -n '1,40p' /var/log/agent.log; exit 3
  fi
  if pgrep -x mtg >/dev/null && pgrep -x sing-box >/dev/null \
     && ss -tlnp 2>/dev/null | grep -q ':443 '; then
    ok=1; break
  fi
  sleep 2
done

echo "==== agent.log ===="; sed -n '1,80p' /var/log/agent.log
if [ -n "$ok" ]; then
  echo "[harness] PASS: mtg + sing-box up, mtg listening on :443"; exit 0
fi
echo "[harness] FAIL: boot did not reach steady state in time"; exit 1
```

- [ ] **Step 3: Commit** — `git add harness/agent-boot/Dockerfile harness/agent-boot/entrypoint.sh && git commit -m "feat(harness): Dockerfile + in-container entrypoint"`

---

### Task 5: run.sh + Makefile target

**Files:**
- Create: `harness/agent-boot/run.sh`
- Modify: `Makefile`
- Create/Modify: `.gitignore` (add `harness/agent-boot/.cache/` and build scratch)

- [ ] **Step 1: run.sh**

```bash
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
MTG_VER="${MTG_VER:-2.2.8}"
ARCH="linux-amd64"
CACHE="$HERE/.cache"; mkdir -p "$CACHE"
TARBALL="$CACHE/mtg-$MTG_VER-$ARCH.tar.gz"

[ "$(uname -m)" = "x86_64" ] || echo "[warn] host is $(uname -m); harness needs amd64"

if [ ! -f "$TARBALL" ]; then
  echo "[harness] downloading mtg $MTG_VER $ARCH"
  curl -fsSL -o "$TARBALL" \
    "https://github.com/9seconds/mtg/releases/download/v$MTG_VER/mtg-$MTG_VER-$ARCH.tar.gz"
fi

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
# extract the ELF via our own builder code; compute sha
PYTHONPATH="$REPO/src" python3 - "$TARBALL" "$BUILD/mtg" <<'PY'
import sys, hashlib, pathlib
from mthydra.controller.image.builder import _extract_runnable
data = pathlib.Path(sys.argv[1]).read_bytes()
elf = _extract_runnable(data, f"mtg-x-{'linux-amd64'}.tar.gz", member="mtg")
pathlib.Path(sys.argv[2]).write_bytes(elf)
print(hashlib.sha256(elf).hexdigest())
PY
MTG_SHA="$(PYTHONPATH="$REPO/src" python3 -c "import hashlib,sys;print(hashlib.sha256(open('$BUILD/mtg','rb').read()).hexdigest())")"

# generate the seed
PYTHONPATH="$REPO/src" python3 "$HERE/make_seed.py" "$BUILD/seed.json" \
  "file:///opt/harness/mtg" "$MTG_SHA"

# stage the agent source closure (ru_agent + descriptor + __init__)
mkdir -p "$BUILD/_agentsrc/mthydra"
cp "$REPO/src/mthydra/__init__.py" "$BUILD/_agentsrc/mthydra/"
cp -r "$REPO/src/mthydra/ru_agent" "$REPO/src/mthydra/descriptor" "$BUILD/_agentsrc/mthydra/"
find "$BUILD/_agentsrc" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$HERE/Dockerfile" "$HERE/entrypoint.sh" "$BUILD/"
cp -r "$HERE/shims" "$BUILD/"

echo "[harness] docker build"
docker build --platform linux/amd64 -t mthydra-agent-harness "$BUILD"
echo "[harness] docker run"
docker run --rm --platform linux/amd64 --privileged mthydra-agent-harness
```

- [ ] **Step 2: Makefile target** — append:

```makefile
.PHONY: agent-harness
agent-harness:  ## Run the full RU-agent boot in an amd64 container (needs Docker + amd64 host)
	bash harness/agent-boot/run.sh
```

- [ ] **Step 3: .gitignore** — add line `harness/agent-boot/.cache/`

- [ ] **Step 4: Commit** — `chmod +x harness/agent-boot/run.sh && git add harness/agent-boot/run.sh Makefile .gitignore && git commit -m "feat(harness): run.sh + make agent-harness target"`

---

### Task 6: End-to-end green

- [ ] **Step 1:** `make agent-harness` on an amd64 Docker host.
- [ ] **Step 2:** Expect final line `[harness] PASS: mtg + sing-box up, mtg listening on :443`.
- [ ] **Step 3:** If it fails, the printed `agent.log` shows the exact boot step (same diagnosis loop as a real box, but local + instant). Fix the agent code, re-run. Iterate to green.
- [ ] **Step 4:** Once green, commit any agent fixes the harness surfaced (separately, with their own TDD).

## Self-review notes
- Spec §4 components ↔ Tasks 2–5: covered. §6 ELF guard ↔ Task 1: covered.
- §5 success criteria ↔ Task 4 entrypoint poll: covered (shutdown/mtg/sing-box/:443).
- Host-global core_pattern (§4.3) ↔ Task 4 bind-mount: covered.
- The agent imports `mthydra.descriptor.authority`; Task 5 stages `descriptor` into
  the agent closure — matches `package_agent`.
