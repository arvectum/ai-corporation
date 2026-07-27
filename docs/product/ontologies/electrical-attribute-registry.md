# ARV-067A — единый реестр характеристик электротехнической онтологии

Статус: `RESEARCH_ASSET_READY`.

## Назначение

Реестр устраняет независимое определение одних и тех же характеристик в разных категориях. Он задаёт стабильные IDs, типы значений, единицы, компараторы, semantic roles, алиасы, происхождение и уровень зрелости.

Реестр остаётся offline research asset и не подключён к production resolver.

## Уровни зрелости

- `verified_detailed_profile` — определение подтверждено исходным детальным профилем `electrical.v1.yaml` либо контрактом волны ARV-067D с fixture gate и обязательным human review;
- `provisional_taxonomy` — характеристика присутствует как discriminator в `nomenclature.v1.yaml`, но её типизация пока является инженерной гипотезой и не может использоваться как production match rule;
- `deprecated` — сохранённый для обратной совместимости ID, который нельзя использовать в новых профилях.

## Основные правила

1. Один физический смысл получает один стабильный attribute ID.
2. Ролевые характеристики не объединяются автоматически. Например, `primary_voltage_kv`, `secondary_voltage_kv`, `coil_voltage_v` и `rated_operational_voltage_v` остаются разными полями.
3. Единицы преобразуются только внутри одной физической размерности.
4. Comparator должен поддерживать value type характеристики.
5. `minimum` означает минимальную способность кандидата; `maximum` — максимально допустимое значение; `contains` — включение множества; `range_overlap` — пересечение диапазонов.
6. Profile-level comparator может отличаться от canonical default только с явным обоснованием `profile_requirement_semantics`.
7. Перевод `provisional_taxonomy` в verified-состояние требует источника, категории, fixture cases и human review.
8. Наличие характеристики в реестре не является доказательством соответствия ГОСТ, СТО, требованиям заказчика или операторскому реестру.

## Структура

Основной файл `attribute_registry.v1.yaml` содержит:

- реестр единиц и коэффициенты преобразования;
- контракт компараторов;
- общие value sets;
- список версионированных фрагментов характеристик;
- alias для исторического расхождения `electromechanical_contactor → electromagnetic_contactor`;
- governance boundaries.

Характеристики разделены на восемь фрагментов:

- подтверждённые четырьмя исходными детальными профилями;
- коммутационное оборудование и изоляция;
- кабели, воздушные линии и строительные элементы;
- трансформаторы и силовое оборудование;
- измерения и аккумуляторные батареи;
- автоматизация и РЗА;
- связь и системы электропитания;
- 30 характеристик детальных профилей ARV-067D.

Всего зарегистрировано 156 стабильных IDs. Из них 52 подтверждены детальными профилями, а 104 сохраняются как `provisional_taxonomy` до категорийной верификации.

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

Для verified-характеристики волны профилей provenance указывает соответствующий profile registry и `explicit_attribute_definition`.

Перед добавлением нужно проверить:

- нет ли уже характеристики с тем же смыслом;
- не является ли это ролью существующей характеристики;
- подходит ли единица и размерность;
- допустим ли comparator для value type;
- требуется ли value set;
- есть ли источник и статус верификации;
- существуют ли fixture cases и human-review gate.

## Проверка

```bash
python schemas/categories/electrical/validate_attributes.py
python schemas/categories/electrical/validate_wave1_profiles.py
python -m pytest -q tests/test_arv067a_attribute_registry.py
python -m pytest -q tests/test_arv067d_wave1_profiles.py
```

Ожидаемый маркер:

```text
ARV-067A attribute registry: OK (..., runtime_import=false)
```

## Границы

- production runtime не изменён;
- БД и миграции не изменены;
- profile bindings остаются offline overlays;
- provisional-типизация не является нормативным утверждением;
- verified profile attribute не является доказательством эквивалентности моделей;
- автоматическое заключение о соответствии остаётся отключённым.

Маркеры результата:

- `ARV-067A_ATTRIBUTE_REGISTRY_READY`
- `ARV-067A_ALL_TAXONOMY_DISCRIMINATORS_REGISTERED`
- `ARV-067A_DETAILED_PROFILE_CONTRACT_PRESERVED`
- `ARV-067A_PRODUCTION_RUNTIME_NOT_WIRED`
