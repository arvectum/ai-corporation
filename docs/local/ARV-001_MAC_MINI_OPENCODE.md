# ТЗ для OpenCode — ARV-001: локальная приёмка и заморозка golden report

## 0. Цель

Завершить локальную часть ARV-001 на Mac mini: взять один уже принятый реальный
R10.1-отчёт из завершённого контура ARV-003, провести независимую ручную проверку,
рассчитать метрики действующим fail-closed evaluator, получить только честный
`PASS`, создать детерминированный freeze manifest и открыть Draft PR с исключительно
санитизированными доказательствами.

Это не задача разработки нового анализа. Запрещено повторно вызывать модель,
перегенерировать отчёт, менять пороги или исправлять исходный результат ради PASS.

## 1. Подтверждённая исходная точка

ARV-003 Gate 5 принят в issue `#68` на merged commit:

```text
f96e3588f2e1f61f19d54e44594debfc81a745bf
```

Принятый санитизированный результат ARV-003:

```text
ARV003_GATE5_CONTROLLED_ACCEPTANCE_COMPLETE
manifest_version=r10.1-controlled-provider-evidence-v3
executions=2
repeat_identity_verified=true
batch_count=25
accepted_claims=20
rejected_claims=0
unsupported_claims=0
retries=0
raw_provider_response_stored=false
external_cost=0
```

Локально существует controlled evidence bundle с двумя завершёнными execution и
санитизированным `controlled-evidence.manifest.json`. Его нужно найти и проверить
read-only. Нельзя создавать замену, подменять его synthetic fixture или выполнять
новый provider run.

Репозиторная часть ARV-001 уже находится в `main` через PR `#88` и включает:

- `quality_gates/arv001/policy.json`;
- `quality_gates/arv001/review.schema.json`;
- `quality_gates/arv001/freeze_manifest.schema.json`;
- `quality_gates/arv001/evaluate.py`;
- `tests/quality/test_arv001_quality_gate.py`.

## 2. Абсолютные запреты

Запрещено:

- выполнять provider/LLM/VLM/OCR generation calls, повторять ARV-003 controlled
  runner или запускать локальную модель;
- менять принятый ARV-003 bundle, execution directories, immutable snapshot,
  source graph, canonical output или отчёт;
- менять R9/R10.1 producer, report renderer, persistence, provider policy,
  tokenizer/profile, Redis/PostgreSQL/Colima/Docker runtime;
- ослаблять `quality_gates/arv001/policy.json`, schema, assertions или тесты;
- превращать `null`/missing truth в `0`, придумывать truth pack, дефекты, review
  или подписи проверяющих;
- считать OpenCode одним из двух независимых human reviewers;
- выполнять прямые `INSERT`, `UPDATE`, `DELETE`, DDL, миграции или repair в БД;
- коммитить номер закупки, клиента, run/customer/project/case IDs, исходные
  документы, их текст, evidence quotes, canonical JSON, source graph, полный
  отчёт, provider body, логи, API key, DSN, cookie, токены, ФИО, email или
  приватные абсолютные пути;
- выполнять подачу заявки, ЭЦП, вход в кабинет, письма поставщикам и любые иные
  внешние действия;
- использовать `--no-verify`, force-push или самостоятельно merge-ить PR;
- переходить к ARV-004, ARV-005, ARV-067 или ARV-072.

Все реальные материалы и review sheets остаются в приватном каталоге вне Git.
В Git разрешены только псевдоним кейса, агрегированные метрики, reason codes,
SHA-256 и итоговый freeze manifest.

## 3. Изолированный Git worktree

Не использовать рабочее дерево с ранее существовавшими untracked-файлами.
Создать отдельный worktree от свежего `origin/main`.

```bash
REPO="/Users/master/Documents/AI-Corporation-live"
WORKTREE="/Users/master/Documents/AI-Corporation-arv001-opencode"
BRANCH="opencode/arv-001-local-acceptance"

cd "$REPO"
git fetch origin --tags --prune
git switch main
git pull --ff-only
BASE_SHA="$(git rev-parse HEAD)"
git merge-base --is-ancestor \
  f96e3588f2e1f61f19d54e44594debfc81a745bf \
  "$BASE_SHA"

if [ -e "$WORKTREE" ]; then
  echo "ARV-001_BLOCKED=WORKTREE_ALREADY_EXISTS"
  exit 2
fi

git worktree add -b "$BRANCH" "$WORKTREE" origin/main
cd "$WORKTREE"
git status --short --branch
git rev-parse HEAD
```

После создания worktree tracked tree должен быть чистым.

