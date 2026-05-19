# mthydra

Resilient Telegram access controller. See `doc/design.md` for the architecture, `doc/build-plan.md` for the artifact decomposition, and `doc/specs/` and `doc/plans/` for individual artifact specs and implementation plans.

## Development

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
# Also install the backup-monitor wheel for integration tests:
pip install -e 'mthydra-backup-monitor[dev]'
pytest
```

## Deployment (Ubuntu 24.04)

```bash
sudo apt install python3-venv age
sudo useradd --system --home /var/lib/mthydra --create-home mthydra
sudo -u mthydra python3 -m venv /opt/mthydra
sudo -u mthydra /opt/mthydra/bin/pip install <path-to-wheel>

# Install the backup monitor wheel on the monitor host (separate from active controller)
sudo -u mthydra /opt/mthydra/bin/pip install <path-to-mthydra-backup-monitor-wheel>

# Create directory layout
sudo systemd-tmpfiles --create packaging/tmpfiles.d/mthydra.conf

# Install service units
sudo install -Dm0644 packaging/systemd/mthydra-controller.service \
    /etc/systemd/system/mthydra-controller.service
sudo install -Dm0644 packaging/systemd/mthydra-backup-monitor.service \
    /etc/systemd/system/mthydra-backup-monitor.service

# Configure
sudo install -Dm0644 packaging/etc/mthydra/controller.toml.example \
    /etc/mthydra/controller.toml
# Edit /etc/mthydra/controller.toml, then place the age public key:
echo 'age1...' | sudo tee /etc/mthydra/age-recipient.txt

# Initialise state
sudo /opt/mthydra/bin/mthydra-controller init \
    --db-path /var/lib/mthydra/state.sqlite \
    --age-recipient-file /etc/mthydra/age-recipient.txt \
    --provider-credential aws=AKID:SECRET \
    --provider-credential b2=KEY_ID:KEY_SECRET

# Start
sudo systemctl daemon-reload
sudo systemctl enable --now mthydra-controller
# On the monitor host:
sudo systemctl enable --now mthydra-backup-monitor
```
