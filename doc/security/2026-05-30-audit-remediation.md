# Security Audit Remediation — 2026-05-30

Tracks remediation of `~/Downloads/report (2).md` (third-pass external static
audit of mthydra at HEAD `382aa03`). **Every finding was re-verified against
the actual source before acting** — as with the 2026-05-27 pass, several
claims were inaccurate or already fixed. Verdicts and the action column
reflect the verified state.

Audit headline counts: 0 critical, 2 high, 12 medium, 9 low. After this pass:
**7 findings fixed**, **6 already-fixed/false-positives**, **10 re-confirmed
accepts** (mostly carried forward from 2026-05-27).

## Verdicts

| ID | Report claim | Verified verdict | Action | Commit |
|----|--------------|------------------|--------|--------|
| H5 | APScheduler CVE-2026-31072 (CRIT 9.8, unpatched) | **NOT EXPLOITABLE.** Report itself notes "Not exploitable with default SQLAlchemyJobStore" — we don't even use a job store (the controller's wheels are bare `BlockingScheduler` ticks, no persistence layer). The CVE number was also "fabricated" in the 2026-05-27 audit per that pass's verdict; no patched version exists upstream. | NONE | — |
| M11 | `log_event()` outside transaction — "systemic across `burned.py`, `ru_images.py`, `cover_pool.py`" | **PARTIAL.** `burned.py` is the only real case: it does explicit `BEGIN…COMMIT` then calls `log_event()` after the COMMIT, opening a crash window where the burn is durable but the audit row is lost. `ru_images.py` and `cover_pool.py` do NOT pre-commit — their DML INSERT + `log_event`'s audit INSERT land in the same implicit transaction, committed atomically by `log_event`'s own `commit()` (the trailing `conn.commit()` is a redundant no-op). | **FIX** burned.py only | `f12ce26` |
| M2 | Bare `except Exception: pass` in `triggers.py:100-107`, `scheduler.py:79-81` | **PARTIAL.** triggers.py was fixed in the 2026-05-27 pass (now logs). scheduler.py occurrences are intentional belt-and-braces in scheduler-callback edges where re-raising would tear down the whole scheduler thread; the wheel design tolerates dropped ticks (state re-derives next tick). Adding logging there is reasonable but low value. | NONE (re-confirm 2026-05-27 verdict) | — |
| M3 | No TLS cert pinning on `urlopen` calls | **PARTIAL ACCEPT.** System CA store + TLS is the defended model; HPKP-style pinning is out of scope (and would force fleet-wide rollouts on every cert rotation). | NONE (re-confirm) | — |
| M4 | Error message leaks `box_id` in `seed.py:102-105` | **FALSE POSITIVE (still).** Lines 102-105 are cloud-init YAML template literals, not an error message. Same verdict as 2026-05-27. | NONE | — |
| M5 | Three separate DB connections in backup `pipeline.py` | **NOT A BUG (re-confirm).** The 3 short txns (`started → pushed → index_updated`) are intentional because `put_blob` / `put_index` are multi-second network uploads and a single SQLite write txn cannot be held across them without locking the DB for every wheel. Partial-failure rows are exactly what `abandon_zombie_starts` (startup) + `reconcile_pending` clean up. | NONE (re-confirm) | — |
| M6 | Lost-update race on S3 `index.json` | **BOUNDED/ACCEPTED (re-confirm).** Requires concurrent writers = split-brain (two active controllers), which the standby-promotion discipline forbids. Within one active node, gen is monotonic and `reconcile_pending` never downgrades. ETag-conditional PUT is also of uncertain support on the B2 S3-compat endpoint. | NONE (re-confirm) | — |
| M10 | Unlocked module-level `_mirror_path` in `audit.py` | **NOT A BUG (re-confirm).** `set_audit_mirror` is called once at serve startup *before* any wheel thread arms; after that `_mirror_path` is read-only and reads are GIL-atomic. Concurrent mirror writes use `open(..., "a")` (O_APPEND), atomic at line granularity. | NONE (re-confirm) | — |
| M12 | `_cmd_serve` skips startup checks in `ops/main.py` | **FALSE POSITIVE.** There is no `cmd_serve` in `src/mthydra/ops/main.py` (`serve` is a `mthydra-controller` subcommand, not a `mthydra-ops` one). The previous M12 fix landed in the controller and stands. Auditor pointed at the wrong file. | NONE | — |
| M13 | Path traversal in `ru_bringup.py:293-309` via `--release` | **REAL (defense-in-depth).** `args.release` is operator-controlled CLI input that lands directly in a filename. Not an external attack vector, but a typo like `--release ../../tmp` would silently create a file outside `CYCLE_STATE_DIR`. Whitelist to `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` (covers all real mtg upstream tag shapes). | **FIX** | `6aa6474` |
| M14 | Unsplittable argv forwarding in `scripts/install.sh:67` | **INTENTIONAL (documented).** The `$FWD` word-splitting is the only way to pass through arbitrary forwarded args from a POSIX `sh` wrapper; line is marked `# shellcheck disable=SC2086 — intentional word-splitting of forwarded args`. The wrapper holds no secrets and is callable only as root. | NONE | — |
| M15 | Non-atomic write of downloaded mtg binary, `ru_agent/binary.py:47` | **REAL.** `out.write_bytes(content)` leaves a partial file on power cut; supervisor would then try to exec a truncated binary on next boot. Switch to tempfile + fsync + chmod + atomic rename + directory fsync (same shape L4's adopt.py fix already uses). | **FIX** | `027de84` |
| M16 | Non-atomic write of Reality private key, `cli.py:2827` | **REAL.** `Path(...).write_text(priv + "\n")` leaves a partial private key on crash. Atomic tempfile pattern; chmod 0600 BEFORE the rename so the final file never appears with default mode. | **FIX** | `3493fb6` |
| M17 | `write_atomic` in `data_exit/config_writer.py:83-99` missing fsync | **REAL.** `os.replace` alone is not crash-durable: the rename can be in the page cache while file contents are not, so a power cut after rename leaves a (renamed) empty/partial file. Add `f.flush() + os.fsync(f.fileno())` before the rename and `os.fsync(dir_fd)` after — matches the L4 adopt.py pattern. | **FIX** | `1144361` |
| L2 | Substring CIDR match in iptables `verify.py` | **ALREADY FIXED (2026-05-27, `d754e73`).** Current code does exact token match: `toks[i+1] == cidr` (line 61). Auditor missed the prior fix. | NONE | — |
| L3 | TOCTOU symlink race in `adopt.py` | **ACCEPTED (re-confirm).** Operator-only, daemon-down, non-concurrent. The `exists()` checks are friendly errors, not a security boundary. | NONE | — |
| L4 | No fsync after adopt file ops | **ALREADY FIXED (2026-05-27, `ff152e6`).** `_fsync_path` helper in `restore/adopt.py:18` fsyncs the live DB + parent directory after rename. Auditor missed the prior fix. | NONE | — |
| L5 | No file lock on `backup-monitor` state | **N/A (re-confirm).** Single-instance daemon (systemd unit); no concurrent writers. | NONE | — |
| L6 | Default umask on state file (no secrets) | **ACCEPTED (re-confirm).** Defense-in-depth; the file holds no secrets. | NONE | — |
| L7 | No rate limiting on `seed.py`, `descriptor_refresh.py` | **N/A (re-confirm).** Controller has no network server; B2 pull is anonymous static-object GET against B2's own quotas. | NONE | — |
| L8 | Docker runs as root in `Dockerfile` | **FALSE POSITIVE (re-confirm).** No `Dockerfile` exists in the repo. | NONE | — |
| L9 | `parse_cohort` crash on malformed input (`ValueError` when no `=` in kv pair) | **ACCEPTED.** `ValueError` IS the correct failure for malformed CLI input from a trusted operator; bubbles up with the offending string in the message. A clearer pre-validation message would be cosmetic only. | NONE | — |
| L10 | Systemd template injection via config in `install.py:410-433` | **REAL (defense-in-depth).** `install.py` builds systemd unit bodies via f-strings interpolating operator paths (`venv_dir`, `db_path`, `config_path`). Installer runs as root so this isn't an external attack vector, but a newline in any of those would inject lines into `[Service]`. New `_systemd_safe_path()` refuses `\n`/`\r`/`\x00`. | **FIX** | `8757f6c` |
| L18 | `restore/decrypt.py:26` lacks subprocess timeout | **REAL.** `age_crypt.encrypt_file` uses `timeout=300`; the decrypt side had no timeout. A wedged `age` process would indefinitely block an operator-side restore. Match the 300-second timeout + map `subprocess.TimeoutExpired` to `DecryptError`. | **FIX** | `0d0d399` |

## Outcome

**7 commits (`f12ce26` → `8757f6c`), all on `origin/main`:**

| Finding | Commit | Subject |
|---|---|---|
| M11 (burned.py only) | `f12ce26` | fold cover_burned audit into the burn transaction |
| M15 | `027de84` | atomic write of downloaded mtg binary (crash-safe) |
| M16 | `3493fb6` | atomic write of Reality private key in data-exit-reality-keygen |
| M17 | `1144361` | fsync data-exit config_writer.write_atomic (file + parent dir) |
| L18 | `0d0d399` | timeout=300 on restore/decrypt subprocess (match age_crypt) |
| M13 | `6aa6474` | sanitize --release before using it in state-file path |
| L10 | `8757f6c` | refuse newline/NUL in systemd-unit interpolated paths |

**Test surface:** every fix landed with passing tests in its scope; the touched
files all stayed ruff-clean (the repo's pre-existing lint debt in
`controller/` / `ru_agent/` tests is unrelated and out of scope per the
2026-05-27 audit's outcome).

## Auditor accuracy notes (third pass)

The third-pass report claimed 27 findings; 7 were real and actionable (those
fixed above), 3 were already-fixed in earlier passes (**L2**, **L4**, plus
the controller-side **M12** fix from 2026-05-27), 4 were false positives
(**M4** location, **M12** file, **L5** non-existence, **L8** non-existence),
1 was partially correct (**M11** real for 1 file of 3 claimed), and the rest
were re-confirmed accepts from the prior pass. The pattern from the
2026-05-27 audit (loose verification of file/line references; some claims
fabricated from training-data shape rather than the actual source) persists.
**Continue to re-verify every claim against the actual code before acting.**
