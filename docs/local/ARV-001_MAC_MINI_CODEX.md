# ТЗ для Codex — ARV-001 local real golden-report acceptance

## 0. Назначение

Провести на Mac mini финальную локальную приёмку ARV-001 на одном реальном,
разрешённом к использованию отчёте. Репозиторный quality-gate уже должен быть в
`main`. Локальная задача не разрабатывает новую функциональность и не меняет
R9/R10.1 contracts. Она создаёт проверяемое доказательство и отдельный follow-up
PR только с санитизированным freeze manifest и acceptance note.

## 1. Жёсткие границы

Запрещено:

- коммитить исходные документы, текст закупки, номер закупки, название клиента,
  полный PDF, canonical JSON, source graph, provider response/body, логи, токены,
  cookie, DSN, приватные пути или ФИО проверяющих;
- ослаблять `quality_gates/arv001/policy.json`, менять пороги, исключать проваленные
  поля, переводить missing truth в `0` или редактировать output вручную;
- менять frozen R9 producer, R10.1 producer, source graph, persistence, PDF
  renderer, provider policy, tokenizer/profile, Redis/PostgreSQL runtime;
- выполнять подачу, ЭЦП, вход в кабинет, отправку поставщикам или любые внешние
  действия;
- повторять provider run автоматически либо подбирать другой provider/model,
  чтобы получить PASS;
- останавливать или перенастраивать посторонние локальные сервисы. Порт 8090 и
  embeddings-сервис не трогать.

Любое реальное содержимое остаётся только в утверждённом приватном каталоге вне
Git. В Git допускаются только псевдоним кейса, агрегированные числа, reason codes,
SHA-256 и итоговый manifest.

## 2. Preflight

```bash
cd /Users/master/Documents/AI-Corporation-live
git fetch origin --tags --prune
git switch main
git pull --ff-only
git status --short --branch
git rev-parse HEAD
```

Рабочее дерево должно быть чистым. Убедиться, что в `main` присутствуют:

```bash
test -f quality_gates/arv001/policy.json
test -f quality_gates/arv001/review.schema.json
test -f quality_gates/arv001/freeze_manifest.schema.json
test -f quality_gates/arv001/evaluate.py
```

Создать отдельную ветку только для локального доказательства:

```bash
git switch -c codex/arv-001-local-acceptance
```

## 3. Проверка репозиторного контура

Использовать существующее поддерживаемое Python 3.11 окружение либо создать
одноразовое вне репозитория.

```bash
python3.11 -m venv /tmp/arvectum-arv001-venv
source /tmp/arvectum-arv001-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

python quality_gates/arv001/evaluate.py validate-package
python -m pytest -q tests/quality/test_arv001_quality_gate.py
python -m ruff check quality_gates/arv001/evaluate.py tests/quality/test_arv001_quality_gate.py
python -m compileall -q quality_gates/arv001
```

Любое падение сначала зафиксировать. Не чинить его ослаблением тестов или gate.
Если найден настоящий дефект реализации ARV-001, остановить real acceptance и
подготовить отдельный минимальный fix commit с regression test.

## 4. Проверка зависимости ARV-003

До выбора кейса подтвердить наличие принятого real-provider результата R10.1:

- exact customer/project/case/run identity проверена;
- controlled run завершён по действующему runbook;
- output прошёл grounding/schema/budget checks;
- canonical output и final report являются immutable artifacts;
- source graph доступен для независимой проверки;
- provider/model/profile зафиксированы без секретов;
- нет незакрытого Gate 5 failure.

Если такого результата нет, остановиться без импровизации и вернуть:

```text
ARV-001_LOCAL_ACCEPTANCE_BLOCKED=ARV-003_REAL_OUTPUT_NOT_ACCEPTED
```

Не создавать фиктивный real review и не менять `evidence_class` synthetic → real.

## 5. Приватный рабочий каталог

Создать каталог вне Git, например:

```bash
CASE_ALIAS="arv001-real-001"
PRIVATE_ROOT="/Users/master/.local/share/arvectum/arv001/${CASE_ALIAS}"
mkdir -p "$PRIVATE_ROOT"
chmod 700 "$PRIVATE_ROOT"
```

В каталоге должны находиться или быть доступны read-only:

- утверждённый source bundle;
- canonical output;
- source graph;
- final PDF/report;
- ручной truth pack;
- отдельные review sheets двух проверяющих.

`CASE_ALIAS` не должен содержать клиента, номер закупки или иные идентификаторы.

## 6. Immutable hashes

Вычислить SHA-256 без копирования содержимого в репозиторий:

```bash
shasum -a 256 <SOURCE_BUNDLE_ARCHIVE_OR_MANIFEST>
shasum -a 256 <CANONICAL_OUTPUT_JSON>
shasum -a 256 <SOURCE_GRAPH_JSON>
shasum -a 256 <FINAL_REPORT_PDF>
```

Проверить, что hashes повторяются при втором запуске и относятся к exact current
run. Если canonical/report/source graph не связаны с одним run, фиксировать
`wrong_procurement_identity` или `artifact_binding` как Sev-1 и остановить
передачу.

## 7. Ручной truth pack

Два независимых проверяющих должны отдельно просмотреть исходники и отчёт.
Минимальные роли:

1. `operator-a` — оператор закупки;
2. `reviewer-b` — domain expert или quality reviewer.

В приватном truth pack зафиксировать:

- полный перечень критических требований;
- полный перечень критических рисков;
- все material claims отчёта;
- evidence locator каждого material claim;
- обязательные документы/таблицы и факт обработки;
- ложные критические выводы;
- system decision и reviewed decision;
- defects с severity/category/status/evidence/rationale.

Если truth pack неполон, использовать `null` + `missing_truth_reason`; не ставить
ноль. Такой кейс закономерно получит `NOT_READY`.

## 8. Санитизированный review JSON

Создать только в `$PRIVATE_ROOT/arv001-review.json` по
`quality_gates/arv001/review.schema.json`.

Обязательные правила:

- `evidence_class = "real"` только после шага 4;
- `stage = "initial_freeze"`;
- `case_ref = CASE_ALIAS`;
- никаких цитат и текста исходников;
- reviewer `subject` — устойчивый псевдоним, не ФИО/e-mail;
- все counts выводятся из truth pack, а не из system output;
- `positive_inputs_supported=true` только когда квалификационные и коммерческие
  основания положительного решения подтверждены;
- automatic-fail defect нельзя закрывать как `accepted_risk`.

Проверить JSON Schema:

```bash
python - <<'PY'
import json
from pathlib import Path
import jsonschema
schema = json.loads(Path('quality_gates/arv001/review.schema.json').read_text())
review = json.loads(Path('${PRIVATE_ROOT}/arv001-review.json').read_text())
jsonschema.validate(review, schema)
print('ARV-001 review schema: OK')
PY
```

Если shell не подставляет `${PRIVATE_ROOT}` внутри heredoc, передать путь через
переменную окружения или аргумент; не копировать файл в repo.

## 9. Двойная детерминированная оценка

```bash
python quality_gates/arv001/evaluate.py evaluate \
  "$PRIVATE_ROOT/arv001-review.json" \
  --output "$PRIVATE_ROOT/evaluation-1.json"

python quality_gates/arv001/evaluate.py evaluate \
  "$PRIVATE_ROOT/arv001-review.json" \
  --output "$PRIVATE_ROOT/evaluation-2.json"

cmp "$PRIVATE_ROOT/evaluation-1.json" "$PRIVATE_ROOT/evaluation-2.json"
shasum -a 256 "$PRIVATE_ROOT/evaluation-1.json" "$PRIVATE_ROOT/evaluation-2.json"
```

