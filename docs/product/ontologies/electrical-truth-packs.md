# ARV-067H — truth packs и расширенный benchmark электротехнической онтологии

Статус: `CANDIDATE_TRUTH_PACK_CONTRACT_READY`, перевод в shadow runtime заблокирован.

## Назначение

ARV-067H добавляет воспроизводимый benchmark-контур для 15 детальных профилей ARV-067D. Контур проверяет объёмы выборок, статусы сопоставления, критические различия, разбиение train/dev/test, отсутствие точного leakage, отдельные OCR- и unseen-manufacturer-срезы, метрики и release gates.

Первая версия является synthetic contract pack. Она проверяет корректность matcher-контракта и инфраструктуры benchmark, но не заменяет независимо размеченную выборку реальных закупок и не подтверждает production accuracy.

## Объём

Для каждого из 15 профилей детерминированно материализуется 160 элементов:

- 100 positive: 35 `EXACT`, 25 `LIKELY_ANALOG`, 20 `PARTIAL`, 20 `UNCERTAIN`;
- 60 hard-negative: 60 `NO_MATCH` с критическим несовпадением.

Общий объём — 2400 truth items:

- train — 1200;
- dev — 600;
- test — 600;
- positive — 1500;
- hard-negative — 900.

Профили:

- cable_joint;
- cable_lug_connector;
- cable_pipe_system;
- surge_arrester;
- high_voltage_fuse;
- high_voltage_disconnector;
- load_break_switch;
- recloser;
- line_hardware;
- insulator;
- residual_current_device;
- residual_current_breaker;
- switch_disconnector;
- control_relay;
- motor_starter.

## Форматы и шум

Генератор создаёт четыре формата исходного представления:

- обычный текст;
- таблица технического задания;
- каталожная карточка;
- OCR-подобный скан.

Используются варианты clean, abbreviation, punctuation loss и OCR confusion. В committed contract получается 510 OCR-элементов.

OCR-срез проверяет downstream matcher на структурированных значениях, сопровождаемых OCR-подобным исходным текстом. Он не измеряет точность OCR-движка и не должен так интерпретироваться.

## Трассировка и хеширование

Каждый truth item содержит:

- стабильный item ID;
- профиль и target category;
- split;
- manufacturer ID;
- уникальный source-record ID;
- source format и noise mode;
- surface text;
- requested и candidate attributes;
- evidence flag;
- ожидаемый outcome;
- ожидаемые группы проблемных атрибутов;
- benchmark ID и версию;
- ontology registry ID и версию;
- generator version;
- SHA-256 канонического payload.

Pack root вычисляется как SHA-256 отсортированного списка item hashes. Повторная генерация обязана давать те же 15 pack roots.

Генератор поддерживает материализацию 15 JSONL-файлов и versioned index:

```bash
python schemas/categories/electrical/truth_pack_generator.py \
  --output-dir /tmp/arv067h-truth-packs
```

Сгенерированные JSONL не коммитятся как production dataset. Они воспроизводятся из versioned seed contract и текущей зафиксированной версии профилей.

## Разбиение и leakage

Manufacturer pools разделены по split:

- train — synthetic.vektor_electro, synthetic.sever_kabel, synthetic.ural_switchgear;
- dev — synthetic.volga_controls, synthetic.sibir_line;
- test — synthetic.neva_electro, synthetic.taiga_power.

Test manufacturers отсутствуют в train и dev. Валидатор запрещает повторение:

- item ID;
- item hash;
- source-record ID;
- полного surface text;
- test manufacturer в train/dev.

Все split используют одно семейство синтетического генератора. Этот generator bias раскрыт явно и не считается устранённым независимой разметкой.

## Метрики

Runner рассчитывает:

- category precision и recall;
- attribute precision и recall по ожидаемым issue attributes;
- false EXACT rate на hard-negative;
- false ANALOG rate на hard-negative;
- recall критических несовпадений;
- review rate;
- outcome accuracy;
- отдельные unseen-manufacturer и OCR-срезы;
- per-profile metrics.

В synthetic contract replay ожидаются:

- category precision — 1,0;
- category recall — 0,8;
- attribute precision/recall — 1,0;
- false EXACT и false ANALOG на hard-negative — 0;
- critical mismatch recall — 1,0;
- review rate — 1,0.

Эти числа показывают только соответствие генератора и matcher-контракта. Они не являются метриками качества на реальных документах.

## Release gates

Жёсткие пороги:

- category precision ≥ 0,98;
- category recall ≥ 0,75;
- attribute precision и recall ≥ 0,98;
- false EXACT = 0;
- false ANALOG ≤ 0,01;
- critical mismatch recall ≥ 0,99;
- review rate ≥ 0,95;
- отсутствие точного leakage;
- наличие 15 pack roots;
- независимая приёмка каждого профиля.

Synthetic metric gates и leakage gates проходят. Общий release gate остаётся заблокированным, потому что независимая приёмка не выполнена.

## Независимая приёмка

Для каждого профиля предусмотрена отдельная запись acceptance:

- primary annotator;
- acceptance annotator;
- дата;
- acceptance hash;
- объём аудита;
- disagreement rate;
- rationale.

Условия принятия:

- аннотаторы различаются;
- ни один из них не является actor синтетического генератора;
- проверено не менее 32 элементов;
- disagreement rate не выше 0,02;
- сохранён acceptance hash и содержательное rationale.

Текущий статус:

- accepted profiles — 0;
- pending profiles — 15;
- independent acceptance complete — false;
- shadow-runtime promotion allowed — false.

Реальные эксперты и результаты проверки не выдумывались.

## Проверка

```bash
python schemas/categories/electrical/validate_truth_packs.py
python schemas/categories/electrical/truth_pack_runner.py
python -m pytest -q tests/test_arv067h_truth_packs.py
python -m pytest -q tests/test_arv067h_release_report.py
python -m compileall -q \
  schemas/categories/electrical/truth_pack_generator.py \
  schemas/categories/electrical/truth_pack_runner.py \
  schemas/categories/electrical/validate_truth_packs.py \
  tests/test_arv067h_truth_packs.py \
  tests/test_arv067h_release_report.py
```

Ожидаемый маркер:

```text
ARV-067H truth packs: OK (profiles=15, items=2400, positive=1500, hard_negative=900, ocr=510, unseen_manufacturer=600, fixture_cases=25, independent_acceptance=false, release=BLOCKED, runtime_import=false)
```

## Границы

- синтетические labels не являются независимой человеческой истиной;
- одинаковое семейство генератора во всех split создаёт раскрытый generator bias;
- OCR extraction accuracy не измеряется;
- production accuracy claims запрещены;
- production resolver, БД и миграции не меняются;
- runtime import отключён;
- ARV-067I может подключать только shadow runtime и только после независимой приёмки и повторного release review.

Маркеры результата:

- `ARV-067H_TRUTH_PACK_CONTRACT_READY`
- `ARV-067H_2400_REPRODUCIBLE_ITEMS_READY`
- `ARV-067H_EXACT_LEAKAGE_GATES_READY`
- `ARV-067H_METRICS_AND_SLICES_READY`
- `ARV-067H_INDEPENDENT_ACCEPTANCE_PENDING`
- `ARV-067H_SHADOW_RUNTIME_BLOCKED`
- `ARV-067H_PRODUCTION_RUNTIME_NOT_WIRED`
