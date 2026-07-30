# ARV-076 — Acceptance record

## Идентификация

- Дата/время UTC:
- Оператор:
- Git commit до/после:
- Production Colima profile:
- Production Docker context:
- Backup set ID:
- Backup root/device:
- Restore root/device:
- Restore profile:

## Preflight

- [ ] Git status зафиксирован
- [ ] Production containers/volumes зафиксированы
- [ ] Source Colima home зафиксирован
- [ ] Backup root находится на независимом устройстве
- [ ] Свободное место проверено
- [ ] Окно остановки PostgreSQL согласовано

## Backup evidence

- Exit code:
- Total time:
- PostgreSQL dump time/size/SHA-256:
- PostgreSQL volume archive time/size/SHA-256:
- Image archives total size:
- Backup set total size:
- Source containers restarted:
- Source `pg_isready`:
- Source fingerprint unchanged:
- `verify` result:

## Restore evidence

- Isolated `COLIMA_HOME`:
- `docker version`:
- Raw volume restore time:
- Raw volume fingerprint match:
- Logical restore time:
- Logical restore fingerprint match:
- `pg_isready`:
- SQL smoke:
- Containers/volumes inventory:
- Application smoke command/result:

## Рабочий SSD недоступен

- Метод безопасной имитации недоступности:
- Backup verify без SSD:
- Restore без SSD:
- PostgreSQL smoke без SSD:
- Application smoke без SSD:

## Неизменность production

- Git commit до/после совпадает:
- Git status до/после совпадает:
- Production container inventory до/после совпадает:
- Production volume inventory до/после совпадает:
- Production ports/services после acceptance:

## RPO/RTO и capacity

- Фактический RPO:
- Фактический RTO до PostgreSQL:
- Фактический RTO до application smoke:
- Минимально необходимое свободное место:
- Рекомендованный размер backup-носителя:

## Security review

- [ ] `.env` не копировался
- [ ] Docker auth не копировался
- [ ] plaintext password/token/API key отсутствует в backup manifest и отчёте
- [ ] временный restore password удалён после cleanup

## Итог

- Решение: `PASS / FAIL`
- Блокеры:
- Неблокирующие замечания:
- Следующая дата restore drill:
