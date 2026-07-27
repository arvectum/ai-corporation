.PHONY: check test ci test-redis-integration test-r8-postgres test-r8-acceptance-foundation test-r8-acceptance-tenant-concurrency test-r8-acceptance-migration-backfill test-r8-acceptance-tampering test-r8-acceptance eis-preflight r4-local-start redis-start redis-ping redis-stop redis-clean

check:
	python -m compileall -q src
	python -m ruff check src/modules/customer_pilot/router.py src/shared/api/middleware.py src/shared/config/settings.py src/shared/runtime/preflight.py src/shared/redis/ tests/integration/conftest.py tests/integration/test_redis_*.py tests/unit/redis/ tests/test_r0_security_boundary.py

test:
	python -m pytest -q

test-r8-postgres:
	python scripts/acceptance/run_r8_postgres_tests.py

test-r8-acceptance-foundation:
	python scripts/acceptance/run_r8_acceptance.py --phase foundation

test-r8-acceptance-tenant-concurrency:
	python scripts/acceptance/run_r8_acceptance.py --phase tenant-concurrency

test-r8-acceptance-migration-backfill:
	python scripts/acceptance/run_r8_migration_backfill.py

test-r8-acceptance-tampering:
	python scripts/acceptance/run_r8_tampering.py

test-r8-acceptance:
	python scripts/acceptance/run_r8_acceptance.py --phase full

ci: check test

test-redis-unit:
	python -m pytest -q tests/unit/redis/

test-redis-integration:
	mkdir -p output; echo "pyver:$$(python --version 2>&1)"; echo "url:$$AI_CORP_REDIS_URL"; echo "ns:$$AI_CORP_REDIS_NAMESPACE"; echo "files:$$(ls tests/integration/test_redis_*_integration.py 2>&1)"; overall=0; for f in tests/integration/test_redis_*_integration.py; do echo "=== $$f ==="; python -m pytest -v "$$f" --run-integration -p no:cacheprovider > output/$$(basename $$f).log 2>&1; status=$$?; echo "exit($$f)=$$status"; cat output/$$(basename $$f).log; if [ "$$status" -ne 0 ]; then overall=$$status; fi; done; exit $$overall

redis-start:
	docker compose -f docker-compose.redis-test.yml up -d

redis-ping:
	docker compose -f docker-compose.redis-test.yml exec redis redis-cli ping

redis-stop:
	docker compose -f docker-compose.redis-test.yml down

redis-clean:
	docker compose -f docker-compose.redis-test.yml down -v

# Local-only developer targets: require the maintainer's local trust material
# under /Users/master and are intentionally not used by CI or deployment.
eis-preflight:
	@test -x .venv-r3/bin/python || (echo ".venv-r3/bin/python is required"; exit 2)
	@test -f /Users/master/.config/arvectum/r3-soap-token.env || (echo "R3 SOAP token environment is required"; exit 2)
	@zsh -lc 'source /Users/master/.config/arvectum/r3-soap-token.env; export ARVECTUM_ETP_TLS_ENABLED=true ARVECTUM_ETP_TLS_POLICY_PATH=/Users/master/.config/arvectum/trust/policy.yaml ARVECTUM_ETP_TLS_FAIL_CLOSED=true ARVECTUM_ETP_PROXY_BYPASS_ENABLED=true NO_PROXY="zakupki.gov.ru,.zakupki.gov.ru" no_proxy="zakupki.gov.ru,.zakupki.gov.ru" ZAKUPKI_GOV_RU_SOAP_ENABLED=true; unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; .venv-r3/bin/python scripts/ops/etp_trust.py verify-host --host zakupki.gov.ru; .venv-r3/bin/python scripts/ops/etp_trust.py verify-host --host int.zakupki.gov.ru'

r4-local-start: eis-preflight
	@zsh -lc 'source /Users/master/.config/arvectum/r3-soap-token.env; export ARVECTUM_ETP_TLS_ENABLED=true ARVECTUM_ETP_TLS_POLICY_PATH=/Users/master/.config/arvectum/trust/policy.yaml ARVECTUM_ETP_TLS_FAIL_CLOSED=true ARVECTUM_ETP_PROXY_BYPASS_ENABLED=true NO_PROXY="zakupki.gov.ru,.zakupki.gov.ru" no_proxy="zakupki.gov.ru,.zakupki.gov.ru" ZAKUPKI_GOV_RU_SOAP_ENABLED=true; unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; .venv-r3/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8001'
