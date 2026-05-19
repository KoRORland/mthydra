.PHONY: test test-monitor cov lint smoke help

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

# Smoke test: manual procedure only — cannot be automated without a real B2 bucket
# and the operator's age private key.  Run before every release.
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