## 4. Репозиторный preflight

Проверить обязательные файлы:

```bash
test -f quality_gates/arv001/policy.json
test -f quality_gates/arv001/review.schema.json
test -f quality_gates/arv001/freeze_manifest.schema.json
test -x quality_gates/arv001/evaluate.py
test -f tests/quality/test_arv001_quality_gate.py
```

Использовать существующее поддерживаемое Python 3.11 окружение. Если его нет,
создать временное вне репозитория:

```bash
python3.11 -m venv /tmp/arvectum-arv001-opencode-venv
source /tmp/arvectum-arv001-opencode-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Запустить:

```bash
python quality_gates/arv001/evaluate.py validate-package
python -m pytest -q tests/quality/test_arv001_quality_gate.py
python -m ruff check \
  quality_gates/arv001/evaluate.py \
  tests/quality/test_arv001_quality_gate.py
python -m compileall -q quality_gates/arv001
```

Если проверка падает, не переходить к реальному кейсу. Не чинить gate ослаблением.
Вернуть:

```text
ARV-001_BLOCKED=REPOSITORY_GATE_FAILED:<sanitized_code>
```

## 5. Найти и подтвердить принятый ARV-003 bundle

Проверка выполняется read-only. Сначала определить фактический `ARVECTUM_DATA_DIR`
через существующую локальную конфигурацию, не печатая секреты и DSN. Учесть, что
runtime/data могут находиться на `/Volumes/ArvectumSSD`.

Найти ровно один accepted bundle, содержащий:

```text
controlled-evidence.manifest.json
execution-1/
execution-2/
```

Не использовать исторические `.partial.*`, failed-run каталоги, synthetic probes,
fixtures или bundle без принятого manifest.

Проверить manifest локально без публикации его содержимого. Минимальные условия:

- `manifest_version == "r10.1-controlled-provider-evidence-v3"`;
- `repeat_count == 2`;
- `repeat_identity_verified == true`;
- обе execution имеют `status == "success"`;
- обе execution имеют `canonical_input_eligible == true`;
- `stable_identity.provider`, `model`, `request_id`, `evidence_packet_hash`,
  `batch_plan_hash`, `corpus_evidence_hash`, `grounded_claims_hash`,
  `source_graph_hash` и `production_model_hash` присутствуют;
- `batch_count == 25` и ordered batch identities совпадают между execution;
- суммарно зафиксированы 20 accepted claims, 0 rejected claims;
- `retry_count == 0`;
- usage и latency присутствуют;
- `raw_response_stored == false`;
- safety flags подтверждают отсутствие credentials, raw tender text, raw provider
  body, evidence quotes и local paths;
- вычисленный SHA-256 manifest совпадает с его собственным `manifest_hash` по
  репозиторному алгоритму;
- bundle относится к accepted outcome issue `#68`, а не к старому failed run.

Проверить SHA-256 файлов, перечисленных в publication summary обеих execution.
Не выводить actual run/customer/procurement identities в терминальный отчёт,
PR или GitHub.

Если принятый bundle не найден или проверка не проходит, остановиться:

```text
ARV-001_LOCAL_ACCEPTANCE_BLOCKED=ARV-003_ACCEPTED_BUNDLE_NOT_VERIFIED
```

## 6. Приватный рабочий каталог

Использовать нейтральный псевдоним:

```bash
CASE_ALIAS="arv001-real-001"
```

Предпочтительно создать каталог на смонтированном SSD, иначе в пользовательском
private data directory:

```bash
if [ -d /Volumes/ArvectumSSD ]; then
  PRIVATE_ROOT="/Volumes/ArvectumSSD/Arvectum/private/arv001/${CASE_ALIAS}"
else
  PRIVATE_ROOT="$HOME/.local/share/arvectum/arv001/${CASE_ALIAS}"
fi
mkdir -p "$PRIVATE_ROOT"
chmod 700 "$PRIVATE_ROOT"
```

Не печатать `PRIVATE_ROOT` в GitHub/PR. Внутри него разрешено хранить:

- read-only ссылки или копии exact accepted artifacts;
- private source-bundle manifest;
- manual truth pack;
- два независимых human review sheet;
- sanitized review JSON;
- evaluation outputs;
- freeze candidates;
- локальный журнал команд без raw content и secrets.

## 7. Выбрать один exact golden-report candidate

ARV-001 замораживает один отчёт, а не абстрактную пару execution.
Детерминированно выбрать `execution-1` из принятого ARV-003 bundle после проверки
repeat identity обеих execution.

Использовать exact artifacts `execution-1`:

