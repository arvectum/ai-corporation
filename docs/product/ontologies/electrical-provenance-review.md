# ARV-067G — provenance и экспертная верификация электротехнической онтологии

Статус: `RESEARCH_ASSET_READY`.

## Назначение

ARV-067G добавляет единый аудируемый слой происхождения для классификационных, атрибутивных, реляционных и нормативных утверждений электротехнической онтологии.

Слой не изменяет исходные активы ARV-067A–ARV-067F и не подключён к production runtime. Он индексирует утверждения, фиксирует конкретные версии источников, хранит неизменяемые события проверки и блокирует применение неподтверждённых данных.

## Модель provenance-утверждения

Каждый claim содержит:

- стабильный `logical_claim_id`, версию и неизменяемый `claim_id`;
- тип утверждения: `category`, `alias`, `attribute`, `allowed_value`, `relation` или `normative_requirement`;
- subject, predicate и object;
- SHA-256 канонического assertion payload;
- одну или несколько ссылок на точные ревизии источников;
- hash источника и locator: path, page, row, clause или JSON Pointer;
- extraction method, timestamp и confidence;
- review status, reviewer metadata, rationale и ссылку на текущее событие истории;
- `review_required`, `production_ready`, conflict groups, supersession и active state.

## Версии источников

Источник отделён от его ревизии. Каждая ревизия содержит собственный content hash, дату фиксации, статус, срок повторной проверки и `supersedes_revision_id`.

Изменение файла или внешнего документа создаёт новую ревизию. Перезапись content hash уже использованной ревизии запрещена валидатором.

Первая волна включает шесть источников:

- фрагмент категорий выключателей ARV-067B;
- фрагмент характеристик коммутации и изоляции ARV-067A;
- реестр характеристик и допустимых значений ARV-067A;
- реляционные утверждения коммутационного оборудования ARV-067C;
- структурированный фрагмент нормативных требований ARV-067F;
- снимок первичного реестра ПАО «Россети» на 10.06.2026.

Полный текст исходного PDF, защищённые материалы и секреты не сохраняются.

## Покрытие первой волны

Добавлено 24 provenance-утверждения — по четыре на каждый обязательный тип:

- 4 категории;
- 4 alias;
- 4 характеристики;
- 4 допустимых значения;
- 4 отношения;
- 4 нормативных требования.

Все первоначальные записи имеют статус `machine_extracted`. Ни одна запись не объявлена `human_verified`, потому что отдельная экспертная проверка ещё не проводилась. Поэтому `production_ready: false` у всех 24 claims.

## Неизменяемая история проверки

История состоит из append-only review events. Событие хранит:

- claim ID и последовательный номер;
- предыдущий и новый статусы;
- тип участника, reviewer ID и роль;
- timestamp и rationale;
- hash предыдущего события;
- собственный SHA-256 hash канонического payload.

Допустимые статусы:

- `machine_extracted`;
- `human_verified`;
- `rejected`;
- `superseded`.

Переход к `human_verified` требует идентификатора эксперта, допустимой роли, даты и содержательного rationale. История не переписывается при появлении новой версии источника или claim.

## Production gate

Утверждение может стать production-ready только при одновременном выполнении условий:

- статус `human_verified`;
- confidence не ниже 0,90;
- проверка экспертом электротехнического или нормативного профиля;
- claim активен и больше не требует review;
- все source revisions актуальны;
- нет нерешённых конфликтов.

Низкая уверенность автоматически оставляет claim в `review_required`. В первой волне отдельно отмечены два low-confidence relation claims: совместимость автомата с абстрактной ролью расцепителя и альтернативность элегазового и вакуумного выключателей.

## Конфликты

Конфликтующие источники или claims должны быть зарегистрированы отдельной conflict group. Скрытое примирение запрещено.

Нерешённый конфликт блокирует production-ready и сохраняет все конфликтующие утверждения для review. Разрешение конфликта требует отдельного review event. В исходной выборке первой волны известных конфликтов не зафиксировано; механизм проверяется отрицательными contract fixtures.

## Аудит-отчёт

Детерминированный отчёт на 27.07.2026 показывает:

- 6 источников;
- 24 claims;
- 24 review events;
- 0 production-ready claims;
- 24 claims, требующих проверки;
- 2 low-confidence claims;
- 1 источник, требующий периодической повторной проверки;
- 0 устаревших ревизий;
- 0 зарегистрированных конфликтов.

Отчёт пересобирается из реестров. Расхождение committed report с вычисленным результатом считается ошибкой.

## Проверка

```bash
python schemas/categories/electrical/validate_provenance.py
python schemas/categories/electrical/generate_provenance_report.py
python -m pytest -q tests/test_arv067g_provenance.py
python -m compileall -q \
  schemas/categories/electrical/provenance_contract.py \
  schemas/categories/electrical/generate_provenance_report.py \
  schemas/categories/electrical/validate_provenance.py \
  tests/test_arv067g_provenance.py
```

Ожидаемый маркер:

```text
ARV-067G provenance: OK (sources=6, claims=24, review_events=24, conflicts=0, fixture_cases=24, production_ready=0, runtime_import=false)
```

## Границы

- наличие source hash не доказывает корректность исходного утверждения;
- `machine_extracted` и прежние статусы `source_verified` не заменяют независимое экспертное решение;
- ни одно утверждение первой волны не активировано для production;
- полные тексты ограниченного доступа и секреты не сохраняются;
- production resolver, БД и миграции не изменены;
- `runtime_import: false`;
- ARV-067H должен создать контролируемые truth packs и benchmark;
- ARV-067I остаётся обязательным перед shadow runtime.

Маркеры результата:

- `ARV-067G_PROVENANCE_CLAIM_REGISTRY_READY`
- `ARV-067G_IMMUTABLE_REVIEW_HISTORY_READY`
- `ARV-067G_LOW_CONFIDENCE_AND_CONFLICT_GATES_READY`
- `ARV-067G_AUDIT_REPORT_READY`
- `ARV-067G_PRODUCTION_RUNTIME_NOT_WIRED`
