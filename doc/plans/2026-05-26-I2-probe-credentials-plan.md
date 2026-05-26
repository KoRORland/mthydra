# Spec I2 — Plan

1. Schema v13 → v14: `probe_credentials` table + UNIQUE-on-active partial index.
2. `state/probe_credentials.py`: issue/revoke/list helpers.
3. CLI: `probe-credential-issue`, `probe-credential-list`, `probe-credential-revoke`.
4. Tests.
