# ARV-076 — Backup/restore Docker runtime и Colima

## 1. Назначение

Этот runbook описывает независимый backup/restore-контур локального runtime Арвектум на macOS:

- рабочий Colima profile и Docker context;
- named volumes Docker;
- PostgreSQL logical dump как каноническую переносимую копию;
- холодный архив критичного PostgreSQL volume как независимую физическую копию;
- конфигурацию Colima и Compose в версии без секретов;
- SHA-256 manifest, обязательную verify-проверку и изолированный restore test.

Копия каталога рабочего `~/.colima` или VM-диска Colima **не считается единственным backup**. Канонические данные восстановления — `postgres.dump`, архивы named volumes, сохранённые образы, очищенная конфигурация и manifest.

## 2. Границы безопасности

Запрещено:

- писать backup в `/Volumes/ArvectumSSD/Docker/colima` или на тот же физический том для acceptance;
- копировать `.env`, Docker auth, токены, private keys и plaintext credentials;
- архивировать работающий PostgreSQL volume;
- выполнять `docker system prune`, `docker volume prune`, `colima reset` или restore поверх production volume;
- считать каталог `.incomplete-*` успешной копией;
- удалять старые копии до отдельного `verify`;
- закрывать ARV-076 без restore evidence и application smoke.

Скрипт fail-closed: итоговый каталог публикуется только после возврата source-контейнеров в исходное состояние и успешной проверки manifest.

## 3. Артефакты

Каждый backup set имеет имя:

```text
YYYYMMDDTHHMMSSZ-xxxxxxxx/
```

Минимальный состав:

```text
BACKUP_COMPLETE
MANIFEST.json
SHA256SUMS
VERIFY_EVIDENCE.json
config/colima.yaml
config/compose/*.yml
images/*.tar.gz
metadata/inventory.json
metadata/backup_metrics.json
metadata/image_archives.json
metadata/volume_archives.json
postgres/postgres.dump
postgres/schema.sql
postgres/fingerprint.json
volumes/*.tar.gz
```

`postgres/fingerprint.json` хранит структуру, версии расширений, Alembic revision и точные количества строк по пользовательским таблицам, но не выгружает содержимое строк.

## 4. Целевые RPO/RTO

До накопления фактической статистики принимаются операционные цели:

- **RPO:** не более 24 часов для регулярной копии и не более 1 часа перед рискованным инфраструктурным изменением;
- **RTO:** не более 4 часов до поднятого PostgreSQL и не более 8 часов до безопасного application smoke;
- backup перед миграцией/обновлением считается отдельной точкой восстановления и не заменяет регулярный график.

Фактические значения каждого прогона фиксируются в `metadata/backup_metrics.json` и acceptance record.

## 5. Политика хранения

Базовая GFS-ротация:

- 7 последних дневных точек;
- 4 последние недельные точки;
- 6 последних месячных точек;
- минимум одна копия на независимом зашифрованном носителе;
- перед удалением каждая точка повторно проходит `verify`;
- `.incomplete-*` автоматически не удаляются: сначала расследуется причина сбоя.

Dry-run ротации:

```bash
python scripts/ops/arv076_runtime_backup.py prune \
  --backup-root "$ARVECTUM_BACKUP_ROOT"
```

Удаление только после просмотра списка:

```bash
python scripts/ops/arv076_runtime_backup.py prune \
  --backup-root "$ARVECTUM_BACKUP_ROOT" \
  --apply
```

## 6. Preflight

### 6.1. Подготовить независимый backup root

Использовать второй зашифрованный том, NAS/сетевой том с шифрованием либо внутренний диск при достаточном свободном месте. Для acceptance путь должен находиться не на рабочем `/Volumes/ArvectumSSD`.

Пример:

```bash
export ARVECTUM_BACKUP_ROOT="/Volumes/ArvectumBackup/ai-corporation/runtime"
mkdir -p "$ARVECTUM_BACKUP_ROOT"
chmod 700 "$ARVECTUM_BACKUP_ROOT"
```

### 6.2. Зафиксировать Git и runtime до изменений

```bash
cd /Users/master/Documents/AI-Corporation-live

git status --short
git rev-parse HEAD
docker context show
docker version
docker ps -a
docker volume ls
colima status --profile default
```

Ожидается чистый Git либо заранее объяснённые локальные изменения. Скрипт не должен изменять репозиторий.

### 6.3. Уточнить фактические имена