- canonical output, прошедший ARV-003 verification;
- source graph, связанный с тем же run;
- human-readable report artifact, который реально будут читать reviewers;
- принятый source corpus/snapshot.

Для `report_sha256` использовать существующий human-readable report artifact из
accepted execution. Предпочтительно `report.html`, если именно он создан bundle.
Использовать PDF только если PDF уже является принятым artifact этого же run.
Не генерировать новый PDF и не пересобирать отчёт ради ARV-001.

### 7.1. Private source-bundle manifest

Если accepted runtime уже содержит immutable source-bundle/snapshot manifest,
использовать его exact bytes. Если отдельного файла нет, создать **только вне Git**
детерминированный private manifest из read-only accepted corpus:

- aliases `doc-001`, `doc-002`, ... в canonical source order;
- SHA-256 exact source bytes или принятого immutable document representation;
- размер в байтах;
- document order;
- ordered chunk hashes/counts;
- corpus evidence hash и immutable snapshot hash из accepted contour.

Manifest не должен содержать имена файлов, номер закупки, клиента, IDs, текст или
пути. Сериализация: UTF-8 JSON, `sort_keys=true`, separators `(',', ':')`, newline.

`source_bundle_sha256` — SHA-256 exact bytes этого private manifest либо уже
существующего immutable source-bundle manifest. Сам файл в Git не коммитить.

### 7.2. Artifact hashes

Вычислить дважды и сравнить:

```bash
shasum -a 256 <PRIVATE_SOURCE_BUNDLE_MANIFEST>
shasum -a 256 <EXECUTION_1_CANONICAL_OUTPUT>
shasum -a 256 <EXECUTION_1_SOURCE_GRAPH>
shasum -a 256 <EXECUTION_1_HUMAN_READABLE_REPORT>
```

Проверить связь exact artifacts с одним accepted execution через ARV-003 manifest,
publication hashes и source-graph identity. При несовпадении остановиться:

```text
ARV-001_BLOCKED=ARTIFACT_BINDING_MISMATCH
```

## 8. Manual truth pack и два независимых human review

OpenCode может подготовить структуру review sheet, посчитать hashes и свести
завершённые оценки. OpenCode не является human reviewer и не вправе самостоятельно
заполнить truth judgments.

Нужны минимум два разных человека:

1. `operator-a`, роль `operator`;
2. `reviewer-b`, роль `domain_expert` или `quality_reviewer`.

Они должны независимо ознакомиться с одним и тем же source bundle, source graph,
canonical output и human-readable report. Каждый sheet хранится только в
`$PRIVATE_ROOT` и включает:

- подтверждение identity/binding;
- полный перечень критических требований;
- полный перечень критических рисков;
- перечень material claims отчёта;
- наличие и валидность evidence locator каждого material claim;
- обязательные документы/таблицы и факт обработки;
- false critical findings;
- system decision и reviewed decision;
- defects: severity, category, status, evidence reference, rationale;
- дату и alias reviewer без ФИО/email.

Reviewers не должны видеть sheet друг друга до завершения собственных оценок.
После завершения OpenCode сравнивает sheets. При разногласии требуется третий
человек `adjudicator-c`, отличный от обоих reviewers, с письменным rationale.

Если двух реально независимых завершённых human reviews нет, остановиться:

```text
ARV-001_WAITING_FOR_TWO_HUMAN_REVIEWS
```

Нельзя дублировать один review под двумя aliases.

## 9. Сформировать private truth pack

После получения human sheets создать private consolidated truth pack с
append-only ссылками на оба исходных sheet. Зафиксировать:

- `mandatory_total`, `processed_total`, `silent_losses`;
- `material_total`, `evidence_supported`, `valid_locators`,
  `evidence_mismatches`;
- critical requirements `truth_total` и `supported_found`;
- critical risks `truth_total` и `supported_found`;
- critical findings `system_total` и `false_positive_total`;
- safety gates;
- system/reviewed decision и agreement;
- `positive_inputs_supported`;
- defects;
- reviewer aliases/roles/completion;
- adjudication, если она потребовалась.

Если полный truth для requirements или risks отсутствует, использовать
`truth_total=null`, `supported_found=null` и непустой `missing_truth_reason`.
Не использовать ноль вместо неизвестного. Это корректно приведёт к `NOT_READY`.

## 10. Санитизированный review JSON

Создать только в:

```text
$PRIVATE_ROOT/arv001-review.json
```

Строго по `quality_gates/arv001/review.schema.json`:

```text
schema_version=arv001-review-v1
task_id=ARV-001
stage=initial_freeze
evidence_class=real
case_ref=arv001-real-001
producer_mode=production_llm_r10_1
```

