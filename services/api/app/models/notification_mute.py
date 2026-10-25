"""Модель отключённых типов уведомлений `notification_mutes` (стори 9.3; AC-01, AC-02).

**Отдельная таблица, а не JSONB-поле в `users`/`profiles`** (D-9.3-2).
Gate «не создавать отключённый тип» обязан жить ВНУТРИ SQL-вставки: три из
четырёх путей создания уведомлений — пакетные `INSERT ... SELECT`, где
адресат производный. Поле профиля потребовало бы прочитать настройку в Python
и только потом вставить — второй запрос и окно гонки между чтением и вставкой
(класс дефекта R6 стори 7.3). Таблица даёт предикат
`NOT EXISTS (SELECT 1 FROM notification_mutes ...)`, который разрешается в той
же транзакции, что и вставка (D-9.3-6).

**Хранится ОТКЛЮЧЁННОЕ (opt-out)** (D-9.3-3):

- новый пользователь не требует ни одной строки и получает дефолт «всё включено»;
- новый тип (9.4: `window.expiring`, `attendance.result`; 9.6: `support.replied`)
  автоматически включён — миграция при добавлении типа не нужна;
- opt-in потребовал бы backfill 11 строк на каждого и ломался бы при каждом
  новом типе.

**`PRIMARY KEY (user_id, type)`** — одновременно уникальность (нельзя
отключить дважды, а `ON CONFLICT DO NOTHING` из D-9.3-12 становится
тривиальным) и индекс под gate-предикат. Отдельный индекс не создаётся:
предикат `m.user_id = :u AND m.type = :t` покрывается PK целиком, и любой
дополнительный индекс был бы мёртвой записью, которую никто не читает.

**Неотключаемые типы защищены СХЕМОЙ** (D-9.3-5). Гард API (`409`) не
закрывает прямую вставку мимо API (миграция данных, ручной SQL, будущий
внутренний вызов). `ck_notification_mutes_required` отвергает
`report.resolved`/`sanction.issued` на уровне БД: иначе отключение сделало бы
FR38/FR39 невыполнимыми по воле пользователя.

**`TimestampMixin` НЕ наследуется** (X-7.1-1). Путь записи — сырой
`INSERT ... ON CONFLICT DO NOTHING`/`DELETE` из репозитория; `onupdate` ORM на
нём не срабатывает, и `updated_at` осталась бы старой навсегда — ложный аудит
«когда менялось». Здесь изменения — это наличие/отсутствие строки, а не
колонка.

**`created_at` — единственная метка.** «Когда отключил» полезно для разбора
жалоб «я не получал»; «когда обновил» у opt-out-строки смысла не имеет:
обновление — это `DELETE` + `INSERT` с новой меткой.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import NOTIFICATION_TYPES, REQUIRED_NOTIFICATION_TYPES


#: AC-02 стори 9.1, D-9.3-5: `col IN ('a','b')` из кортежа значений.
#: Копия — общий существующий долг (`notification.py`, `chat.py`,
#: `attendance.py`, `access_log.py`, `sanction.py`, `support_ticket.py`).
def _in_clause(column: str, values: tuple[object, ...]) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


#: D-9.3-5: текст ограничения «неотключаемые типы». Форма —
#: `type NOT IN (...)`, а НЕ `type IN (<отключаемые>)`: список отключаемых
#: пришлось бы переписывать при каждом новом типе, и миграция 9.4/9.6
#: упиралась бы в CHECK вместо простого `ALTER ... DROP/ADD`.
#: `NOT IN` с непустым списком даёт `TRUE`/`FALSE`, а не `UNKNOWN`: `type`
#: объявлен `NOT NULL`.
REQUIRED_GUARD_SQL = f"type NOT IN ({', '.join(repr(str(v)) for v in REQUIRED_NOTIFICATION_TYPES)})"


class NotificationMute(Base):
    """Отключённый пользователем тип уведомлений (opt-out, FR46)."""

    __tablename__ = "notification_mutes"

    # AC-01: адресат настройки. `CASCADE` — удаление учётки уносит её
    # настройки: строка без пользователя не адресуется никому (ADR-07).
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # AC-01, NFR60: `сущность.действие`, тот же домен, что у `notifications.type`.
    # `String(64)` — как в `notifications`: расхождение длины между таблицами
    # одного домена сделало бы `UNION`/сравнение типов зависимым от колонки.
    type: Mapped[str] = mapped_column(String(64), nullable=False)

    # AC-01: когда отключил. Миксин не наследуется (см. docstring модуля).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # AC-01: PK `(user_id, type)` — уникальность + индекс под gate.
        PrimaryKeyConstraint("user_id", "type"),
        # AC-02 стори 9.1, D-9.1-3: ПЕРЕЧЕНЬ допустимых типов, поверх формы.
        # Вторая копия перечня (первая — `notifications.type`, третья —
        # литерал миграции `0031`); расхождение ловится тестом сверки трёх
        # источников (риск 3 стори).
        CheckConstraint(_in_clause("type", NOTIFICATION_TYPES), name="type"),
        # AC-02, NFR60: форма типа — та же, что у `notifications`. Опечатку
        # формы (`BAD_FORMAT`) не поймал бы перечень, а неизвестный тип клиент
        # обязан игнорировать (NFR60), то есть ошибку не увидел бы никто.
        # Форма проверяется здесь отдельно: `notification_mutes.type` не имеет
        # регекс-CHECK из `0021` (он принадлежит `notifications`).
        # D-9.3-5: неотключаемые типы запрещены СХЕМОЙ, не только API.
        CheckConstraint(REQUIRED_GUARD_SQL, name="required"),
        # D-9.3-2: предикат gate читает `created_at` только для разбора
        # «когда отключил» — индексов не добавляет (PK покрывает предикат).
        # Проверка «нельзя отключить дважды» держится PK, не `UNIQUE`.
        # AC-01, P-10: `type` не может быть пустой строкой — перечень это уже
        # закрывает, но отдельный CHECK документирует инвариант явно.
    )


__all__ = ["REQUIRED_GUARD_SQL", "NotificationMute"]
