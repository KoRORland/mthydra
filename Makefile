.PHONY: test test-monitor cov lint smoke smoke-descriptor smoke-install smoke-ru-cycle smoke-eu-automation help

help:
	@echo "Targets:"
	@echo "  test         Run controller test suite (tests/)"
	@echo "  test-monitor Run backup-monitor test suite (mthydra-backup-monitor/tests/)"
	@echo "  cov          Run controller tests with coverage report"
	@echo "  lint         Run ruff lint + format check on both packages"
	@echo "  smoke        Print the manual smoke-test procedure (no automation)"

test:
	pytest tests/

test-monitor:
	pytest mthydra-backup-monitor/tests/

cov:
	pytest tests/ --cov --cov-report=term-missing

lint:
	ruff check src/ tests/
	ruff check mthydra-backup-monitor/src/ mthydra-backup-monitor/tests/

smoke-descriptor:
	@echo "--- descriptor smoke test (spec B §13.4) ---"
	@echo "1. mthydra-controller init --db-path /tmp/smoke.sqlite \\"
	@echo "     --age-recipient-file /etc/mthydra/age-recipient.txt"
	@echo "2. mthydra-controller eu-add <fingerprint> <endpoint> --db-path /tmp/smoke.sqlite"
	@echo "3. mthydra-controller descriptor-sign-now --db-path /tmp/smoke.sqlite"
	@echo "4. mthydra-controller descriptor-show --db-path /tmp/smoke.sqlite"
	@echo "5. Extract payload + sig from DB, then:"
	@echo "   mthydra-controller descriptor-verify payload.json sig.bin --db-path /tmp/smoke.sqlite"
	@echo "6. Remove /tmp/smoke.sqlite"

# Smoke test: manual procedure only — cannot be automated without a real B2 bucket
# and the operator's age private key.  Run before every release.
smoke-install:
	@echo "--- mthydra install smoke procedure (spec N) ---"
	@echo "1. Provision a naked Ubuntu 24.04 root shell on an EU VPS you trust."
	@echo "2. Generate the operator age key ON YOUR LAPTOP (NOT on the host):"
	@echo "     age-keygen -o ~/.config/mthydra/operator.age"
	@echo "     grep '# public key:' ~/.config/mthydra/operator.age   # → age1..."
	@echo "3. Copy packaging/etc/mthydra/install.ini.example → install.ini and edit."
	@echo "4. scp scripts/install.sh root@<eu-host>:/root/ ; scp install.ini ..."
	@echo "5. On the EU host as root:"
	@echo "     export B2_APPLICATION_KEY=<the b2 secret>"
	@echo "     sh /root/install.sh --git-url <repo> --git-ref <tag> \\"
	@echo "         --config /root/install.ini --verbose"
	@echo "6. Confirm: heartbeat email + Telegram crit test both arrived."
	@echo "7. systemctl status mthydra-controller mthydra-daily-check.timer"
	@echo "8. Repeat with --standby on the warm-substitute host."

smoke-ru-cycle:
	@echo "--- mthydra ru-image-cycle smoke procedure (spec O) ---"
	@echo "1. On the EU controller host:"
	@echo "     mthydra-controller upstream-check          # confirm a release is available"
	@echo "     mthydra-ops image-build-template > /tmp/profile-v2.1.7.json"
	@echo "     # edit profile JSON per runbook §3.2"
	@echo "2. Have 2 (provider, region) targets ready for canaries."
	@echo "3. Run the cycle:"
	@echo "     mthydra-ops ru-image-cycle \\"
	@echo "         --release v2.1.7 --profile-json /tmp/profile-v2.1.7.json \\"
	@echo "         --canaries 2 \\"
	@echo "         --canary-target provider=selectel,region=ru-msk-1 \\"
	@echo "         --canary-target provider=firstvds,region=ru-spb-1 \\"
	@echo "         --agent-source-url <b2 url> --agent-source-sha256 <sha> \\"
	@echo "         --descriptor-refresh-url <b2 url>"
	@echo "4. Paste each cloud-init bundle in the corresponding provider console,"
	@echo "   feed each public IP back to the prompt as the VMs boot."
	@echo "5. Submit probe-record from each registered vantage during the soak."
	@echo "6. Confirm the promote prompt → iv-v2.1.7 in image-list."
	@echo "7. For a single replacement box (no image cycle):"
	@echo "     mthydra-ops ru-bringup --provider selectel --region ru-msk-1 \\"
	@echo "         --agent-source-url <b2> --agent-source-sha256 <sha> \\"
	@echo "         --descriptor-refresh-url <b2>"

smoke-eu-automation:
	@echo "--- mthydra EU-side RU automation smoke procedure (spec P) ---"
	@echo "1. On the EU controller host as the mthydra user:"
	@echo "     mthydra-ops image-prepare --yes        # latest mtg -> built -> promoted"
	@echo "     mthydra-ops agent-publish              # tar + S3 upload + presign -> /var/lib/mthydra/agent.json"
	@echo "2. For each vantage:"
	@echo "     mthydra-controller vantage-set-ssh <id> --host <ip> --user probe --key-path /var/lib/mthydra/ssh/<id>.key"
	@echo "3. Confirm the probe runner is ticking (within 30 min):"
	@echo "     mthydra-controller obs-status --json | jq '.obligations_healthy[] | select(.obligation_id==\"probe_coverage_proven\")'"
	@echo "4. Bring up a box with no extra flags:"
	@echo "     mthydra-ops ru-bringup --provider timeweb --region ru-msk-1 \\"
	@echo "         --descriptor-refresh-url <b2>"
	@echo "5. Probe coverage should stay green automatically going forward."

smoke:
	@echo "--- mthydra smoke test procedure (spec A §13.4) ---"
	@echo "1. Ensure /etc/mthydra/controller.toml points at a test B2 bucket."
	@echo "2. Run: mthydra-controller init --db-path /tmp/smoke.sqlite \\"
	@echo "          --age-recipient-file /etc/mthydra/age-recipient.txt \\"
	@echo "          --provider-credential b2=KEY_ID:KEY_SECRET"
	@echo "3. Run: mthydra-controller backup-now --db-path /tmp/smoke.sqlite \\"
	@echo "          --config /etc/mthydra/controller.toml"
	@echo "4. Confirm generation 1 blob appears in the test bucket."
	@echo "5. Download gen-0000000001.age and run:"
	@echo "   age -d -i ~/.age/operator.key gen-0000000001.age > /tmp/smoke-restored.sqlite"
	@echo "6. Run: mthydra-controller restore --from gen-0000000001.age \\"
	@echo "          --identity ~/.age/operator.key --into /tmp/smoke-r.sqlite --summary-only"
	@echo "7. Verify schema_version and burned_domains_count in output."
	@echo "8. Remove /tmp/smoke.sqlite /tmp/smoke-restored.sqlite /tmp/smoke-r.sqlite"
