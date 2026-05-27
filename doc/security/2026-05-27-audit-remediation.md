# Security Audit Remediation — 2026-05-27

Tracks remediation of `~/Downloads/report.md` (external static audit of mthydra
v0.0.1). **Every finding was re-verified against the actual source before
acting** — the report's line numbers and several mechanisms were inaccurate or
fabricated. Verdicts and the plan below reflect the *verified* state of the code.

## Verdicts

| ID | Report claim | Verified verdict | Action |
|----|--------------|------------------|--------|
| C1 | `descriptor_refresh.py:74-121` `resp.status==304` → empty body → hash mismatch → terminate | **REAL, wrong mechanism.** No `status==304` branch exists. Real path: `_fetch_b2` sends `If-Modified-Since`; a 304 makes `urllib.urlopen` **raise HTTPError**, caught by `tick()`'s `except Exception` → `failure_count++` → 24 ticks → `terminate_fn`. Steady-state (descriptor unchanged) self-terminates the fleet. | **FIX** |
| C2 | `ops/main.py:129` `shell=True` injection via `--provider`/`--region` | **FALSE as RCE.** `cmd_setup_host` has no such flags; the shell strings are static literals — no user input reaches the shell. `shell=True` only used for `&&`/`\|\|`. | **HARDEN** (list-form), downgrade severity |
| H1 | `emailer.py:25` `SMTP_SSL` missing `context=` | **REAL, broader.** `SMTP_SSL(context=None)` *and* `SMTP.starttls(context=None)` both resolve to `ssl._create_stdlib_context()` = no cert/hostname check. Affects emailer.py + both controller sinks. | **FIX** (all 3 sites) |
| H2 | `config.py:1` docstring "lives in git" misleads re: secrets | **REAL.** Docstring says "Non-secret … lives in git" but `ops bootstrap` writes SMTP passwords / bot tokens into the TOML. | **FIX** (docstring + .gitignore) |
| H3 | `ops/main.py:315` B2 secret on CLI | **REAL.** `--provider-credential b2=KEY:SECRET` passed as subprocess argv → visible in `ps aux`. | **FIX** (env passthrough) |
| H4 | `age_crypt.py:18` weak recipient validation | **REAL.** Only `startswith("age1")` + `len>=32`; no bech32 checksum → a typo'd recipient silently yields undecryptable backups. | **FIX** (bech32 checksum) |
| H5 | Loose dep floors / CVEs | **PARTLY REAL.** Floors `cryptography>=41`, `APScheduler>=3.10` are loose (older releases have real CVEs). The cited `CVE-2026-31072` looks fabricated; not relied upon. Installed venv already on safe 48.0.0 / 3.11.2. | **FIX** (raise real floors) |
| M1 | `ru_agent/__main__` race on config path; fix = `threading.Lock` | **REAL bug, wrong fix.** Single in-process writer (refresh thread) but `write_bytes` is non-atomic and sing-box reads it as a **separate process** — a lock cannot protect that. | **FIX** (atomic `os.replace`) |
| M2 | bare `except Exception: pass` in timer callbacks | **REAL** (`backup/triggers.py:100-107`). | **FIX** (log) |
| M7 | no timeout on age subprocess | **REAL** (`age_crypt.encrypt_file`). | **FIX** (generous timeout) |
| M9 | `_require_positive` accepts 0 | **REAL** (`value < 0`). | **FIX** (`positive=` kwarg for must-be-≥1) |
| M8 | `shutdown(wait=False)` abandons jobs | **ACCEPTED.** 14 wheels; ticks are short + idempotent and re-derive state next boot; `wait=True` risks shutdown hangs. | **WON'T FIX** (documented) |
| M3 | no TLS cert pinning on urlopen sites | **PARTIAL.** Verifying contexts added via H1; full HPKP pinning out of scope. | partial |
| M4 | `seed.py:102-105` error leaks box_id | **FALSE POSITIVE.** Those lines are cloud-init YAML, not a credential error; no box_id leak there. | NONE |
| M11 | `burned.py:log_event` outside txn | **FALSE POSITIVE.** burned.py has no `log_event`; audit logging lives in state/audit.py. | NONE |
| M12 | `_cmd_serve` skips startup checks | **REAL.** Active serve armed all wheels with no validation. | **FIX** (gate on local startup checks) |
| L1 | bool accepted as int | **REAL.** Fixed alongside M9 (bool rejected in `_require_positive`). | **FIXED** (in M9 commit) |
| M5 | 3 DB connections under one lock → orphan zombie rows | **NOT A BUG.** The 3 short transactions (started→pushed→index_updated) are *intentional*: `put_blob`/`put_index` are multi-second network uploads and a single SQLite write txn cannot be held across them without locking the DB for every wheel. Partial-failure rows are exactly what `abandon_zombie_starts` (startup) + `reconcile_pending` clean up. The report's single-conn+savepoint fix is infeasible. | NONE |
| M6 | S3 index.json lost-update (no ETag CAS) | **BOUNDED/ACCEPTED.** Requires concurrent writers = split-brain (two active controllers), which the standby-promotion discipline forbids. Within one active node, gen is monotonic and `reconcile_pending` never downgrades the index (`>= our gen` guard). ETag-conditional PUT is also of uncertain support on the B2 S3-compat endpoint. | NONE |
| M8 | `shutdown(wait=False)` abandons jobs | **ACCEPTED.** 14 wheels; ticks are short + idempotent and re-derive state next boot; `wait=True` risks shutdown hangs. | NONE |
| M10 | unlocked module-level `_mirror_path` in state/audit.py | **NOT A BUG.** `set_audit_mirror` is called once at serve startup *before* any wheel thread arms; after that `_mirror_path` is read-only and reads are GIL-atomic. The concurrent mirror writes use `open(..., "a")` (O_APPEND), atomic at line granularity for the short audit lines. No data race. | NONE |
| L2 | substring CIDR match in iptables verify | **REAL.** `cidr not in out` / `str(port) not in out` substring-matched (prefix CIDR, port-as-substring, cross-rule). | **FIXED** (token-exact, same-rule) |
| L3 | adopt.py TOCTOU between `exists()` and move | **ACCEPTED.** `adopt_restored_state` runs operator-only, daemon-down, non-concurrent; the `exists()` checks produce friendly errors, they are not a security boundary. No realistic race. | NONE |
| L4 | no fsync after adopt | **REAL.** Rename + DB commit weren't fsynced to the directory. | **FIXED** (fsync live file + parent dir) |
| L5/L6 | `backup-monitor/state.py` locking/umask | **FALSE POSITIVE.** No `state.py` exists. | NONE |
| L8 | `backup-monitor/Dockerfile` runs as root | **FALSE POSITIVE.** No Dockerfile exists. | NONE |
| L7 | no rate limiting on seed/refresh | **N/A.** Controller has no network server; B2 pull is anonymous static-object GET. | NONE |

