"""Модель анкеты `profiles` (Task 1.1–1.8).

D-1: анкета — отдельная таблица с FK на `users`. Бан и санкции живут на
учётке, модерация — на анкете; скрытие или удаление анкеты не ломает вход.

D-2: `id` — внутренний bigint, наружу не выходит никогда (AC-04).
Публичный идентификатор — `uuid`.

D-3: `age smallint`, не `birth_date`: дата рождения — лишний объём ПД по
152-ФЗ, которого PRD не требует (FR11: анкета = фото, текст, город).

D-5: `city` — текст, не FK на справочник. Справочника в архитектуре нет,
в пилоте город один; нормализация — на уровне сервиса (2.3) + CHECK в БД.

D-8: `name`, `city`, `age`, `bio` — NULL-able: строка анкеты появляется
в начале онбординга, до заполнения текста (фото загружаются в 2.2 раньше,
а `profile_photos.profile_id` NOT NULL).

D-9: `reliability_score` НЕ создаётся — перенесён в пост-MVP.

CR 2.1, Решение 1: `gender` — не просто дубль `users.gender`, а часть
составного FK `(user_id, gender) -> users(id, gender)`. Расхождение
«женская учётка + male-анкета» теперь невозможно на уровне БД, включая
записи сырым SQL и из воркера.

CR 2.1, патч 14: признак полноты — CHECK `num_nulls(...) IN (0, 4)`, а не
отдельная колонка `status`. Инвариант: анкета либо черновик (все четыре
поля NULL), либо заполнена целиком. Из него следует, что фильтр выборки
по `city IS NOT NULL` (эпик 4) отбирает только полные анкеты — ровно то,
что утверждал D-8, но теперь это гарантирует БД, а не надежда.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import Gender

if TYPE_CHECKING:
    from app.models.user import User


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """Собирает `CHECK (col IN ('a','b'))` из значений StrEnum — единый источник списка.

    Тот же приём, что в `user.py`: список полов не дублируется строкой, иначе
    добавление значения в `Gender` не изменило бы ограничение в БД.
    """
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


# CR 2.1, патч 1: `btrim(x)` без второго аргумента режет ТОЛЬКО пробел. Таб,
# перевод строки, \f, \v остаются, и `city = E'Томск\t'` проходит CHECK,
# но навсегда выпадает из выборки `WHERE city = 'Томск'`. Набор «пробельных»
# символов задан явно; NBSP (U+00A0) сюда не входит — он не whitespace для
# `btrim` и должен отсекаться нормализацией в сервисе 2.3.
_WHITESPACE = "E' \\t\\r\\n\\f\\v'"


def _trimmed(column: str) -> str:
    """`btrim(col, <пробельные>)` — единая запись для всех строковых CHECK."""
    return f"btrim({column}, {_WHITESPACE})"


class Profile(TimestampMixin, Base):
    """Анкета пользователя. Существует отдельно от учётной записи (D-1)."""

    __tablename__ = "profiles"

    # D-2: внутренний PK, наружу не отдаётся (AC-04, тест 8.2).
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # D-2: публичный идентификатор. Генерация и в БД (вставки вне ORM), и в Python
    # (объект готов до flush) — как в `users`.
    uuid: Mapped[uuid_lib.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        default=uuid_lib.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # CR 2.1, Решение 1: FK на users объявлен составным в `__table_args__`
    # (`ForeignKeyConstraint`), поэтому здесь ни `ForeignKey`, ни `ondelete`.
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # D-7: имя нужно карточке анкеты; в списке колонок архитектуры его не было.
    name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # D-5: город — текст; CHECK требует отсутствие пробельных символов по краям.
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # D-3: возраст «замерзает» на момент заполнения — приемлемо для пилота.
    age: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # D-4: зеркало `users.gender`. Дубль оправдан: выборка фильтрует
    # «город + противоположный пол» и нужен составной индекс без join.
    # CR 2.1, Решение 1: расхождение с `users.gender` запрещено составным FK.
    gender: Mapped[str] = mapped_column(String(16), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    # CR 2.1, патч 14: признак полноты анкеты. Отдельная колонка, а не связка
    # «все поля NULL или все заполнены»: онбординг пошаговый (фото в 2.2
    # приезжают раньше текста), и связка запретила бы сохранить частичную анкету.
    # Фильтр выборки эпика 4 становится явным: `status = 'complete'`.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default=text("'draft'")
    )
    # Скрытие анкеты владельцем: строка остаётся, вход не ломается (D-1).
    is_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # D-7: lazy="raise" — ленивая загрузка в async падает MissingGreenlet.
    # back_populates не задан: `user.py` в этой стори не правится (Task 1.7).
    # cascade в ORM не задан — удаление делает БД (ON DELETE CASCADE).
    user: Mapped[User] = relationship(lazy="raise")

    __table_args__ = (
        # AC-02: uq_profiles_uuid / uq_profiles_user_id — имена из convention,
        # вручную не задаются (иначе двойной префикс, §18.7).
        UniqueConstraint("uuid"),
        UniqueConstraint("user_id"),
        # AC-02, D-6: составной индекс под выборку «город пилота + пол».
        # Отдельный индекс по city был бы его префиксом — дублировать не нужно.
        # `is_hidden` в индекс не входит: значений два, селективность нулевая.
        Index("idx_profiles_city_gender", "city", "gender"),
        # CR 2.1, Решение 1: `gender` анкеты обязан совпадать с `users.gender`.
        # Прежний простой FK проверял только существование учётки, и `male`
        # у женской учётки проходил — выборка показала бы однополые анкеты.
        # ON DELETE CASCADE сохранён: удаление учётки удаляет анкету (AC-06).
        # Имя задано явно; naming convention для составного FK дала бы
        # `fk_profiles_user_id_users`, совпав с именем старого ограничения.
        ForeignKeyConstraint(
            ["user_id", "gender"],
            ["users.id", "users.gender"],
            name="fk_profiles_user_gender_users",
            ondelete="CASCADE",
        ),
        # AC-07: пол — только два значения из StrEnum.
        CheckConstraint(_in_clause("gender", tuple(Gender)), name="gender"),
        # AC-02: возраст либо не заполнен, либо 18..99.
        CheckConstraint("age IS NULL OR (age >= 18 AND age <= 99)", name="age"),
        # AC-02: пустая строка неотличима от отсутствия, но проходит в выборку
        # по городу и ломает карточку. btrim сравнивает с пустым.
        # CR 2.1, патч 2: `length(name) <= 50` убран — предел держит `varchar(50)`,
        # и CHECK по длине был мёртвым (до него дело не доходит).
        CheckConstraint(f"name IS NULL OR ({_trimmed('name')} <> '')", name="name"),
        # AC-07: city = btrim(city) — пробельные символы по краям запрещены в БД,
        # а не только в сервисе: иначе '  Томск' не найдётся запросом по 'Томск'.
        # Регистр не проверяется: CHECK на lower(city) заставил бы хранить
        # только нижний регистр, а показывать нужно «Томск» (D-5).
        CheckConstraint(
            f"city IS NULL OR ({_trimmed('city')} <> '' AND city = {_trimmed('city')})",
            name="city",
        ),
        # CR 2.1, патч 2: предел `text` держит сервис (2.3), не CHECK.
        CheckConstraint(f"bio IS NULL OR ({_trimmed('bio')} <> '')", name="bio"),
        # CR 2.1, патч 14: `status` — закрытый набор значений.
        CheckConstraint("status IN ('draft', 'complete')", name="status"),
        # CR 2.1, патч 14: `complete` требует все четыре поля. Иначе строка
        # `city='Томск'` с пустыми name/age/bio объявлялась бы полной и
        # рендерилась пустой карточкой. `draft` разрешает любую заполненность —
        # онбординг пошаговый (фото в 2.2 раньше текста).
        CheckConstraint(
            "status <> 'complete' OR num_nulls(name, city, age, bio) = 0",
            name="completeness",
        ),
    )
