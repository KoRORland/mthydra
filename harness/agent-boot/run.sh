#!/bin/bash
# make agent-harness — run the real RU-agent boot end-to-end in an amd64 container.
# Needs Docker + (ideally) an amd64 host. See doc/specs/2026-06-03-agent-boot-harness-design.md
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
MTG_VER="${MTG_VER:-2.2.8}"
ARCH="linux-amd64"
CACHE="$HERE/.cache"; mkdir -p "$CACHE"
TARBALL="$CACHE/mtg-$MTG_VER-$ARCH.tar.gz"

command -v docker >/dev/null || { echo "[harness] docker not found"; exit 2; }
[ "$(uname -m)" = "x86_64" ] || \
    echo "[harness] warn: host is $(uname -m); the harness needs an amd64 target"

if [ ! -f "$TARBALL" ]; then
    echo "[harness] downloading mtg $MTG_VER $ARCH"
    curl -fsSL -o "$TARBALL" \
        "https://github.com/9seconds/mtg/releases/download/v$MTG_VER/mtg-$MTG_VER-$ARCH.tar.gz"
fi

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

# Extract the ELF via our own builder code (exercises _extract_runnable + the
# ELF/arch guard); compute its sha.
MTG_SHA="$(PYTHONPATH="$REPO/src" python3 - "$TARBALL" "$BUILD/mtg" <<'PY'
import hashlib
import pathlib
import sys
from mthydra.controller.image.builder import _extract_runnable
data = pathlib.Path(sys.argv[1]).read_bytes()
elf = _extract_runnable(data, "mtg-x-linux-amd64.tar.gz", member="mtg")
pathlib.Path(sys.argv[2]).write_bytes(elf)
print(hashlib.sha256(elf).hexdigest())
PY
)"
echo "[harness] mtg ELF extracted (sha=${MTG_SHA:0:12}…)"

# Generate a real seed pointing image.url at the file the container serves.
PYTHONPATH="$REPO/src" python3 "$HERE/make_seed.py" \
    "$BUILD/seed.json" "file:///opt/harness/mtg" "$MTG_SHA"

# Stage the agent source closure (mirrors package_agent: ru_agent + descriptor).
mkdir -p "$BUILD/_agentsrc/mthydra"
cp "$REPO/src/mthydra/__init__.py" "$BUILD/_agentsrc/mthydra/"
cp -r "$REPO/src/mthydra/ru_agent" "$REPO/src/mthydra/descriptor" "$BUILD/_agentsrc/mthydra/"
find "$BUILD/_agentsrc" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp "$HERE/Dockerfile" "$HERE/entrypoint.sh" "$BUILD/"
cp -r "$HERE/shims" "$BUILD/"

echo "[harness] docker build"
docker build --platform linux/amd64 -t mthydra-agent-harness "$BUILD"
echo "[harness] docker run"
docker run --rm --platform linux/amd64 --privileged mthydra-agent-harness
