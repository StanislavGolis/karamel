"""Решение по жалобе и выдача санкции (стори 8.4).

**Порядок — КОНТРАКТ, а не стиль** (D-47, класс H-1 из CR 8.2): статус жалобы
проверяется ПОД блокировкой. `SELECT ... FOR UPDATE` возвращает уже свежую
строку (в READ COMMITTED PostgreSQL после ожидания блокировки перечитывает
строку), поэтому статус читается в том же запросе, что и блокировка. Проверка
«до блокировки» дала бы двум модераторам одновременный проход и `500` на
`UNIQUE(report_id)` вместо `409`.

**Вторая линия — CAS на `reports`** (`UPDATE ... WHERE status='open'`,
архитектура §5: «условный `UPDATE` — это CAS на строке, отдельная блокировка
не нужна, потому что сам статус и есть флаг»). Она защищает независимо от
блокировки: мутация «снять `AND status='open'`» обязана краснить тест
согласованности `reports.status` ↔ `sanctions.type`.

**Обвиняемый ВЫЧИСЛЯЕТСЯ, колонки нет** (D-57). `reports` хранит только
`reporter_id` (проверено по `models/report.py`), и материализация адреса
потребовала бы миграции с backfill по всем прошлым жалобам. Один SQL на оба
типа жалобы; вычисление детерминировано, потому что состав пары и диалога
после подачи не меняется.

**`dismissed` не создаёт санкцию и не уведомляет обвиняемого** (D-46, FR89).
Санкция — запись о последствии; её отсутствие последствием не является, а
уведомление `sanction.issued` утверждало бы ограничение, которого нет (FR39).

**Миграции нет** (D-54): `sanctions`, `reports.resolution` + три `CHECK`,
`users.status` созданы в `0025` и ждали потребителя.
"""

from __future__ import annotations

import json
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SanctionType
from app.repositories.notification_settings import mute_gate_sql
from app.services.moderation_reports_service import ReportNotFoundError

__all__ = [
    "NOT_APPLICABLE_CODE",
    "REPORT_ALREADY_RESOLVED_CODE",
    "REPORT_RESOLVED_TYPE",
    "SANCTION_ISSUED_TYPE",
    "ResolveResult",
    "ReportAlreadyResolvedError",
    "ReportNotFoundError",
    "SanctionNotApplicableError",
    "resolve_report",
]

#: D-48: `409`, отдельный от `ALREADY_REPORTED` (8.2) — разные действия.
REPORT_ALREADY_RESOLVED_CODE = "REPORT_ALREADY_RESOLVED"

#: D-48, FR89: `422`, а не `409`. Значение пришло от клиента в неподходящем
#: состоянии пары — та же граница, что `ATTENDANCE_PENDING`/`ATTENDANCE_CONFIRMED`
#: в 7.4 (D-11). Сервер корректен; состояние изменилось не по его вине.
NOT_APPLICABLE_CODE = "SANCTION_NOT_APPLICABLE"

#: D-50, NFR60: `сущность.действие`. Форма проверяется `TYPE_PATTERN` схемы WS.
SANCTION_ISSUED_TYPE = "sanction.issued"
REPORT_RESOLVED_TYPE = "report.resolved"

#: D-52, FR109: единственный статус пары, влекущий санкцию за неявку.
_SANCTIONABLE_ATTENDANCE = "no_show"
#: FR89: расхождение — не основание санкции. Допустимо только закрытие.
_DISPUTED_ATTENDANCE = "disputed"

#: D-46: исход → (status жалобы, resolution). `dismissed` — без санкции.
_RESOLUTION_TO_STATUS: dict[str, str] = {
    "warning": "resolved_warning",
    "ban": "resolved_ban",
    "dismissed": "dismissed",
}

#: D-49: санкция → статус учётной записи. `dismissed` здесь отсутствует
#: намеренно: ограничения нет, значит и статус не меняется.
_SANCTION_TO_USER_STATUS: dict[str, str] = {
    SanctionType.WARNING.value: "warned",
    SanctionType.BAN.value: "banned",
}


class ReportAlreadyResolvedError(Exception):
    """AC-05, D-48: по жалобе уже вынесено решение — `409`."""

    code = REPORT_ALREADY_RESOLVED_CODE


class SanctionNotApplicableError(Exception):
    """AC-04, FR89: санкция за эту пару недопустима — `422`."""

    code = NOT_APPLICABLE_CODE


