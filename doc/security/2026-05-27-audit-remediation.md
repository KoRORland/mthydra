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
| M4/M5/M6/M10/M11/M12 | various | not in scope of this pass; not clearly-correct low-risk. | DEFER |
| L5/L6 | `backup-monitor/state.py` locking/umask | **FALSE POSITIVE.** No `state.py` exists. | NONE |
| L8 | `backup-monitor/Dockerfile` runs as root | **FALSE POSITIVE.** No Dockerfile exists. | NONE |
| L1–L4, L7 | misc | DEFER (low impact / N/A — no network server, so L7 rate-limiting N/A). | DEFER |

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
