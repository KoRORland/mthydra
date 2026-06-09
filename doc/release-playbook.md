# Release playbook

How to cut a mthydra release. The rule that overrides everything else here:

> **Never tag a release that has not booted a real RU box and brought the tunnel
> up in the integration harness.** Unit tests pass on code that still ships a
> broken RU-agent tarball (that exact bug — a missing top-level module in the
> agent closure — shipped green unit tests and would have bricked every new box;
> caught only by the integration harness on 2026-06-09).

## When to release

Per the release cadence: fixes and features land on `main` continuously and are
picked up via `mthydra-ops upgrade`. **Do not bump + tag for every fix.** Cut a
tagged version only when explicitly asked, or when a coherent unit of work is
validated and ready. Until then, CHANGELOG entries accrue under an
`Unreleased — <date>` block with no version number.

## The gate (run in order; stop on the first failure)

```bash
# 1. Lint — scope to the files you changed (local ruff is newer than the pin,
#    so a blanket `make lint` shows phantom errors on untouched files).
ruff check <changed files>

# 2. Unit + property suites, both packages — must be fully green.
make test
make test-monitor

# 3. HARD GATE — the real fleet. Boots EU controller + RU vantage + RU box in
#    containers, walks the MVP quickstart with the real CLI, and asserts the
#    mtg tunnel comes up on :443 and is reachable from the vantage.
make integration            # === bash harness/integration-mvp/run.sh ===
#    Require the final line:  ✅ TUNNEL UP
#    Anything else (❌, a Traceback, a non-zero exit) BLOCKS the release.
```

Only when all three are green do you proceed to tag:

```bash
# 4. Fill in the version header in CHANGELOG.md (Unreleased -> v<X.Y.Z>),
#    bump `version` in pyproject.toml, confirm each change is upgradable from
#    the previous version (schema backfills handled by `mthydra-ops upgrade`).
# 5. Commit, tag, push.
git commit -am "release: v<X.Y.Z>"
git tag v<X.Y.Z>
git push origin main --tags
```

## What `make integration` actually proves

It is the only check that exercises the cross-host artifact path end to end:

- the EU controller really `bootstrap`s, signs a descriptor, stages + promotes
  the mtg image, and `backup-now`s to S3 (MinIO);
- `ru-bringup` really **packages and publishes the RU-agent tarball** and the
  descriptor to S3 and presigns them — this is where agent-closure bugs surface;
- the RU box really downloads that tarball + the mtg ELF from S3, renders the
  mtg + sing-box configs, and brings the tunnel up on `:443`;
- the RU vantage really completes a TLS handshake against the box.

A green unit suite cannot see any of this — the tarball is assembled and
imported on a *different host* than the one that built it.

## Requirements / gotchas

- Needs Docker and an **amd64 host** (the mtg ELF is amd64).
- First run pulls `ubuntu:24.04`, `minio/minio`, `minio/mc`, sing-box, and the
  mtg release tarball (cached under `harness/agent-boot/.cache/`).
- On Fedora/SELinux hosts the harness uses `docker cp` (not bind mounts) to
  avoid relabeling; no host config needed.
- Containers are left running on success for inspection. Tear down with:
  `docker rm -f mt-minio mt-controller mt-vantage mt-rubox && docker network rm mtnet`.
- If the gate fails, read `docker logs mt-rubox` — the agent prints its full
  boot log + the verbose debug log (`/run/mthydra/debug/agent-debug.log`).

See `harness/integration-mvp/README.md` for the full topology and the
real-vs-stood-in breakdown.