@dataclass(frozen=True, slots=True)
class ResolveResult:
    """AC-05: РОВНО то, что нужно ответу, и ничего лишнего.

    `slots` — нельзя добавить поле по неосторожности в другом месте.
    `chat_uuid` нужен роуту для WS-публикации и наружу не выходит (D-50:
    у `no_show` канала не существует, там `None`).
    """

    report_uuid: uuid_lib.UUID
    status: str
    resolution: str
    sanction_uuid: uuid_lib.UUID | None
    resolved_at: datetime
    chat_uuid: uuid_lib.UUID | None


#: AC-05, D-47: статус читается В ТОМ ЖЕ запросе, что и блокировка.
#: Отдельный `SELECT status` до `FOR UPDATE` дал бы класс H-1 из CR 8.2.
#: `chat_uuid` тянется здесь же: второй запрос за ним разошёлся бы с
#: `chat_id` при первой правке и дал бы разное число SQL на успехе.
_LOCK_REPORT_SQL = """
SELECT r.id,
       r.target_type,
       r.chat_id,
       r.attendance_id,
       r.reporter_id,
       r.status,
       c.uuid AS chat_uuid
  FROM reports AS r
  LEFT JOIN chats AS c ON c.id = r.chat_id
 WHERE r.uuid = :uuid
   FOR UPDATE OF r
"""

#: AC-04, D-52: состояние пары читается ПОД блокировкой жалобы и СЕЙЧАС,
#: а не берётся из `reports`: между подачей и решением пару могли
#: до-подтвердить (7.2), и санкция за `confirmed` была бы ложной.
_ATTENDANCE_STATUS_SQL = """
SELECT a.status
  FROM attendance AS a
 WHERE a.id = :attendance_id
"""

#: D-57: обвиняемый одним запросом на ОБА типа жалобы. `UNION` дедуплицирует:
#: у `no_show` участник и автор карточки — разные люди, но одна из ветвей
#: всегда отсекается предикатом `<> r.reporter_id`, а `UNION` страхует от
#: двойной строки при вырожденных данных. Второй запрос дал бы N+1 по ветвям.
_ACCUSED_SQL = """
SELECT accused.user_id
  FROM (
        SELECT a.participant_id AS user_id
          FROM reports AS r
          JOIN attendance AS a ON a.id = r.attendance_id
         WHERE r.id = :report_id
           AND r.target_type = 'no_show'
           AND a.participant_id <> r.reporter_id
        UNION
        SELECT ec.author_id AS user_id
          FROM reports AS r
          JOIN event_cards AS ec ON ec.id = r.event_card_id
         WHERE r.id = :report_id
           AND r.target_type = 'no_show'
           AND ec.author_id <> r.reporter_id
        UNION
        SELECT ec.author_id AS user_id
          FROM reports AS r
          JOIN chats AS c ON c.id = r.chat_id
          JOIN event_cards AS ec ON ec.id = c.event_card_id
         WHERE r.id = :report_id
           AND r.target_type = 'message'
           AND ec.author_id <> r.reporter_id
        UNION
        SELECT jr.applicant_id AS user_id
          FROM reports AS r
          JOIN chats AS c ON c.id = r.chat_id
          JOIN join_requests AS jr
            ON jr.card_id = c.event_card_id AND jr.status = 'selected'
         WHERE r.id = :report_id
           AND r.target_type = 'message'
           AND jr.applicant_id <> r.reporter_id
       ) AS accused
"""

#: AC-01, D-17: `ON CONFLICT (report_id)` резолвится по `UNIQUE(report_id)` —
#: единственной защите от двух решений. Предпроверка запрещена (P-3).
#: `RETURNING uuid` даёт адрес санкции для ответа и уведомления.
_INSERT_SANCTION_SQL = """
INSERT INTO sanctions (user_id, report_id, moderator_id, type, reason)
VALUES (:user_id, :report_id, :moderator_id, :type, :reason)
ON CONFLICT (report_id) DO NOTHING
RETURNING uuid
"""