```bash
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker volume ls
docker inspect arvectum-postgres --format '{{json .Mounts}}'
```

В командах ниже предполагаются:

```text
PostgreSQL container: arvectum-postgres
PostgreSQL volume:    arvectum-postgres_arvectum_postgres_data
Database:             ai_corporation
User:                 ai_corporation
Colima profile:       default
Colima home:          /Volumes/ArvectumSSD/Docker/colima
```

При отличиях передать фактические значения CLI-параметрами.

### 6.4. Проверить место

```bash
df -h "$ARVECTUM_BACKUP_ROOT"
docker system df -v
```

Резерв свободного места: не менее `1.5 ×` ожидаемого backup set плюс место для изолированного restore runtime.

## 7. Инвентаризация без изменения runtime

```bash
python scripts/ops/arv076_runtime_backup.py inventory \
  --repository-root "$PWD" \
  --colima-profile default \
  --colima-home /Volumes/ArvectumSSD/Docker/colima \
  --output /tmp/arv076-inventory.json
```

Проверить, что файл содержит:

- Colima profile/status и список файлов profile с размерами;
- Docker version/info/context;
- containers, images, networks, volumes;
- SHA-256 Compose-файлов;
- отсутствуют значения паролей, токенов и API keys.

## 8. Создание backup set

### 8.1. Stop/start order

Скрипт выполняет последовательность:

1. собирает inventory и очищенные конфиги;
2. делает live `pg_dump`, schema dump и database fingerprint;
3. сохраняет PostgreSQL/helper image;
4. останавливает контейнеры, назначенные каждому cold volume;
5. архивирует named volumes;
6. запускает только те source-контейнеры, которые были запущены до backup;
7. ждёт `pg_isready` и повторно сверяет fingerprint;
8. создаёт SHA-256 manifest и выполняет verify;
9. атомарно переименовывает `.incomplete-*` в финальный versioned set.

### 8.2. Команда

```bash
python scripts/ops/arv076_runtime_backup.py backup \
  --repository-root "$PWD" \
  --backup-root "$ARVECTUM_BACKUP_ROOT" \
  --colima-profile default \
  --colima-home /Volumes/ArvectumSSD/Docker/colima \
  --postgres-container arvectum-postgres \
  --postgres-volume arvectum-postgres_arvectum_postgres_data \
  --postgres-user ai_corporation \
  --postgres-database ai_corporation \
  --helper-image postgres:16-alpine \
  --confirm-production-downtime
```

Для дополнительных volumes обязательно указать контейнер, который надо остановить:

```bash
  --volume my_named_volume \
  --volume-container my_named_volume=my-container
```

Для полного offline-набора добавить сохранение всех образов запущенных контейнеров:

```bash
  --save-running-images
```

Acceptance не проводится с `--no-require-different-device`. Этот флаг допустим только для теста логики на некритичных данных.

## 9. Независимая verify-проверка

```bash
export ARVECTUM_BACKUP_SET="$(find "$ARVECTUM_BACKUP_ROOT" -maxdepth 1 -type d -name '20*T*Z-*' | sort | tail -1)"

python scripts/ops/arv076_runtime_backup.py verify \
  --backup-set "$ARVECTUM_BACKUP_SET"
```

Успех:

- exit code `0`;
- `verified: true`;
- отсутствуют `missing`, `sha256_mismatch`, `size_mismatch`, `unlisted_artifact`;
- присутствуют `BACKUP_COMPLETE`, `postgres/postgres.dump` и архив PostgreSQL volume.

## 10. Изолированный restore test

Restore использует отдельные `COLIMA_HOME`, `DOCKER_CONFIG`, profile и Docker socket. Production profile и production volumes не затрагиваются.

```bash
export ARVECTUM_RESTORE_ROOT="/Users/master/tmp/arv076-restore-$(date -u +%Y%m%dT%H%M%SZ)"

python scripts/ops/arv076_runtime_backup.py restore-test \
  --backup-set "$ARVECTUM_BACKUP_SET" \
  --restore-root "$ARVECTUM_RESTORE_ROOT" \
  --restore-profile arv-076-restore \
  --keep-runtime
```

Скрипт обязан проверить два независимых пути:

1. распаковку холодного Docker volume и запуск PostgreSQL из него;
2. восстановление `postgres.dump` в новый пустой volume.

Для обоих путей сравниваются Alembic revision, extensions, список таблиц и точные row counts. После успеха остаётся только logical-restore container для application smoke.

