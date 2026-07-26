# ARV-072 — регрессионный competitive benchmark

Дата решения: 2026-07-26.

Статус подготовки: `ARV_072_BENCHMARK_PROTOCOL_READY`.

Статус live-прогона: `BLOCKED_BY_DEPENDENCIES_AND_AUTHORIZED_ACCESS`.

Связанная задача: GitHub issue #41.

## Цель

Сравнить Arvectum и не менее пяти продуктов на одном наборе из пяти реальных
закупок. Сравниваются не маркетинговые заявления, а извлечение, доказуемость,
решение, время, стоимость и документы.

## Почему benchmark разделён на два слоя

### 1. Public-surface snapshot

Фиксирует только наблюдаемые на официальных сайтах режим доступа, опубликованные
функции, тарифы и ограничения. Это помогает выбрать cohort и спланировать
доступ, но не даёт права ставить баллы за качество.

### 2. Controlled live-output benchmark

Каждый продукт получает одну и ту же закупку, одинаковый supplier context и
одинаковый набор вопросов. Входы фиксируются SHA-256. Для каждого product/case
делаются два запуска, если это допускает официальный интерфейс и тариф.

## Cohort

В registry включены Arvectum, ТЕНвизор, TendRO, два разных продукта с названием
TenderAI, Zakupki Assistant, Semantor и ТендерФорум. В итоговый рейтинг попадают
только продукты, способные обработать одинаковый вход. Остальные сохраняются в
таблице как `not_comparable`.

## Набор закупок

Выбраны пять разных типов сложности:

1. unit-price service table;
2. многопозиционная электротехническая поставка;
3. сложная система медицинского оборудования;
4. долгосрочные IT-услуги;
5. модернизация государственной информационной системы.

Наличие номера в manifest не означает готовность case. Перед live-run обязательны
authoritative source bundle, human-reviewed truth pack и их immutable hashes.

## Rubric

Вес измерений:

- extraction — 25;
- evidence — 25;
- decision — 20;
- time — 10;
- cost — 10;
- documents/reproducibility — 10.

Missing не равен нулю. Итоговый балл запрещён, если отсутствует больше 20%
обязательных item evidence или продукт не способен принять тот же вход.

Automatic fail применяется при неверной идентичности закупки, существенной
фабрикации, неподтверждённом положительном решении, silent source loss,
evidence mismatch, небезопасном внешнем действии или нарушении условий доступа.

## Live-run protocol

1. Заморозить source bundle, truth pack, supplier context и question set.
2. Зафиксировать дату, тариф, product version/label и mode.
3. Запускать вручную либо через официальный API; не обходить CAPTCHA и лимиты.
4. Хранить screenshot/export/timing/cost evidence вне Git.
5. В Git сохранять только hashes и redacted result.
6. Делать два запуска на product/case, не меняя вход.
7. Два reviewer независимо выставляют item scores 0–4.
8. Разногласия adjudicate до публикации.
9. Не объявлять победителя, пока нет пяти сопоставимых продуктов × пяти кейсов.
10. Любой automatic fail показывать отдельно, а не прятать в среднем балле.

## Что завершено

- создана GitHub-задача с окончательными acceptance criteria;
- зафиксирован product cohort;
- собран dated public-surface snapshot по официальным страницам;
- определены пять real-procurement cases и readiness states;
- создана rubric, result schema, validator и report template;
- исключено смешение публичных claims и live evidence.

## Подтверждённые блокеры live execution

1. В main найден controlled real-provider runner, но не найден маркер принятого
   R10.1 Gate 5 live evidence. Следовательно, Arvectum пока нельзя честно
   сравнивать как accepted production output.
2. ARV-001 release-quality gate ещё должен быть привязан к accepted ARV-003.
3. Для конкурентов нужны официальные аккаунты, trial или demo invitation.
4. Не все пять truth packs заморожены и независимо приняты.

Эти блокеры не разрешается маскировать stub-результатами, публичными demo cards,
выдуманными оценками или неавторизованным доступом.

## Следующее допустимое действие

После закрытия всех четырёх blockers выполнить live matrix, заполнить redacted
per-run results, провести двойное review/adjudication и только затем закрыть
issue #41 маркером `ARV_072_COMPETITIVE_BENCHMARK_COMPLETE`.