#: AC-05, D-47: CAS. `status = 'open'` — вторая линия защиты; без неё
#: проигравший гонку перезаписал бы `resolution` своим решением, и
#: `reports.status` разошёлся бы с `sanctions.type`.
#: `resolved_at = now()` — бикондиционал `resolved_after_reported` выполняется:
#: жалоба создана раньше решения.
_UPDATE_REPORT_SQL = """
UPDATE reports
   SET status = :status,
       resolution = :resolution,
       reason = :reason,
       resolved_at = now()
 WHERE id = :report_id
   AND status = 'open'
RETURNING resolved_at
"""

#: AC-01, D-49: статус учётной записи. `<> 'banned'` для бана и `= 'active'`
#: для предупреждения — ПОНИЖЕНИЯ НЕТ: `banned` не становится `warned`, а
#: `warned` не откатывается в `active`. Понижение отменило бы действующую
#: санкцию фактом новой жалобы.
_UPDATE_USER_STATUS_SQL = {
    "warned": "UPDATE users SET status = 'warned' WHERE id = :user_id AND status = 'active'",
    "banned": "UPDATE users SET status = 'banned' WHERE id = :user_id AND status <> 'banned'",
}

#: AC-01, D-50: по строке каждому адресату. `type` — параметр, а не литерал
#: в двух копиях: список адресатов и типы задаются одним запросом, иначе
#: правка одного из двух `INSERT` разошлась бы с другим.
_INSERT_NOTIFICATION_SQL = f"""
INSERT INTO notifications (user_id, type, payload)
SELECT CAST(:user_id AS bigint), CAST(:type AS varchar), CAST(:payload AS jsonb)
 WHERE {mute_gate_sql("CAST(:user_id AS bigint)", "CAST(:type AS varchar)")}
"""


def _payload(
    *, report_uuid: uuid_lib.UUID, sanction_uuid: uuid_lib.UUID | None, resolution: str, reason: str
) -> str:
    """AC-01, NFR27: адреса и причина, БЕЗ текста переписки и имени модератора.

    `reason` входит в payload намеренно: FR39 требует, чтобы обвиняемый видел
    ПРИЧИНУ ограничения, а не только факт. Текста сообщений здесь нет —
    `payload` собирается из тела запроса модератора, а не из `messages`.
    """
    return json.dumps(
        {
            "report_uuid": str(report_uuid),
            "sanction_uuid": str(sanction_uuid) if sanction_uuid is not None else None,
            "resolution": resolution,
            "reason": reason,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def _accused_id(session: AsyncSession, *, report_id: int) -> int:
    """D-57: обвиняемый одним запросом. 0 строк — рассинхрон данных сервера.

    `ValueError` (не `404`/`422`): жалоба валидировала обвиняемого при подаче
    (7.4/8.2), и его отсутствие означает дефект данных, а не ввод клиента —
    класс `A-ENUM-01` из 7.2.
    """
    result = await session.execute(text(_ACCUSED_SQL), {"report_id": report_id})
    rows = result.all()
    if len(rows) != 1:
        raise ValueError(
            f"Ожидался ровно один обвиняемый по жалобе {report_id}, получено {len(rows)}"
        )
    return int(rows[0][0])


async def resolve_report(
    session: AsyncSession,
    *,
    report_uuid: uuid_lib.UUID,
    moderator_id: int,
    resolution: str,
    reason: str,
) -> ResolveResult:
    """AC-01…AC-09: вынести решение по жалобе и выдать санкцию.

    Порядок (D-47, контракт):
    (1) `SELECT ... FOR UPDATE` по `reports.uuid` — пусто → `404`;
    (2) `status != 'open'` ПОД блокировкой → `409`;
    (3) для `no_show` — чтение `attendance.status` под той же блокировкой;
    (4) проверка применимости (AC-04, FR89);
    (5) `INSERT` санкции `ON CONFLICT DO NOTHING RETURNING` → пусто → `409`
        (только для `warning`/`ban`);
    (6) CAS-`UPDATE reports ... WHERE status='open'` → пусто → `409`;
    (7) `UPDATE users.status` (только для санкции, D-49);
    (8) уведомления: обвиняемому `sanction.issued`, автору `report.resolved`.

    `commit` делает роут. Исключение на любом шаге откатывает транзакцию
    целиком (`get_session` закрывает сессию без `commit`), поэтому санкция-сирота
    после провалившегося CAS в БД не остаётся.
    """
    result = await session.execute(text(_LOCK_REPORT_SQL), {"uuid": report_uuid})
    row = result.first()
    if row is None:
        # AC-10: жалобы нет. Тот же класс и код, что у очереди 8.3.
        raise ReportNotFoundError()

    report_id, target_type, chat_id, attendance_id, reporter_id, status, chat_uuid = row

    # (2) AC-05, D-47: проверка ПОД блокировкой. Строка уже свежая: после
    # ожидания блокировки READ COMMITTED перечитывает её.
    if status != "open":
        raise ReportAlreadyResolvedError()

    # (3)–(4) AC-04, D-52, FR89: санкция за неявку допустима только при
    # `no_show`; при `disputed` — только закрытие без санкции.
    if target_type == "no_show":
        if attendance_id is None:
            # Невыразимо по схеме (`target_scope`), но проверяется явно:
            # молчаливый пропуск дал бы санкцию по жалобе без предмета.
            raise ValueError(f"Жалоба {report_id} типа no_show без attendance_id")
        pair = await session.execute(text(_ATTENDANCE_STATUS_SQL), {"attendance_id": attendance_id})
        pair_row = pair.first()
        if pair_row is None:
            raise ValueError(f"Нет пары attendance {attendance_id} для жалобы {report_id}")
        attendance_status = pair_row[0]
        # FR89: `disputed` — не основание санкции; допустимо только закрытие.
        # `confirmed`/`pending` — жалоба невозможна, состояние изменилось после
        # подачи (7.2 позволяет до-подтвердить), санкция была бы ложной.
        only_dismissed = attendance_status == _DISPUTED_ATTENDANCE and resolution == "dismissed"
        if attendance_status != _SANCTIONABLE_ATTENDANCE and not only_dismissed:
            raise SanctionNotApplicableError()

    accused_id = await _accused_id(session, report_id=report_id)

    sanction_uuid: uuid_lib.UUID | None = None
    if resolution != "dismissed":
        # (5) AC-08: `UNIQUE(report_id)` + `ON CONFLICT` — единственная защита.
        inserted = await session.execute(
            text(_INSERT_SANCTION_SQL),
            {
                "user_id": accused_id,
                "report_id": report_id,
                "moderator_id": moderator_id,
                "type": resolution,
                "reason": reason,
            },
        )
        sanction_uuid = inserted.scalar_one_or_none()
        if sanction_uuid is None:
            # Второй модератор уже вставил санкцию — тот же исход, что и
            # провалившийся CAS: решение принято не нами.
            raise ReportAlreadyResolvedError()

    # (6) AC-05, AC-09, D-47, D-53: CAS. `reason` пишется И сюда, и в санкцию:
    # «причина решения» для карточки 8.5 против «за что наказан» (NOT NULL).
    updated = await session.execute(
        text(_UPDATE_REPORT_SQL),
        {
            "report_id": report_id,
            "status": _RESOLUTION_TO_STATUS[resolution],
            "resolution": resolution,
            "reason": reason,
        },
    )
    resolved_at = updated.scalar_one_or_none()
    if resolved_at is None:
        raise ReportAlreadyResolvedError()

    # (7) AC-02, AC-06, D-49: статус учётной записи — только при санкции.
    if resolution in _SANCTION_TO_USER_STATUS:
        await session.execute(
            text(_UPDATE_USER_STATUS_SQL[_SANCTION_TO_USER_STATUS[resolution]]),
            {"user_id": accused_id},
        )

    # (8) AC-01, D-50, M11: обвиняемому — только при санкции. При `dismissed`
    # ограничения не было, и `sanction.issued` утверждал бы обратное (FR39).
    payload = _payload(
        report_uuid=report_uuid,
        sanction_uuid=sanction_uuid,
        resolution=resolution,
        reason=reason,
    )
    if sanction_uuid is not None:
        await session.execute(
            text(_INSERT_NOTIFICATION_SQL),
            {"user_id": accused_id, "type": SANCTION_ISSUED_TYPE, "payload": payload},
        )
    await session.execute(
        text(_INSERT_NOTIFICATION_SQL),
        {"user_id": reporter_id, "type": REPORT_RESOLVED_TYPE, "payload": payload},
    )

    return ResolveResult(
        report_uuid=report_uuid,
        status=_RESOLUTION_TO_STATUS[resolution],
        resolution=resolution,
        sanction_uuid=sanction_uuid,
        resolved_at=resolved_at,
        chat_uuid=chat_uuid,
    )