## Outcome

Every report finding now has a resolution. **Fixed:** all Critical + High
(C1, H1–H5), C2 hardened (downgraded — not actually injectable), Mediums
M1/M2/M7/M9/M12, Lows L1/L2/L4. **Verified non-bug / accepted with rationale:**
M5 (intentional incremental commits + zombie reconciliation), M6 (bounded by
single-active + monotonic gen), M8 (idempotent short ticks), M10 (set-once
before threads; O_APPEND), L3 (operator-only, daemon-down). **False positives:**
C2-as-RCE, M4, M11, L5, L6, L8. **N/A:** L7 (no network server).

Notably the report's *fixes* were wrong twice — M1 (a threading.Lock cannot
protect a separate-process reader; used atomic os.replace) and C1 (described a
`status==304` branch that doesn't exist; real path was urllib raising HTTPError
on 304) — and its suggested M5 fix (single txn across network I/O) is infeasible.

Full suite green throughout (1041+ passing).

Postscript: a `git add -A` during this work swept local dev files
(`.opencode/config.json` with a live token, `.ipynb`) into the first commit;
caught in review, purged from history via `git filter-repo`, force-pushed.
Because the secret was already pushed, it must be rotated out-of-band
regardless of the history rewrite.

## Fix plan (commit-per-step, TDD where logic changes)

1. **C1** — `descriptor_refresh`: make not-modified a first-class success.
   `_fetch_b2` catches HTTPError 304 and returns a `NOT_MODIFIED` sentinel;
   `tick()` treats it as success (`failure_count = 0`). Test: 304 loop never
   terminates.
2. **H1** — explicit `ssl.create_default_context()` for emailer `SMTP_SSL`
   and both controller sinks' `starttls(context=...)`.
3. **H3** — `ops bootstrap`: pass B2 app key to `init` via env
   (`MTHYDRA_PROVIDER_CREDENTIAL`-style) instead of argv.
4. **H4** — `age_crypt`: bech32 decode + checksum validation. Test: good key
   passes, mutated-checksum key rejected.
5. **H2** — rewrite `config.py` docstring; add `controller.toml` to `.gitignore`.
6. **H5** — bump floors: `cryptography>=44.0.1`, `APScheduler>=3.11`.
7. **M1** — atomic config writes in `ru_agent/__main__` (write tmp + `os.replace`).
8. **M7** — `encrypt_file`: `timeout=300` (generous; avoids killing large-DB encryption).
9. **M2** — `backup/triggers`: log the swallowed exception.
10. **M9** — `_require_positive(positive=False)`; pass `positive=True` for sizes/counts/intervals.
11. **C2** — `setup-host`: replace `shell=True` with list-form per-command calls.