`artifacts` должен содержать exact SHA-256:

- `source_bundle_sha256`;
- `canonical_output_sha256`;
- `report_sha256`;
- `source_graph_sha256`.

В review JSON запрещены raw facts, quotes, customer/procurement/run IDs и пути.
`evidence_ref` в defects должен быть нейтральной ссылкой вроде
`truth-pack:defect-003`, а не цитатой или local path.

Проверить JSON Schema через Python, передав private path через environment, а не
вставляя его в репозиторный файл:

```bash
export ARV001_REVIEW_PATH="$PRIVATE_ROOT/arv001-review.json"
python - <<'PY'
import json
import os
from pathlib import Path
import jsonschema

schema = json.loads(
    Path("quality_gates/arv001/review.schema.json").read_text(encoding="utf-8")
)
review = json.loads(Path(os.environ["ARV001_REVIEW_PATH"]).read_text(encoding="utf-8"))
jsonschema.validate(review, schema)
print("ARV-001 review schema: OK")
PY
```

## 11. Двойная детерминированная evaluation

```bash
python quality_gates/arv001/evaluate.py evaluate \
  "$PRIVATE_ROOT/arv001-review.json" \
  --output "$PRIVATE_ROOT/evaluation-1.json"
RC1=$?

python quality_gates/arv001/evaluate.py evaluate \
  "$PRIVATE_ROOT/arv001-review.json" \
  --output "$PRIVATE_ROOT/evaluation-2.json"
RC2=$?

cmp "$PRIVATE_ROOT/evaluation-1.json" "$PRIVATE_ROOT/evaluation-2.json"
shasum -a 256 \
  "$PRIVATE_ROOT/evaluation-1.json" \
  "$PRIVATE_ROOT/evaluation-2.json"
```

Ожидается byte-identical output. Для freeze допустим только:

```text
verdict=PASS
exit_code=0
freeze_allowed=true
```

`CONDITIONAL`, `FAIL` и `NOT_READY` не исправлять вручную и не переводить в PASS.
Вернуть:

```text
ARV-001_GATE_NOT_FROZEN=<VERDICT>:<sorted_reason_codes>
```

и остановиться без commit/PR.

## 12. Двойной freeze manifest

Только после честного `PASS`:

```bash
FROZEN_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
APPROVAL_ID="arv001-local-acceptance-001"

python quality_gates/arv001/evaluate.py freeze \
  "$PRIVATE_ROOT/arv001-review.json" \
  --frozen-at "$FROZEN_AT" \
  --approval-id "$APPROVAL_ID" \
  --output "$PRIVATE_ROOT/freeze-1.json"

python quality_gates/arv001/evaluate.py freeze \
  "$PRIVATE_ROOT/arv001-review.json" \
  --frozen-at "$FROZEN_AT" \
  --approval-id "$APPROVAL_ID" \
  --output "$PRIVATE_ROOT/freeze-2.json"

cmp "$PRIVATE_ROOT/freeze-1.json" "$PRIVATE_ROOT/freeze-2.json"
shasum -a 256 "$PRIVATE_ROOT/freeze-1.json" "$PRIVATE_ROOT/freeze-2.json"
```

Проверить `freeze_manifest.schema.json`. Затем вручную и автоматизированно
проверить, что manifest не содержит identifiers, raw content, paths или secrets.

## 13. Разрешённые файлы в Git

Создать только:

```text
evidence/arv001/arv001-real-001/freeze_manifest.json
evidence/arv001/arv001-real-001/acceptance.md
```

Скопировать `freeze-1.json` как `freeze_manifest.json`.

`acceptance.md` должен содержать только санитизированные данные:

- base SHA и branch;
- policy version и SHA-256;
- ARV-003 accepted manifest version и его SHA-256;
- case alias;
- producer mode;
- выбранный execution alias `execution-1`;
- report format без local path;
- verdict и sorted reason codes;
- рассчитанные metric values;
- defect counts по severity/status;
- reviewer count и роли без ФИО;
- decision agreement/adjudication state;
- exact four artifact hashes;
- два evaluation SHA-256 и byte-identity result;
- freeze manifest SHA-256;
- команды и результаты проверок;
- privacy/secret scan result;
- completion markers.

Не добавлять private review, truth pack, evaluation outputs, ARV-003 execution
files или accepted controlled manifest целиком.

## 14. Privacy и secret gate

До `git add` получить actual sensitive identifiers локально и проверить, что
они не встречаются в двух разрешённых Git-файлах. Проверка не должна печатать
само sensitive value.

