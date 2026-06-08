#!/usr/bin/env python3
"""Extract the inlined /run/mthydra/seed.json out of a cloud-init bundle.

ru-bringup writes a #cloud-config YAML with the seed JSON inlined under
write_files: as a 6-space-indented `content: |` block. We don't want a YAML
dep, and the format is fixed, so pull the block out by hand.

Usage: extract_seed.py <cloud-init.yaml> <out_seed.json>
"""
import sys
from pathlib import Path


def main() -> int:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    lines = src.read_text().splitlines()
    collecting = False
    body: list[str] = []
    for line in lines:
        if not collecting:
            if line.strip() == "content: |":
                collecting = True
            continue
        # The block ends at the first line that isn't part of the 6-space indent
        # (e.g. "runcmd:" at column 0, or the next write_files entry).
        if line and not line.startswith("      "):
            break
        body.append(line[6:])
    if not body:
        print("extract_seed: no inlined seed.json found", file=sys.stderr)
        return 1
    out.write_text("\n".join(body) + "\n")
    print(f"extract_seed: wrote {out} ({len(body)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
