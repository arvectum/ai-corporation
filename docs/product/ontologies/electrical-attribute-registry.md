# ARV-067A — единый реестр характеристик электротехнической онтологии

Статус: `RESEARCH_ASSET_READY`.

## Назначение

Реестр устраняет независимое определение одних и тех же характеристик в разных категориях. Он задаёт стабильные IDs, типы значений, единицы, компараторы, semantic roles, алиасы, происхождение и уровень зрелости.

Реестр остаётся offline research asset и не подключён к production resolver.

## Уровни зрелости

- `verified_detailed_profile` — определение перенесено без изменения типа, единицы, comparator и value-set из одного из четырёх детальных профилей `electrical.v1.yaml`;
- `provisional_taxonomy` — характеристика присутствует как discriminator в `nomenclature.v1.yaml`, но её типизация пока является инженерной гипотезой и не может использоваться как production match rule;
- `deprecated` — сохранённый для обратной совместимости ID, который нельзя использовать в новых профилях.

## Основные правила

1. Один физический смысл получает один стабильный attribute ID.
2. Ролевые характеристики не объединяются автоматически. Например, `primary_voltage_kv`, `secondary_voltage_kv`, `coil_voltage_v` и `rated_operational_voltage_v` остаются разными полями.
3. Единицы преобразуются только внутри одной физической размерности.
4. Comparator должен поддерживать value type характеристики.
5. `minimum` означает минимальную способность кандидата; `maximum` — максимально допустимое значение; `contains` — включение множества; `range_overlap` — пересечение диапазонов.
6. Перевод `provisional_taxonomy` в verified-состояние требует источника, категории, fixture cases и human review.
7. Наличие характеристики в реестре не является доказательством соответствия ГОСТ, СТО, требованиям заказчика или операторскому реестру.

## Структура

Основной файл `attribute_registry.v1.yaml` содержит:

- реестр единиц и коэффициенты преобразования;
- контракт компараторов;
- общие value sets;
- список версионированных фрагментов характеристик;
- alias для исторического расхождения `electromechanical_contactor → electromagnetic_contactor`;
- governance boundaries.

Характеристики разделены на семь фрагментов:

- подтверждённые четырьмя детальными профилями;
- коммутационное оборудование и изоляция;
- кабели, воздушные линии и строительные элементы;
- трансформаторы и силовое оборудование;
- измерения и аккумуляторные батареи;
- автоматизация и РЗА;
- связь и системы электропитания.

Всего зарегистрировано 126 стабильных IDs. Из них 22 подтверждены существующими детальными профилями, а 104 сохраняются как `provisional_taxonomy` до категорийной верификации.

## Добавление характеристики

Новая характеристика должна содержать:

```yaml
- id: stable_snake_case_id
  title_ru: Название
  value_type: number
  canonical_unit: kV
  default_comparator: minimum
  semantic_role: capability
  maturity: provisional_taxonomy
  aliases: []
  provenance:
    source_asset: nomenclature.v1.yaml
    basis: discriminator_id_and_engineering_inference
```

Перед добавлением нужно проверить:

- нет ли уже характеристики с тем же смыслом;
- не является ли это ролью существующей характеристики;
- подходит ли единица и размерность;
- допустим ли comparator для value type;
- требуется ли value set;
- есть ли источник и статус верификации.

## Проверка

```bash
python schemas/categories/electrical/validate_attributes.py
python -m pytest -q tests/test_arv067a_attribute_registry.py
```

Ожидаемый маркер:

```text
ARV-067A attribute registry: OK (..., runtime_import=false)
```

## Границы

- production runtime не изменён;
- БД и миграции не изменены;
- новые категории не активированы для production matching;
- provisional-типизация не является нормативным утверждением;
- автоматическое заключение о соответствии остаётся отключённым.

Маркеры результата:

- `ARV-067A_ATTRIBUTE_REGISTRY_READY`
- `ARV-067A_ALL_TAXONOMY_DISCRIMINATORS_REGISTERED`
- `ARV-067A_DETAILED_PROFILE_CONTRACT_PRESERVED`
- `ARV-067A_PRODUCTION_RUNTIME_NOT_WIRED`
