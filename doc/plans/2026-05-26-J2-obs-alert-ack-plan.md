# Spec J2 — Plan

1. Schema v12 → v13: `alert_acks` table + triggers (no-update; no-delete with compactor sentinel relief).
2. `state/alert_acks.py` with `ack`, `is_acked`, `list_active`, `list_all`.
3. Alerter integration: skip dispatch when `is_acked`.
4. CLI: `obs-alert-ack`, `obs-alert-ack-list` + `--expires-in` parsing (cap 7d).
5. Compactor learns `alert_acks` (cutoff = `expires_at`).
6. Tests + full suite green.