Данные подключения находятся в:

```text
$ARVECTUM_RESTORE_ROOT/RESTORE_EVIDENCE.json
$ARVECTUM_RESTORE_ROOT/postgres-password.txt
```

Пароль не переносится в Git, issue или acceptance report.

## 11. Application smoke на восстановленной БД

Прочитать порт и имя контейнера из `RESTORE_EVIDENCE.json`, затем:

```bash
RESTORED_PORT="$(python - <<'PY'
import json, os
from pathlib import Path
p = Path(os.environ['ARVECTUM_RESTORE_ROOT']) / 'RESTORE_EVIDENCE.json'
print(json.loads(p.read_text())['steps']['kept_runtime']['postgres_port'])
PY
)"
RESTORED_PASSWORD="$(cat "$ARVECTUM_RESTORE_ROOT/postgres-password.txt")"

export AI_CORP_DATABASE_URL="postgresql+psycopg://ai_corporation:${RESTORED_PASSWORD}@127.0.0.1:${RESTORED_PORT}/ai_corporation"
```

В отдельном локальном venv:

```bash
alembic current
python -m pytest -q <существующий безопасный PostgreSQL/application smoke>
python -m uvicorn src.main:app --host 127.0.0.1 --port 18001
```

В другом терминале:

```bash
curl --fail --silent http://127.0.0.1:18001/health
curl --fail --silent http://127.0.0.1:18001/health/ready
```

Не запускать внешние действия, отправку заявок, e-mail, ЭП или customer-data workflows. Smoke должен быть read-only либо работать с заранее разрешёнными тестовыми сущностями.

## 12. Сценарий потери рабочего SSD

Проверка считается выполненной только при отсутствии зависимости от `/Volumes/ArvectumSSD`:

1. остановить изолированный restore profile после первого прогона;
2. размонтировать рабочий SSD либо временно переименовать/заблокировать source path без удаления данных;
3. убедиться, что backup set, репозиторий и restore root доступны независимо;
4. повторить `verify`;
5. создать новый restore root и повторить `restore-test`;
6. выполнить PostgreSQL и application smoke;
7. вернуть рабочий SSD и проверить исходный runtime без изменений.

Нельзя имитировать потерю SSD удалением production profile или production volumes.

## 13. Завершение и cleanup test runtime

После сохранения acceptance evidence:

```bash
export COLIMA_HOME="$ARVECTUM_RESTORE_ROOT/colima-home"
export DOCKER_CONFIG="$ARVECTUM_RESTORE_ROOT/docker-config"
colima stop --profile arv-076-restore
colima delete --force --profile arv-076-restore
unset COLIMA_HOME DOCKER_CONFIG DOCKER_HOST COLIMA_PROFILE
rm -f "$ARVECTUM_RESTORE_ROOT/postgres-password.txt"
```

`RESTORE_EVIDENCE.json` можно сохранить локально; в Git допускается только санитизированный acceptance record без пароля, source data и абсолютных приватных путей.

## 14. Rollback

Если backup прерван:

1. не переименовывать `.incomplete-*` вручную;
2. проверить, что все source-контейнеры вернулись в исходное состояние;
3. выполнить `pg_isready` и SQL smoke production БД;
4. сохранить stderr и локальный `FAILED.json` для расследования;
5. исправить причину и создать новый backup set, не «доделывать» старый.

Если restore test не прошёл:

1. production runtime не изменять;
2. сохранить локальный `RESTORE_EVIDENCE.json`;
3. удалить только изолированный profile/volumes;
4. не удалять backup set;
5. классифицировать сбой: manifest, image load, raw volume, logical dump, fingerprint или application smoke;
6. после исправления повторить restore в новом restore root.

## 15. Критерии успеха ARV-076

- versioned backup set физически независим от рабочего Colima home;
- `pg_dump` и cold archive критичного volume существуют и не пусты;
- manifest покрывает все артефакты, verify проходит повторно;
- restore выполнен в отдельном `COLIMA_HOME`/profile;
- `docker version`, container/volume inventory, `pg_isready`, SQL smoke и fingerprint comparison успешны;
- application smoke успешен на logical restore;
- restore повторён при недоступном рабочем SSD;
- фактические backup/restore time, size и free-space requirement записаны;
- production Git commit/status и runtime inventory после acceptance совпадают с исходным состоянием;
- в артефактах и отчёте нет plaintext secrets.
