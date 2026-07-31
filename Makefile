REDIS_HOST_COMPOSE = docker --context "$${ARVECTUM_DOCKER_CONTEXT:-colima}" compose -f docker-compose.redis.yml -f docker-compose.redis-host.yml

.PHONY: check test ci test-redis-integration test-r8-postgres test-r8-acceptance-foundation test-r8-acceptance-tenant-concurrency test-r8-acceptance-migration-backfill test-r8-acceptance-tampering test-r8-acceptance test-arv001 test-arv076 audit-external-runtime-paths eis-preflight r4-local-start redis-start redis-ping redis-stop redis-clean redis-host-config redis-host-start redis-host-ping redis-host-stop

check:
	python -m compileall -q src scripts quality_gates
	python -m ruff check src/main.py src/shared/api/router_registry.py src/modules/customer_pilot/router.py src/shared/api/middleware.py src/shared/config/settings.py src/shared/runtime/preflight.py src/shared/redis/ scripts/ops/arv076_runtime_backup.py scripts/ops/audit_external_runtime_paths.py quality_gates/arv001/evaluate.py tests/quality/test_arv001_quality_gate.py tests/integration/conftest.py tests/integration/test_redis_*.py tests/unit/redis/ tests/ops/test_arv076_runtime_backup.py tests/ops/test_audit_external_runtime_paths.py tests/test_runtime_preflight_no_fallback.py tests/test_r0_security_boundary.py

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

test-arv001:
	python quality_gates/arv001/evaluate.py validate-package
	python -m pytest -q tests/quality/test_arv001_quality_gate.py

test-arv076:
	python -m pytest -q tests/ops/test_arv076_runtime_backup.py

audit-external-runtime-paths:
	python scripts/ops/audit_external_runtime_paths.py --filesystem-only --json

ci: check test

test-redis-unit:
	python -m pytest -q tests/unit/redis/

test-redis-integration:
	@test -n "$${AI_CORP_REDIS_TEST_URL:-$${AI_CORP_REDIS_URL:-redis://127.0.0.1:6379/1}}" || (echo "test Redis URL is required"; exit 2); \
		test_url="$${AI_CORP_REDIS_TEST_URL:-$${AI_CORP_REDIS_URL:-redis://127.0.0.1:6379/1}}"; \
		canonical_url="$${ARVECTUM_REDIS_URL:-}"; \
		if [ -n "$$canonical_url" ] && [ "$$test_url" = "$$canonical_url" ]; then echo "test Redis endpoint must differ from canonical runtime endpoint"; exit 2; fi; \
		test_namespace="$${AI_CORP_REDIS_TEST_NAMESPACE:-test-arv007-integration}"; \
		case "$$test_namespace" in test-*) ;; *) echo "test Redis namespace must start with test-"; exit 2;; esac; \
		mkdir -p output; echo "redis_test_url_configured=yes"; echo "redis_test_namespace_configured=yes"; \
		overall=0; for f in tests/integration/test_redis_*_integration.py; do \
			log="output/$$(basename $$f).log"; \
			ARVECTUM_REDIS_ENABLED=true AI_CORP_REDIS_ENABLED=true \
			AI_CORP_REDIS_CANONICAL_URL="$$canonical_url" \
			ARVECTUM_REDIS_URL="$$test_url" AI_CORP_REDIS_URL="$$test_url" \
			ARVECTUM_REDIS_NAMESPACE="$$test_namespace" AI_CORP_REDIS_NAMESPACE="$$test_namespace" \
				python -m pytest -v "$$f" --run-integration -p no:cacheprovider > "$$log" 2>&1; \
			status=$$?; echo "redis_test_file=$$(basename $$f) status=$$status"; \
			if [ "$$status" -ne 0 ]; then overall=$$status; fi; \
		done; exit $$overall

redis-start:
	docker compose -f docker-compose.redis-test.yml up -d

redis-ping:
	docker compose -f docker-compose.redis-test.yml exec redis redis-cli ping

redis-stop:
	docker compose -f docker-compose.redis-test.yml down

redis-clean:
	docker compose -f docker-compose.redis-test.yml down -v

redis-host-config:
	@test -f .env.local || (echo ".env.local is required"; exit 2)
	@set -a; . ./.env.local; set +a; \
		test -n "$$ARVECTUM_REDIS_PASSWORD" || (echo "ARVECTUM_REDIS_PASSWORD is required"; exit 2); \
		$(REDIS_HOST_COMPOSE) config --quiet

redis-host-start:
	@test -f .env.local || (echo ".env.local is required"; exit 2)
	@set -a; . ./.env.local; set +a; \
		test -n "$$ARVECTUM_REDIS_PASSWORD" || (echo "ARVECTUM_REDIS_PASSWORD is required"; exit 2); \
		$(REDIS_HOST_COMPOSE) up -d redis

redis-host-ping:
	@test -f .env.local || (echo ".env.local is required"; exit 2)
	@set -a; . ./.env.local; set +a; \
		test -n "$$ARVECTUM_REDIS_PASSWORD" || (echo "ARVECTUM_REDIS_PASSWORD is required"; exit 2); \
		$(REDIS_HOST_COMPOSE) exec -T redis redis-cli ping

redis-host-stop:
	@test -f .env.local || (echo ".env.local is required"; exit 2)
	@set -a; . ./.env.local; set +a; \
		test -n "$$ARVECTUM_REDIS_PASSWORD" || (echo "ARVECTUM_REDIS_PASSWORD is required"; exit 2); \
		$(REDIS_HOST_COMPOSE) stop redis

# Local-only developer targets: require the maintainer's local trust material
# under /Users/master and are intentionally not used by CI or deployment.
eis-preflight:
	@test -x .venv-r3/bin/python || (echo ".venv-r3/bin/python is required"; exit 2)
	@test -f /Users/master/.config/arvectum/r3-soap-token.env || (echo "R3 SOAP token environment is required"; exit 2)
	@zsh -lc 'source /Users/master/.config/arvectum/r3-soap-token.env; export ARVECTUM_ETP_TLS_ENABLED=true ARVECTUM_ETP_TLS_POLICY_PATH=/Users/master/.config/arvectum/trust/policy.yaml ARVECTUM_ETP_TLS_FAIL_CLOSED=true ARVECTUM_ETP_PROXY_BYPASS_ENABLED=true NO_PROXY="zakupki.gov.ru,.zakupki.gov.ru" no_proxy="zakupki.gov.ru,.zakupki.gov.ru" ZAKUPKI_GOV_RU_SOAP_ENABLED=true; unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; .venv-r3/bin/python scripts/ops/etp_trust.py verify-host --host zakupki.gov.ru; .venv-r3/bin/python scripts/ops/etp_trust.py verify-host --host int.zakupki.gov.ru'

r4-local-start: eis-preflight
	@zsh -lc 'source /Users/master/.config/arvectum/r3-soap-token.env; export ARVECTUM_ETP_TLS_ENABLED=true ARVECTUM_ETP_TLS_POLICY_PATH=/Users/master/.config/arvectum/trust/policy.yaml ARVECTUM_ETP_TLS_FAIL_CLOSED=true ARVECTUM_ETP_PROXY_BYPASS_ENABLED=true NO_PROXY="zakupki.gov.ru,.zakupki.gov.ru" no_proxy="zakupki.gov.ru,.zakupki.gov.ru" ZAKUPKI_GOV_RU_SOAP_ENABLED=true; unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; .venv-r3/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8001'
