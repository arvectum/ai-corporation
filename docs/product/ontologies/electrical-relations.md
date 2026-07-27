# ARV-067C — отношения совместимости, комплектности и замены

Статус: `RESEARCH_ASSET_READY`.

## Назначение

ARV-067C добавляет отдельный версионируемый слой отношений поверх дерева
категорий ARV-067B. Он не смешивает характеристики изделия, состав комплекта,
совместимость, замену, операторский допуск и нормативную применимость.

Граф остаётся offline research asset и не подключён к production resolver.

## Типы отношений

Зарегистрированы девять типов:

- `part_of` — структурная принадлежность;
- `accessory_for` — принадлежность или элемент комплектности;
- `compatible_with` — симметричная совместимость;
- `requires` — функциональная или комплектная зависимость;
- `replaces` — направленная замена конкретной модели или исполнения;
- `alternative_to` — возможная проектная альтернатива;
- `not_compatible_with` — подтверждённая несовместимость;
- `approved_for` — операторский или договорный допуск;
- `governed_by` — потенциальная нормативная применимость.

Совместимость и альтернативность не являются транзитивными. Ни один тип не
имеет semantics полной эквивалентности.

`accessory_for` не означает `compatible_with`: наличие элемента в типовом
комплекте не доказывает совместимость конкретных марок и исполнений.

## Конечные точки

Версия 1.0.0 разрешает три активных типа конечных точек:

- `category` — стабильный узел ARV-067B;
- `component_role` — абстрактная компонентная роль ARV-067C;
- `normative_document` — документ из metadata-only реестра ARV-067.

Тип `catalog_entity` зарезервирован для ARV-067E. До появления реестра
изготовителей, серий, моделей и исполнений любые такие assertions отклоняются
валидатором. Поэтому `replaces` и `approved_for` уже типизированы, но
экземпляры этих отношений в v1 намеренно отсутствуют.

## Компонентные роли

Зарегистрированы три роли:

- расцепитель модульного автоматического выключателя;
- токовый измерительный вход устройства РЗА;
- вход измерения напряжения устройства РЗА.

Это не модели производителей и не новые товарные категории. Роль описывает
место компонента в конфигурации и набор характеристик, которые нужно
проверить.

## Assertions

В v1 содержится 25 объяснимых assertions для четырёх обязательных областей:

1. кабели, муфты, наконечники и трубы;
2. СИП и линейная арматура;
3. выключатели и расцепители;
4. РЗА, трансформаторы тока/напряжения и измерительные входы.

Дополнительно добавлены metadata-связи `governed_by` с действующим
нормативным реестром.

Каждый assertion содержит:

- стабильный `assertion_id`;
- тип и две зарегистрированные конечные точки;
- hard/soft strength;
- условия применимости;
- outcome при провале условия;
- evidence и locator;
- provenance и review status;
- reason codes;
- decision ceiling;
- обязательность ручной проверки.

## Decision ceiling

Категорийная совместимость — только шаблон проверки. Даже при выполнении всех
условий она не поднимается выше `CONDITIONAL`, пока отсутствуют конкретные
серия, модель, исполнение и документы изготовителя.

Структурные связи детального профиля, например `part_of` для функции
расцепления модульного автомата, могут иметь результат `SUPPORTED`, поскольку
они описывают класс изделия, а не совместимость двух продаваемых моделей.

Отсутствие assertion или обязательного evidence всегда возвращает
`UNCERTAIN`. Никакая связь не достраивается по догадке.

## Результаты evaluator

Offline evaluator возвращает:

- `SUPPORTED`;
- `CONDITIONAL`;
- `NOT_COMPATIBLE`;
- `CONFLICT`;
- `UNCERTAIN`.

Для каждого результата сохраняются relation IDs, reason codes и флаг
`requires_review`.

Симметричные отношения хранятся один раз в каноническом порядке, но
разрешаются в обоих направлениях. Направленные отношения не разворачиваются.

## Защитные проверки

Валидатор проверяет:

- закрытые JSON Schema;
- наличие всех девяти типов;
- направленность, симметрию, transitivity policy и cardinality;
- существование категорий, компонентных ролей, характеристик и нормативных
  документов;
- канонический порядок симметричных пар;
- дубли type/pair/scope;
- конфликты `compatible_with` / `not_compatible_with`;
- циклы `replaces`;
- обязательный evidence и provenance;
- category-level decision ceiling;
- отсутствие product assertions до ARV-067E;
- 26 fixture cases;
- отсутствие production import.

## Проверка

```bash
python schemas/categories/electrical/validate_relations.py
python -m pytest -q tests/test_arv067c_relations.py
python -m compileall -q \
  schemas/categories/electrical/relation_evaluator.py \
  schemas/categories/electrical/relation_validation_contract.py \
  schemas/categories/electrical/relation_validation_assertions.py \
  schemas/categories/electrical/validate_relations.py \
  tests/test_arv067c_relations.py
```

Ожидаемый маркер:

```text
ARV-067C relation graph: OK (types=9, components=3, assertions=25, fixtures=26, runtime_import=false)
```

## Границы

- production resolver не изменён;
- БД и миграции не изменены;
- модели производителей не добавлены;
- операторский допуск не наследуется от категории;
- нормативная связь не является заключением о соответствии;
- замена модели не разрешается без ARV-067E;
- отсутствие evidence означает `UNCERTAIN`;
- shadow runtime не подключён.

Маркеры результата:

- `ARV-067C_RELATION_GRAPH_READY`
- `ARV-067C_COMPATIBILITY_AND_COMPLETENESS_SEPARATED`
- `ARV-067C_MISSING_EVIDENCE_RETURNS_UNCERTAIN`
- `ARV-067C_REPLACEMENT_AND_APPROVAL_BLOCKED_UNTIL_ARV067E`
- `ARV-067C_PRODUCTION_RUNTIME_NOT_WIRED`
