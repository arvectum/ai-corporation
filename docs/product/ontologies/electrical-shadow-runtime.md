# ARV-067I — безопасный shadow runtime электротехнической онтологии

Статус: `WIRED_BUT_INACTIVE`.

## Назначение

ARV-067I добавляет изолированный runtime-контур для электротехнической онтологии ARV-067. Контур подключается к завершённому анализу demo-run как независимая background-ветка, сохраняет только audit/metadata и не меняет основной ответ, отчёт, PDF, go/no-go или внешние действия.

Текущая версия намеренно не активирует ни один профиль. ARV-067H остаётся в состоянии `RELEASE_BLOCKED`: независимая приёмка не выполнена, accepted profiles — 0. Поэтому даже при ручном включении feature flag loader остановится на benchmark gate.

## Runtime wiring

Middleware наблюдает только успешный вызов:

```text
POST /api/demo/tender-agent/runs/{run_id}/analyze
```

После формирования основного ответа он запускает отдельную background-задачу. Ошибка shadow-ветки не меняет HTTP-ответ и не переводит основной run в другой статус.

Сводный безопасный результат доступен только для чтения:

```text
GET /api/demo/tender-agent/runs/{run_id}/shadow/electrical-ontology
```

Endpoint возвращает metadata-summary без исходного текста, персональных данных и полного audit payload.

## Feature flags и kill switch

Канонические переменные окружения:

- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_ENABLED` — по умолчанию `false`;
- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_KILL_SWITCH` — по умолчанию `true`;
- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_APPROVAL_ID` — обязательная ссылка на явное human approval;
- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_ALLOWED_PROFILES` — явный comma-separated allowlist;
- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_POLICY_PATH` — необязательный путь к versioned policy;
- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_AUDIT_ROOT` — отдельное audit-хранилище;
- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_MAX_SOURCE_CHARS` — default 12 000;
- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_MAX_ITEMS` — default 64;
- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_MAX_AUDIT_BYTES` — default 262 144;
- `ARVECTUM_ELECTRICAL_ONTOLOGY_SHADOW_TIMEOUT_MS` — default 250.

Совместимые `AI_CORP_*` aliases сохранены.

Для запуска shadow matching должны одновременно выполняться все условия:

1. feature flag включён;
2. kill switch выключен;
3. version-pinned ontology и benchmark совместимы;
4. ARV-067H имеет `RELEASE_ELIGIBLE` и `release_gate_passed: true`;
5. independent acceptance завершена для всех 15 профилей;
6. указан approval ID;
7. указан allowlist;
8. профиль находится одновременно в accepted set и allowlist.

Текущее состояние не проходит пункты 4–5, поэтому matching заблокирован корректно и воспроизводимо.

## Version-pinned loader

Loader фиксирует и хеширует:

- policy ARV-067I;
- registry ARV-067D;
- четыре profile fragments;
- matcher ARV-067D;
- release report ARV-067H;
- acceptance registry ARV-067H.

Для каждого audit создаются:

- ontology registry ID и version;
- benchmark ID и version;
- SHA-256 каждого source asset;
- общий snapshot root hash.

Несовпадение ID, version, profile count или acceptance reference приводит к `SAFE_FAILURE`; основной анализ остаётся неизменным.

## Shadow extraction и matching

Контур читает только bounded-копии уже сохранённых normalized/output файлов. Бинарные исходники не открываются повторно.

Из primary output выделяются item-like записи с названием, характеристиками и attributes. Профиль определяется по aliases и canonical marks ARV-067D. При наличии пары `requested_attributes`/`candidate_attributes` используется существующий детерминированный matcher ARV-067D.

Если товар-кандидат или структурированные характеристики отсутствуют, результат остаётся `UNCERTAIN` с reason codes:

- `SHADOW_CANDIDATE_PRODUCT_NOT_PROVIDED`;
- `SHADOW_STRUCTURED_COMPARISON_NOT_AVAILABLE`;
- `HUMAN_REVIEW_REQUIRED`.

Shadow-контур никогда не превращает отсутствие данных в `EXACT`.

## Сравнение и метрики

Audit содержит:

- outcome counts;
- candidate count;
- disagreement count/rate;
- uncertain count/rate;
- error count;
- latency;
- cost units (`0` для детерминированного контура);
- ontology/benchmark trace;
- item-level reason codes.

Если основной результат содержит canonical category IDs, shadow category сравнивается с ними. Любое расхождение остаётся `operator_review_required` и не изменяет основной verdict.

## Tenant isolation, redaction и bounded payloads

Audit сохраняется в отдельном partition:

```text
{audit_root}/{sha256(tenant_id)[:24]}/{run_id}/electrical-ontology-shadow.v1.json
```

Raw tenant ID в пути и payload не сохраняется.

Перед сохранением snippets маскируются:

- email;
- российские телефонные номера;
- последовательности из 10–14 цифр;
- bearer/token/API-key/secret patterns.

Полный исходный текст не сохраняется. Audit ограничен по числу символов, items и общему размеру.

## Safety invariants

Всегда выполняются следующие запреты:

- изменение primary result;
- изменение PDF, DOCX или HTML report;
- изменение go/no-go;
- отправка на ЭТП;
- email;
- электронная подпись;
- любые внешние действия;
- автоматический переход в `operator_approved` или `production_active`;
- production accuracy claims.

В audit эти поля всегда равны `false`, а `human_review_required` — `true`.

## Проверка

```bash
python schemas/categories/electrical/validate_shadow_runtime.py
python -m pytest -q tests/test_arv067i_shadow_runtime.py
python -m pytest -q tests/test_arv067i_shadow_middleware.py
python -m compileall -q \
  src/modules/electrical_ontology_shadow \
  schemas/categories/electrical/validate_shadow_runtime.py \
  tests/test_arv067i_shadow_runtime.py \
  tests/test_arv067i_shadow_middleware.py
```

Ожидаемый маркер:

```text
ARV-067I shadow runtime: OK (policy=1, fixture_cases=18, feature_default=false, kill_switch_default=true, release_gate=false, accepted_profiles=0, production_effect=false)
```

## Результат

- `ARV-067I_VERSION_PINNED_LOADER_READY`
- `ARV-067I_BACKGROUND_SHADOW_BRANCH_READY`
- `ARV-067I_TENANT_REDACTION_BOUNDS_READY`
- `ARV-067I_AUDIT_AND_DISAGREEMENT_METRICS_READY`
- `ARV-067I_KILL_SWITCH_AND_ROLLBACK_READY`
- `ARV-067I_ARV067H_GATE_ENFORCED`
- `ARV-067I_PRIMARY_RESULT_IMMUTABLE`
- `ARV-067I_PRODUCTION_ACTIVATION_NOT_IMPLEMENTED`
