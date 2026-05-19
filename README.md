# mthydra

Resilient Telegram access controller. See `doc/design.md` for the architecture, `doc/build-plan.md` for the artifact decomposition, and `doc/specs/` and `doc/plans/` for individual artifact specs and implementation plans.

## Development

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
# Also install the backup-monitor wheel for integration tests:
pip install -e 'mthydra-backup-monitor[dev]'
```

Common targets (see `Makefile` for full list):

```
make test          # controller test suite
make test-monitor  # backup-monitor test suite
make cov           # controller tests with coverage report
make lint          # ruff on both packages
make smoke         # print manual smoke-test procedure
```

**Coverage** (spec A §13.5 — `controller.backup + controller.state + controller.restore`):
`91%` line coverage measured with `age` binary absent (decrypt.py is 32% without it; all
other in-scope modules ≥ 88%). With `age` installed the overall number is expected to reach
≥ 96%.

## Building wheels

Both packages use `setuptools` and `pyproject.toml`. Install `build` once, then:

```bash
pip install build
# Controller wheel (output: dist/mthydra-*.whl)
python -m build
# Monitor wheel (output: mthydra-backup-monitor/dist/mthydra_backup_monitor-*.whl)
python -m build mthydra-backup-monitor/
```

Copy the resulting `.whl` files to target hosts and install with `pip install <wheel>`.

## Deployment (Ubuntu 24.04)

**Pre-requisites:** create the B2 (Backblaze) bucket with Object Lock + Versioning
enabled *before* running `init`. The controller cannot create the bucket itself.

```bash
sudo apt install python3-venv age
sudo useradd --system --home /var/lib/mthydra --create-home mthydra
sudo -u mthydra python3 -m venv /opt/mthydra

# Controller host
sudo -u mthydra /opt/mthydra/bin/pip install mthydra-*.whl

# Monitor host (separate from active controller per spec A §8 / plan §16.1)
sudo -u mthydra /opt/mthydra/bin/pip install mthydra_backup_monitor-*.whl

# Create directory layout
sudo systemd-tmpfiles --create packaging/tmpfiles.d/mthydra.conf

# Install service units
sudo install -Dm0644 packaging/systemd/mthydra-controller.service \
    /etc/systemd/system/mthydra-controller.service
sudo install -Dm0644 packaging/systemd/mthydra-backup-monitor.service \
    /etc/systemd/system/mthydra-backup-monitor.service

# Configure — controller host
sudo install -Dm0644 packaging/etc/mthydra/controller.toml.example \
    /etc/mthydra/controller.toml
# Edit /etc/mthydra/controller.toml (bucket name, access_key_id, etc.)
echo 'age1...' | sudo tee /etc/mthydra/age-recipient.txt
sudo install -Dm0600 packaging/etc/mthydra/controller.env.example \
    /etc/mthydra/controller.env
# Edit /etc/mthydra/controller.env (uncomment/fill MTHYDRA_* vars as needed)

# Configure — monitor host
sudo install -Dm0644 packaging/etc/mthydra/controller.toml.example \
    /etc/mthydra/controller.toml
# Edit /etc/mthydra/controller.toml (same bucket config as controller)
sudo install -Dm0600 packaging/etc/mthydra/backup-monitor.env.example \
    /etc/mthydra/backup-monitor.env
# Edit /etc/mthydra/backup-monitor.env (MTHYDRA_B2_SECRET, MTHYDRA_SMTP_*)

# Initialise state (controller host only)
sudo /opt/mthydra/bin/mthydra-controller init \
    --db-path /var/lib/mthydra/state.sqlite \
    --age-recipient-file /etc/mthydra/age-recipient.txt \
    --provider-credential aws=AKID:SECRET \
    --provider-credential b2=KEY_ID:KEY_SECRET

# Verify the backup pipeline (optional but recommended before enabling the service)
sudo /opt/mthydra/bin/mthydra-controller backup-now \
    --db-path /var/lib/mthydra/state.sqlite \
    --config /etc/mthydra/controller.toml

# Start
sudo systemctl daemon-reload
sudo systemctl enable --now mthydra-controller
# On the monitor host:
sudo systemctl enable --now mthydra-backup-monitor
```
