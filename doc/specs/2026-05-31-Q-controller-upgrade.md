# Spec Q — `mthydra-ops upgrade` (one-command controller upgrade)

Status: **Draft, awaiting operator review.**
Predecessor: `doc/specs/2026-05-28-N-eu-host-installer.md` (installer; spec Q uses the same source/venv layout); `doc/specs/2026-05-30-P-eu-side-ru-automation.md` (image-prepare's GitHub release resolver — reused).
Successors blocked on this: none. Tagged release `v0.0.2` ships AFTER spec Q lands, so spec Q's first real-world use is to upgrade an existing EU host from current → `v0.0.2`.

---

## 1. Purpose

The installer (spec N) ran from a naked 24.04 host. The follow-on subsystems (specs O + P) added per-box and per-image automation. None of them address "the operator has a running controller and wants to upgrade to a newer version without reinstalling from scratch."

The current manual path is four+ steps (git pull → pip install → systemctl restart → eyeball health). Spec Q collapses it into `mthydra-ops upgrade` — one command, safe by default (pre-upgrade forced backup, auto-rollback on health-check failure), and idempotent on re-run.

This spec is the irreducible "stay current safely" loop. It does NOT solve multi-host orchestration or schema-rollback (the latter is structurally hard; the irreducible answer is "restore from the pre-upgrade backup").

Out of scope (deferred):
- Multi-host orchestration (operator runs `mthydra-ops upgrade` on standby first, then active — the runbook §10 promotion discipline is already tuned to that order).
- Downgrade across SCHEMA_VERSION boundaries (the only safe path is restore-from-backup + `adopt-restored-state`; spec Q surfaces the backup generation so that path is operator-driven, not silently lost).
- Auto-applying upgrades to standbys before the active.
- Anything that requires network coordination between hosts.

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-31.

| ID | Decision | Rationale |
|---|---|---|
| Q-D1 | **One command, single host.** `mthydra-ops upgrade` upgrades the host it runs on. No multi-host orchestration. | Multi-host coordination is its own subsystem (network discovery, ordered rollout, abort propagation). For a private fleet with one active + maybe one standby, the operator runs it twice — standby first, then active — and that's an honest, debuggable workflow. |
| Q-D2 | **8 phases, idempotent.** preflight → resolve-target → record-prior → fetch-and-checkout → pip-install → stop-service → start-and-verify → summary. Re-running after any partial failure picks up where it stopped. | Same pattern as the installer (spec N-D4). Each phase has a clear precondition; on re-run, completed phases are no-ops. |
| Q-D3 | **Default target = latest GitHub release tag.** `--ref <git-ref>` overrides explicitly; `--ref main` opts into rolling-from-main. | Tags-only by default makes upgrades explicit decisions — you must cut a release before it rolls out. Rolling from `main` is an operator escape hatch for development, not the default. |
| Q-D4 | **Forced pre-upgrade backup is the recovery floor.** Phase 3 (`record-prior`) calls `mthydra-controller backup-now`. The backup generation number is recorded in the audit log and printed in the summary; it is the documented restore target if anything irreversible breaks. | Schema migrations are forward-only (Q-D6) — if a new version migrates the DB and proves broken, source-rollback alone won't recover. The pre-upgrade backup IS the recovery story for that case. The recorded generation makes it findable. |
| Q-D5 | **Auto-rollback is the default.** If phase 7 (start-and-verify) fails, automatically `git reset --hard <prior_sha>` + `pip install -e .` + restart + verify again. `--no-auto-rollback` opts out (forensics mode). | Operators forget safety nets. Default-on means an upgrade that breaks the controller doesn't leave the fleet without a controller — it leaves it on the version that was working two minutes ago. The failure case where rollback ALSO fails verify (rare; usually a host-level issue) aborts with a clear "investigate by hand" message + the backup generation from Q-D4. |
| Q-D6 | **Schema migrations are forward-only and require explicit operator acknowledgement** (`--allow-schema-migration`). The upgrade phase 1 (preflight) inspects the target source's `SCHEMA_VERSION` constant; if it is greater than the DB's current version, refuse to proceed without the flag. | Crossing a schema migration boundary is not reversible by source-rollback alone — the new code migrates the DB in place. Auto-rollback can no longer restore service from source-rollback (the new DB will not load with the old code). Forcing the operator to acknowledge the boundary makes the irreversibility loud, not silent. The pre-upgrade backup (Q-D4) is still the recovery path; this gate ensures the operator KNOWS that before crossing. |
| Q-D7 | **No standby orchestration.** Spec Q does not connect to a standby host or coordinate ordering. The runbook tells operators to upgrade standby first, then active — that discipline carries forward. | Multi-host coordination is meaningfully its own design (handshake, abort propagation, version-skew safety). One-host upgrade ships now; ordered orchestration is a separate spec when there's real operator pull for it. |
| Q-D8 | **Rollback also runs verify; if rollback fails verify, abort.** No further automatic action — print the backup generation + the prior SHA + the failed verify output and exit non-zero. | A rollback that itself fails health-check means something is wrong with the host that the upgrade exposed (out-of-disk, broken systemd unit, sink credentials expired since prior boot — things the upgrade didn't cause but did surface). Repeatedly toggling source state can't help; the operator must investigate. |
| Q-D9 | **`pyproject.toml` version is the release marker, not the upgrade target.** The upgrade addresses a git ref; the pyproject version is informational metadata printed in the summary. | Tying upgrade decisions to a string in a TOML file would invite double-bookkeeping (tag the release AND bump the file AND remember to push both). The git tag is the source of truth. The pyproject version still gets bumped on each release for tooling that reads it. |

---

## 3. Components

### 3.1 `src/mthydra/ops/upgrade.py` (new module)

Public surface:
- `cmd_upgrade(args) -> int` — the wizard entry.

Internal helpers (each independently testable):
- `_resolve_target_ref(args, *, upstream_repo, github_api_url) -> str` — returns a git ref string (tag, branch, or SHA). Default: latest tag via `image_ops.resolve_latest_tag`. Override: `args.ref`.
- `_current_head_sha(src_dir: Path) -> str` — `git -C <src_dir> rev-parse HEAD`.
- `_pyproject_version(src_dir: Path) -> str` — parse `pyproject.toml` for `project.version`.
- `_record_prior(src_dir: Path, db_path: str, config_path: str) -> dict` — returns `{"prior_sha": ..., "prior_version": ..., "backup_generation": ...}`; runs `mthydra-controller backup-now` for the backup half.
- `_fetch_and_checkout(src_dir: Path, ref: str) -> None` — `git fetch origin <ref>` + `git reset --hard FETCH_HEAD`.
- `_pip_install(venv_dir: Path, src_dir: Path) -> None` — `<venv>/bin/pip install -e <src>`.
- `_stop_service(unit: str, *, timeout_s: int = 30) -> None` — `systemctl stop` + poll `is-active`.
- `_start_and_verify(unit: str, db_path: str, config_path: str, *, verify_timeout_s: int = 120) -> None` — `systemctl start` + poll `is-active`; then `mthydra-controller startup-check` and `obs-heartbeat-now`. Raises `VerifyFailed` on any failure.
- `_rollback_to(src_dir: Path, venv_dir: Path, prior_sha: str) -> None` — re-fetch + checkout + pip install. Caller runs verify again.
- `_schema_would_migrate(src_dir: Path, db_path: str) -> bool` — parse `SCHEMA_VERSION` constant from `<src_dir>/src/mthydra/controller/state/schema.py` (via AST so we don't import the new code into the running process); read DB's `schema_version` table; return target > current.
- `class VerifyFailed(RuntimeError)` — raised by `_start_and_verify`; caught by `cmd_upgrade` to trigger rollback.

### 3.2 `src/mthydra/ops/main.py` wiring

Lazy dispatch (mirrors existing pattern):
```python
def _dispatch_upgrade(args) -> int:
    from . import upgrade
    return upgrade.cmd_upgrade(args)
```
`_DISPATCH["upgrade"] = _dispatch_upgrade`; new subparser with flags below.

### 3.3 CLI

```bash
mthydra-ops upgrade [--ref <git-ref>]
                    [--no-auto-rollback]
                    [--allow-schema-migration]
                    [--src-dir /opt/mthydra/src]
                    [--venv-dir /opt/mthydra/venv]
                    [--unit mthydra-controller]
                    [--db-path …] [--config …]
                    [--upstream-repo KoRORland/mthydra]
                    [--github-api-url https://api.github.com]
                    [--verify-timeout 120]
                    [--non-interactive] [--verbose] [--quiet] [--dry-run]
```

---

## 4. Phase table

| # | Phase | Skip-if (idempotent) | Effect | On failure |
|---|---|---|---|---|
| 1 | `preflight` | always runs | root check; src is a git checkout; service unit exists; if `_schema_would_migrate` and `--allow-schema-migration` not set, REFUSE | exit non-zero, nothing changed |
| 2 | `resolve-target` | always runs | resolve `--ref`/latest-tag/`main` → concrete ref string; if target SHA == current HEAD SHA, log "already current" and exit 0 | exit non-zero |
| 3 | `record-prior` | always runs | capture current HEAD SHA + pyproject version + forced `backup-now` → state passed to later phases | exit non-zero (no source changes yet) |
| 4 | `fetch-and-checkout` | n/a (always advances HEAD to FETCH_HEAD) | `git fetch origin <ref>` + `git reset --hard FETCH_HEAD` | exit non-zero (source moved partially? `git reset` is atomic, so no — caller can `git reset --hard <prior_sha>` to recover) |
| 5 | `pip-install` | n/a | `pip install -e <src>` in venv | exit non-zero (venv state ambiguous — operator's call) |
| 6 | `stop-service` | service already stopped | `systemctl stop` + poll until inactive | exit non-zero (service not stopped — restart it manually) |
| 7 | `start-and-verify` | n/a — always runs the verify | `systemctl start`; poll `is-active`; run `startup-check`; run `obs-heartbeat-now`. **If anything fails AND `--auto-rollback` is on, run rollback (re-fetch prior SHA + pip + start + verify) and report rollback result.** | exit non-zero if rollback also fails (per Q-D8) |
| 8 | `summary` | always runs | print before/after SHAs + pyproject versions + backup gen + final status | n/a |

---

## 5. Audit + observability

- Phase 3 emits `audit_log` row `action=upgrade_started` with prior SHA + backup_generation in `details_json`.
- Phase 7 success → `action=upgrade_completed` with target SHA + duration.
- Phase 7 fail + rollback success → `action=upgrade_rolled_back` with target SHA + prior SHA + verify_output.
- Phase 8 always prints structured summary to stdout (and to the install-style log if `--verbose`).
- The forced `backup-now` from Q-D4 generates a normal backup audit row (no new schema).

---

## 6. Testing (TDD)

Tests live in `tests/unit/ops/test_upgrade.py`. Patterns:
- Each `_*` helper unit-tested with monkeypatched subprocess.
- `cmd_upgrade` end-to-end with monkeypatched git/pip/systemctl/controller calls; assert phase order, assert audit-log writes happen at the right points.
- Rollback test: `_start_and_verify` first-call raises `VerifyFailed`, second-call (rollback verify) succeeds → assert `git reset` to prior SHA AND `pip install` re-run AND service restarted.
- "Both verify and rollback-verify fail" test: assert exit non-zero, no further loops, the printed message contains the backup generation.
- Schema-migration refusal: build a fake "new src" where the new `SCHEMA_VERSION` is one higher than the DB's; without `--allow-schema-migration`, preflight exits non-zero. With the flag, proceeds.
- `--dry-run`: runs preflight + resolve-target, prints the plan, makes no system changes.
- Skip-no-op: target SHA == current HEAD → exit 0 with "already current" message; no backup taken.

Plus one regression test that `mthydra-ops upgrade --help` is reachable (smoke for the subparser wiring).

---

## 7. Out of scope (deliberately)

- Multi-host orchestration (Q-D7).
- Downgrade across SCHEMA_VERSION (the only safe answer is restore-from-backup; spec Q surfaces the backup generation, doesn't automate the restore).
- Upgrading the OS or apt-installed dependencies (re-run `mthydra-ops setup-host` separately for those).
- Auto-discovering a "current standby" and orchestrating it.
- Auto-applying releases via a daemon (the operator decides when to upgrade; no nightly auto-apply).
- Rolling-back across multiple versions (`--ref <SHA>` lets the operator pick any prior commit, but the upgrade flow itself is one-step-at-a-time).

---

*End of spec Q. After this lands, the first real test of the new command is the v0.0.2 release rollout — `mthydra-ops upgrade` on the existing EC2 host upgrades it from current → `v0.0.2`. That dogfoods spec Q via the act of using it.*
