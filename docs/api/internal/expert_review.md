# Internal API — expert review (ARV-052)

Все маршруты находятся под существующей operator-auth границей:

`/api/operator/pilot/customers/{customer_id}/cases/{case_id}`

## Маршруты

- `POST /runs/{run_id}/expert-escalations` — создать карточку. Обязателен
  `Idempotency-Key`.
- `GET /expert-escalations` — список карточек кейса.
- `GET /expert-escalations/{id}` — карточка.
- `POST /expert-escalations/{id}/commercial-decision` — `approve` или `waive`
  для платной дополнительной проверки.
- `POST /expert-escalations/{id}/assign` — назначить специалиста.
- `POST /expert-escalations/{id}/start` — начать проверку.
- `POST /expert-escalations/{id}/customer-input` — зафиксировать поступившее
  уточнение и вернуть карточку эксперту.
- `POST /expert-escalations/{id}/decision` — записать решение.
- `GET /expert-escalations/{id}/events` — hash-chained audit history и
  `chain_valid`.

## Коды ошибок

Ошибки возвращаются как `detail: {code, message}`. Стабильные коды включают:

- `IDEMPOTENCY_SCOPE_CONFLICT`;
- `ESCALATION_NOT_PERMITTED`;
- `REVIEW_ALREADY_IMMUTABLE`;
- `COMMERCIAL_APPROVAL_REQUIRED`;
- `EXPERT_ROLE_MISMATCH`;
- `DECISION_NOT_PERMITTED`;
- `FEEDBACK_REQUIRED`;
- `SEVERITY_PROMOTION_REQUIRED`;
- `RUN_NOT_CURRENT`;
- `ESCALATION_NOT_FOUND`.

## Интеграционная граница

n8n или CRM в будущем могут вызывать только эти документированные endpoints.
Прямая запись в `pilot_expert_escalations`, event table, `procurement_cases` или
`pilot_feedback` запрещена.
