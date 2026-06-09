# mthydra — Agent Guide

Resilient Telegram access controller. See `doc/design.md` for architecture.

## CLI entry points

- `mthydra-controller` — `src/mthydra/controller/__main__.py`
- `mthydra-ops` — `src/mthydra/ops/main.py`

## Test commands

```
make test          # controller tests (pytest tests/)
make test-monitor  # backup-monitor tests
make cov           # controller tests + coverage
make lint          # ruff on both packages
make integration   # RELEASE GATE: boot EU+vantage+RU-box fleet, assert tunnel up
```

Common test files:
- `tests/unit/controller/state/test_invariants.py`
- `tests/unit/controller/state/test_users_shards.py`
- `tests/unit/controller/state/test_obligations.py`
- `tests/unit/controller/state/test_config.py`
- `tests/unit/controller/test_cli.py`

## Conventions

- **Python ≥3.12**, ruff lint (line-length=100, target=py312, select=E/F/W/I/B/UP/SIM)
- **Production safety**: all state checks, backup triggers, heartbeat, alerter accept `mode="production" | "dryrun" | "offline"`
- **No secrets in git**: `.gitignore` excludes `.claude/`, `.opencode/`, `.ipynb`, `controller.toml`, `controller.env`, `backup-monitor.env`
- **Ed25519-only keys**: invariants #16 and #26 reject placeholder/PRIV-BOOTSTRAP keys in production mode
- **Two packages**: `mthydra` (controller) and `mthydra-backup-monitor` (separate host)
- **Systemd units** in `packaging/systemd/`, example configs in `packaging/etc/`

## Release conventions

See **`doc/release-playbook.md`** for the full gate. The hard rule: **never tag
a release that hasn't passed `make integration`** (boots a real RU box and
brings the tunnel up). Unit tests pass on code that still ships a broken
RU-agent tarball — only the integration harness exercises the cross-host
artifact path. Gate order: `ruff check <changed>` → `make test` +
`make test-monitor` → `make integration` (require `✅ TUNNEL UP`) → tag.

- **Do not assume a release.** Fixes and features land on `main` and are picked up via `mthydra-ops upgrade` (or `--ref <sha>`). Only create a version tag / CHANGELOG version header when explicitly instructed to cut a release.
- **CHANGELOG entries** are written for the *next* release block (version TBD) when work lands; the version number is filled in at release time.
- **Each version must be upgradable from the previous one.** If DB schema or on-disk state changes, `upgrade` must handle backfills automatically. Document any required operator action in the CHANGELOG entry.
- **Behavior changes** require corresponding doc updates (quickstart, runbook, or design doc as appropriate).

## Development workflow

This project uses [obra/superpowers](https://github.com/obra/superpowers) and [opencode-power-pack](https://github.com/waybarrios/opencode-power-pack) plugins. Skills are available via the `skill` tool. Preferred development cycle: spec/plan first, TDD, then implementation — full autonomy, no per-step prompts.

## Doc index

| File | Purpose |
|---|---|
| `doc/design.md` | Architecture overview |
| `doc/runbook.md` | Full operator reference |
| `doc/quickstart-mvp.md` | Install + day-2 routine |
| `doc/release-playbook.md` | Release gate — `make integration` before any tag |
| `harness/integration-mvp/` | The integration fleet harness itself |
| `doc/automation-roadmap.md` | Automation status and roadmap |
| `doc/specs/` | Artifact specifications |
| `doc/plans/` | Implementation plans |
| `doc/security/` | Security audit and remediation |
| `CHANGELOG.md` | Per-release operator-visible changes |