Обязательно проверить:

- actual registry/procurement number;
- customer/project/case/run IDs;
- customer name;
- actual source file names, если они идентифицируют клиента;
- private root и home path;
- provider/model request IDs;
- API/DSN/authorization patterns.

Также выполнить:

```bash
git diff --check
python scripts/ops/secret_scan.py

if grep -RInE \
  '/Users/|/Volumes/|postgres(ql)?://|redis://|Bearer[[:space:]]|BEGIN .*PRIVATE KEY|api[_-]?key|token=' \
  evidence/arv001/arv001-real-001; then
  echo "ARV-001_BLOCKED=SANITIZATION_FAILED"
  exit 2
fi
```

Если sensitive-value scan находит совпадение, удалить только утечку из
санитизированного evidence. Не изменять raw artifacts или исторические данные.

## 15. Полные проверки перед commit

```bash
python quality_gates/arv001/evaluate.py validate-package
python -m pytest -q tests/quality/test_arv001_quality_gate.py
make check
make test
alembic heads
python scripts/ops/secret_scan.py
git diff --check
git status --short
```

Все проверки должны завершиться успешно. Не скрывать warnings/errors и не
ослаблять тесты.

## 16. Commit, push и Draft PR

```bash
git add \
  evidence/arv001/arv001-real-001/freeze_manifest.json \
  evidence/arv001/arv001-real-001/acceptance.md

git diff --cached --check
git commit -m "evidence(arv001): freeze accepted real golden report"
git push -u origin opencode/arv-001-local-acceptance
```

Открыть **Draft PR** в `main`, связать с issue `#87`. Не merge-ить и не закрывать
issue самостоятельно.

PR body должен содержать:

```text
ARV-001_REAL_GOLDEN_REPORT_ACCEPTED
ARV-001_TWO_INDEPENDENT_REVIEWS_COMPLETE
ARV-001_EVALUATION_DETERMINISTIC
ARV-001_FREEZE_MANIFEST_CREATED
ARV-001_PRIVATE_DATA_NOT_COMMITTED
ARV-001_SOURCE_GRAPH_UNCHANGED
ARV-001_PROVIDER_CALLS_ZERO
ARV-001_EXTERNAL_ACTIONS_ZERO
ARV-001_GATE_FROZEN
```

Дождаться exact-head GitHub Actions и проверить все jobs. Если CI падает, не
merge-ить и не изменять gate ради зелёного статуса.

## 17. Финальный отчёт OpenCode

Вернуть одним сообщением:

1. `BASE_SHA`;
2. branch и full remote head SHA;
3. Draft PR URL/state;
4. changed files и diffstat;
5. ARV-003 accepted bundle verification status;
6. ARV-003 manifest version/SHA-256;
7. producer mode и selected execution alias;
8. policy version/SHA-256;
9. review schema validation;
10. reviewer aliases/roles/count и adjudication state без ФИО;
11. counts и рассчитанные metrics;
12. defects по severity/status;
13. verdict, freeze_allowed и sorted reason codes;
14. evaluation-1/evaluation-2 SHA-256 и byte identity;
15. freeze-1/freeze-2 SHA-256 и byte identity;
16. exact four artifact hashes;
17. focused tests, `make check`, `make test`, Alembic, secret scan;
18. privacy dynamic-value scan;
19. exact-head Actions run/jobs/conclusions;
20. review threads и unresolved count;
21. подтверждения: provider calls 0, DB mutations 0, source-graph mutations 0,
    external actions 0, raw/private data in Git 0;
22. completion markers либо один точный blocker marker.

После отчёта остановиться. Финальный merge PR и закрытие issue `#87` выполняются
только после независимой проверки GitHub evidence.

## 18. Допустимые blocker markers

```text
ARV-001_BLOCKED=REPOSITORY_GATE_FAILED:<code>
ARV-001_LOCAL_ACCEPTANCE_BLOCKED=ARV-003_ACCEPTED_BUNDLE_NOT_VERIFIED
ARV-001_BLOCKED=ARTIFACT_BINDING_MISMATCH
ARV-001_WAITING_FOR_TWO_HUMAN_REVIEWS
ARV-001_GATE_NOT_FROZEN=<VERDICT>:<sorted_reason_codes>
ARV-001_BLOCKED=SANITIZATION_FAILED
ARV-001_BLOCKED=LOCAL_TESTS_FAILED:<code>
ARV-001_BLOCKED=CI_FAILED:<job>
```

Не заменять точный blocker убедительно звучащим, но недоказанным отчётом об
успешном завершении.