Допустимый финальный результат для freeze — только `PASS`. `CONDITIONAL`,
`FAIL` и `NOT_READY` не преобразовывать вручную. Сначала устранить причину через
новый run/review или зафиксировать блокер.

## 10. Freeze manifest

Только после `PASS`:

```bash
FROZEN_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python quality_gates/arv001/evaluate.py freeze \
  "$PRIVATE_ROOT/arv001-review.json" \
  --frozen-at "$FROZEN_AT" \
  --approval-id "arv001-local-acceptance-001" \
  --output "$PRIVATE_ROOT/arv001-freeze-manifest.json"
```

Проверить schema и повторяемость freeze с теми же аргументами. Затем вручную
просмотреть manifest: в нём не должно быть исходного номера закупки, клиента,
ФИО, путей, текста документов или provider data.

## 11. Санитизированное доказательство в Git

Создать:

```text
evidence/arv001/arv001-real-001/freeze_manifest.json
evidence/arv001/arv001-real-001/acceptance.md
```

Копировать в Git только проверенный manifest. `acceptance.md` должен содержать:

- base SHA;
- policy version и policy SHA-256;
- case alias;
- producer mode;
- verdict;
- metric values без raw facts;
- open defect counts;
- число независимых reviewers;
- decision agreement/adjudication state;
- exact artifact hashes из manifest;
- команды и результаты;
- подтверждение отсутствия raw/customer/provider/private-path данных;
- completion markers.

Перед commit:

```bash
git diff --check
grep -R "/Users/\|postgresql://\|redis://\|Bearer \|BEGIN .*PRIVATE KEY" \
  evidence/arv001 && exit 1 || true
python scripts/ops/secret_scan.py
```

Не добавлять private review/evaluation files.

## 12. Проверки перед PR

```bash
python quality_gates/arv001/evaluate.py validate-package
python -m pytest -q tests/quality/test_arv001_quality_gate.py
make check
make test
alembic heads
python scripts/ops/secret_scan.py
git status --short
git diff --check
```

## 13. Commit, push, PR

```bash
git add evidence/arv001/arv001-real-001/freeze_manifest.json \
  evidence/arv001/arv001-real-001/acceptance.md
git commit -m "evidence(arv001): freeze accepted real golden report"
git push -u origin codex/arv-001-local-acceptance
```

Открыть Draft PR в `main`, связать с issue `#87`, не merge-ить самостоятельно.
PR body должен явно содержать:

```text
ARV-001_REAL_GOLDEN_REPORT_ACCEPTED
ARV-001_FREEZE_MANIFEST_CREATED
ARV-001_GATE_FROZEN
ARV-001_RAW_CUSTOMER_DATA_NOT_COMMITTED
ARV-001_SOURCE_GRAPH_UNCHANGED
ARV-001_EXTERNAL_EXECUTION_NOT_PERFORMED
```

Дождаться exact-head GitHub Actions. Не скрывать падения и не снижать assertions.

## 14. Финальный отчёт Codex

Вернуть одним сообщением:

1. base SHA и branch;
2. full remote head SHA;
3. Draft PR URL/state;
4. changed files и diffstat;
5. ARV-003 dependency evidence status без секретов;
6. producer mode;
7. policy version/SHA-256;
8. review schema validation;
9. counts и рассчитанные metrics;
10. verdict и reason codes;
11. два evaluation SHA-256 и подтверждение byte identity;
12. freeze manifest SHA-256;
13. reviewer count/roles и adjudication state без ФИО;
14. defect counts по severity;
15. artifact hashes;
16. focused tests, `make check`, `make test`, Alembic, secret scan;
17. exact-head Actions run/jobs/conclusions;
18. review threads/unresolved count;
19. privacy grep result;
20. confirmation: raw data/provider body/private paths were not committed;
21. completion markers либо один точный BLOCKED marker.

После отчёта остановиться. Не переходить к ARV-004/005/067/072.
