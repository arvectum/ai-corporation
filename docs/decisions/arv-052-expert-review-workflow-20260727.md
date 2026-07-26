# Решение ARV-052 — экспертная проверка и эскалация

Дата решения: 2026-07-27.

Статус: `ARV_052_COMPLETE`.

## Что реализовано

- отдельная tenant-scoped карточка экспертной эскалации;
- детерминированные trigger codes, severity и expert-role routing;
- блокировка передачи для Sev-1/Sev-2 и неблокирующий Sev-3;
- внутренний business-hours SLA v1;
- включённый лимит трёх проверок, paid-addon 3 000 ₽, approve/waive и
  обязательный no-charge для safety/error cases;
- состояния assignment/start/customer-input/final decision;
- immutable финальное решение;
- автоматический `PilotFeedback` для corrective decisions;
- новый run вместо изменения утверждённого результата;
- append-only event history с SHA-256 hash chain;
- idempotent creation и tenant isolation;
- внутренний API и focused regression tests;
- Alembic revision `097_add_arv052_expert_review`.

## Принятые границы

- обязательная операторская проверка R8 сохраняется;
- approved PDF не редактируется;
- внешнее исполнение остаётся закрытым;
- actor-role fields пока работают внутри operator-auth boundary и не заявляются
  как полноценный RBAC;
- коммерческая запись не является биллингом или платёжной операцией;
- сроки — внутренние ориентиры пилота, не публичный SLA.

## Критерий закрытия

Задача закрыта, когда сложный отчёт можно воспроизводимо эскалировать,
назначить профильному эксперту, заблокировать при необходимости, завершить
аудируемым решением, вернуть подтверждённую ошибку в quality loop и при
существенной корректировке запустить новый анализ без изменения старого
immutable результата.

Маркеры:

- `ARV_052_PRODUCTIZED_EXPERT_REVIEW_COMPLETE`
- `ARV_052_AUDIT_CHAIN_AND_QUALITY_LOOP_COMPLETE`
- `ARV_052_NO_EXTERNAL_EXECUTION`
