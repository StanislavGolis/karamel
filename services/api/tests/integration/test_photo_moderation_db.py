"""Модерация фото на реальной БД (Task 6.1, 6.3, 6.4, 8.7; AC-10, AC-06).

Эти проверки НЕЛЬЗЯ сделать на заглушках (`tests/unit/test_photo_moderation.py`):

  * 6.1, 6.3 — значение после `flush` без `refresh`. Заглушка репозитория
    возвращает объект, который тест сам и создал, поэтому «значение есть»
    доказывалось бы присваиванием в тесте (§18.2 — тавтология);
  * 6.4 — инвариант против `session.execute(update(...))`. Заглушка не имеет
    identity map, и разница между мутацией ORM и `update()` в ней невидима;
  * 8.7 — доказательство откатом доменной проверки причины. Только реальный
    CHECK `ck_profile_photos_rejection_reason` показывает, что без проверки
    пришёл бы `IntegrityError` → `500`.

D-8 стори 2.2: схема поднимается Alembic (conftest), не `create_all`.
§18.8: изоляция через SAVEPOINT — `commit()` реально фиксируется внутри теста.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PhotoStatus
from app.repositories.profile_photos import ProfilePhotoRepository
from app.repositories.users import UserRepository
from app.services.photo_service import apply_moderation_result

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]


async def _profile_id(session: AsyncSession) -> int:
    """Анкета-черновик для фото; возвращает внутренний `id`."""
    phone = f"+7905{uuid_lib.uuid4().int % 10**7:07d}"
    user = await UserRepository(session).create(
        provider="phone", provider_user_id=phone, phone=phone, gender="female"
    )
    result = await session.execute(
        text(
            "INSERT INTO profiles (user_id, gender, status) "
            "VALUES (:user_id, 'female', 'draft') RETURNING id"
        ),
        {"user_id": user.id},
    )
    profile_id: int = result.scalar_one()
    return profile_id


async def _create_photo(session: AsyncSession, profile_id: int, *, position: int = 1) -> Any:
    """Создаёт фото через репозиторий, НЕ передавая `status` явно.

    Именно отсутствие аргумента — предмет Task 6.1: значение обязано появиться
    из Python-дефолта модели (`default=str(PhotoStatus.PENDING)`), а не быть
    присвоенным тестом.
    """
    return await ProfilePhotoRepository(session).create(
        session,
        SimpleNamespace(id=profile_id),
        photo_uuid=uuid_lib.uuid4(),
        storage_key=f"photos/x/{uuid_lib.uuid4()}.jpg",
        position=position,
        content_type="image/jpeg",
        size_bytes=1024,
        consent_given_at=datetime.now(UTC),
    )


# --- стори 2.5: модерация фото не меняет анкету (AC-03) ---------------------------


async def _profile_snapshot(session: AsyncSession, profile_id: int) -> dict[str, Any]:
    """Все колонки строки `profiles` — снимок для сравнения до/после.

    Читается сырым SQL: ORM-объект в identity map мог бы «подтвердить»
    неизменность, которой в БД нет (§18.2).
    """
    result = await session.execute(
        text("SELECT * FROM profiles WHERE id = :id"), {"id": profile_id}
    )
    row = result.mappings().one()
    return dict(row)


async def test_rejected_photo_keeps_profile_unchanged(session: AsyncSession) -> None:
    """Task 6.2, 6.3, 9.7 (AC-03): отклонение фото не меняет `profiles`.

    Проверяется снимок ВСЕХ колонок `profiles` до и после решения модератора:
    решение по фото — факт таблицы `profile_photos`, а не анкеты. §18.1: если
    `apply_moderation_result` начнёт писать в `profiles` (например, сбрасывать
    `status` в `draft` при `rejected`), тест краснеет на первом же отличии.

    Второй ассерт — вход правила видимости эпика 4 (D-10): после отказа по
    одному из трёх фото `list_approved_by_profile` непуст, то есть анкета
    остаётся доступной для выборки.
    """
    profile_id = await _profile_id(session)
    # Анкета заполнена: именно её видимость проверяется. Все четыре поля
    # обязательны — CHECK `ck_profiles_completeness` не даст поставить
    # `status='complete'` на пустую строку (проверено падением теста).
    await session.execute(
        text(
            "UPDATE profiles SET name = :name, city = :city, age = :age, bio = :bio, "
            "status = 'complete' WHERE id = :id"
        ),
        {"name": "Аня", "city": "Томск", "age": 32, "bio": "люблю горы", "id": profile_id},
    )
    await session.flush()

    photos = [await _create_photo(session, profile_id, position=position) for position in (1, 2, 3)]
    for photo in photos:
        await apply_moderation_result(session, photo.uuid, status=PhotoStatus.APPROVED)

    before = await _profile_snapshot(session, profile_id)
    assert before["status"] == "complete"

    rejected = await apply_moderation_result(
        session, photos[1].uuid, status=PhotoStatus.REJECTED, rejection_reason="не видно лица"
    )

    after = await _profile_snapshot(session, profile_id)
    # AC-03: ни одно поле анкеты не изменилось — включая `updated_at`.
    assert after == before
    assert rejected.status == str(PhotoStatus.REJECTED)
    # AC-03, D-10: анкета не исчезает из выборки — два `approved` остались.
    approved = await ProfilePhotoRepository(session).list_approved_by_profile(profile_id)
    assert [photo.uuid for photo in approved] == [photos[0].uuid, photos[2].uuid]


# --- 6.1: INSERT без `refresh` ---------------------------------------------------


async def test_insert_values_readable_without_refresh(session: AsyncSession) -> None:
    """Task 6.1 (AC-10): `status` и `created_at` читаются после `flush`.

    Долг CR 2.2: раньше значения брались `refresh`-ем, и переход на «тонкий»
    flush оставил бы `None` в ответе API. Тест краснеет, если убрать
    `default=str(PhotoStatus.PENDING)` ИЛИ `server_default`, И `flush`
    перестанет возвращать значения.

    `refresh` здесь НЕ вызывается — в этом весь смысл проверки.
    """
    profile_id = await _profile_id(session)

    photo = await _create_photo(session, profile_id)

    assert photo.status == str(PhotoStatus.PENDING)
    assert photo.created_at is not None
    assert photo.moderated_at is None
    assert photo.rejection_reason is None


# --- 6.3: UPDATE без `refresh` ---------------------------------------------------


async def test_update_values_readable_without_refresh(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 6.3 (AC-10): после решения `status`, `moderated_at`, `updated_at` есть.

    §18.1: заменить мутацию ORM на `update()` — `status`/`moderated_at`
    останутся старыми (identity map), тест покраснеет (см. 6.4).

    `updated_at` проверяется отдельно: `onupdate=func.now()` считается БД,
    и без возврата значения объект нёс бы прежнее время (D-5 `models/base.py`).
    """
    profile_id = await _profile_id(session)
    photo = await _create_photo(session, profile_id)

    # CR 2.4 патч 8: часы репозитория подменяются ФИКСИРОВАННЫМ будущим
    # временем, а не сравниваются `>=` с прежним значением. Прежний ассерт
    # `photo.updated_at >= updated_before` не мог упасть: время монотонно,
    # и равенство проходило. Теперь при удалении строки
    # `photo.updated_at = datetime.now(UTC)` в `set_moderation_result`
    # значение останется прежним и тест покраснеет на точном равенстве.
    sentinel = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # noqa: ANN401
            return sentinel

    monkeypatch.setattr("app.repositories.profile_photos.datetime", _FrozenDatetime)

    moderated = await apply_moderation_result(session, photo.uuid, status=PhotoStatus.APPROVED)

    assert moderated is photo  # тот же объект из identity map
    assert photo.status == str(PhotoStatus.APPROVED)
    assert photo.moderated_at is not None
    assert photo.moderated_at.tzinfo is not None
    assert photo.rejection_reason is None
    # CR 2.4 патч 8: точное значение из подменённых часов. Это единственная
    # проверка, которая действительно падает, если `updated_at` перестали
    # обновлять (см. docstring теста).
    assert photo.updated_at == sentinel


async def test_rejected_values_readable_without_refresh(session: AsyncSession) -> None:
    """Task 6.3 (AC-05, AC-10): причина отказа видна сразу после записи.

    §18.1: убрать `photo.rejection_reason = ...` в
    `set_moderation_result` — тест покраснеет на `None`.
    """
    profile_id = await _profile_id(session)
    photo = await _create_photo(session, profile_id)

    await apply_moderation_result(
        session,
        photo.uuid,
        status=PhotoStatus.REJECTED,
        rejection_reason="  не видно лица  ",
    )

    assert photo.status == str(PhotoStatus.REJECTED)
    assert photo.rejection_reason == "не видно лица"
    assert photo.moderated_at is not None


# --- 8.7: доказательство откатом доменной проверки -------------------------------


async def test_database_check_rejects_blank_reason(session: AsyncSession) -> None:
    """Task 8.7 (AC-05, AC-06): БД отвергает пустую причину — `IntegrityError`.

    Это доказательство откатом для Task 8.3: если убрать доменную проверку
    причины, `apply_moderation_result` дойдёт до `flush`, и PostgreSQL
    выбросит `IntegrityError` от `ck_profile_photos_rejection_reason`.
    Наружу это ушло бы как `500` на штатной ошибке модератора.

    Проверка идёт сырым SQL намеренно: она фиксирует СВОЙСТВО СХЕМЫ,
    не поведение сервиса (оно покрыто unit-тестом 8.3).
    """
    profile_id = await _profile_id(session)
    photo = await _create_photo(session, profile_id)

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "UPDATE profile_photos SET status = 'rejected', "
                "rejection_reason = :reason, moderated_at = now() "
                "WHERE uuid = :uuid"
            ),
            {"reason": "   ", "uuid": photo.uuid},
        )

    await session.rollback()


async def test_database_check_rejects_rejected_without_reason(
    session: AsyncSession,
) -> None:
    """Task 8.7 (AC-06): `rejected` без `moderated_at`/причины запрещён CHECK-ом.

    `ck_profile_photos_status_consistency` требует для `rejected` и время
    решения, и непустую причину. Без этого «отклонено» было бы неотличимо
    от «в очереди» по полям модерации.
    """
    profile_id = await _profile_id(session)
    photo = await _create_photo(session, profile_id)

    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE profile_photos SET status = 'rejected' WHERE uuid = :uuid"),
            {"uuid": photo.uuid},
        )

    await session.rollback()


# --- 6.4: инвариант против `update()` --------------------------------------------


async def test_moderation_goes_through_orm_not_bulk_update(session: AsyncSession) -> None:
    """Task 6.4 (AC-10, D-6): решение пишется мутацией ORM, не bulk `update()`.

    Первая редакция этого теста ("значения читаются после `update()`") НЕ МОГЛА
    УПАСТЬ: SQLAlchemy 2.0 для `session.execute(update(...))` синхронизирует
    identity map (`synchronize_session='auto'`), и `photo.status` в памяти
    обновляется так же, как при мутации. Это тавтология по §18.2 — проверено
    заменой реализации на `update()`: тест остался зелёным.

    Настоящее различие документировано: bulk `update()` НЕ вызывает ORM-события
    `before_update`. Поэтому проверяется именно событие. `update()` в
    `set_moderation_result` — тест краснеет (проверено откатом).

    Почему это важно для продукта: без ORM-события не сработают будущие
    обработчики на `ProfilePhoto` (аудит модерации, уведомление владельцу
    в эпике 7) — они подписываются на события, а не на SQL.
    """
    from sqlalchemy import event

    from app.models.profile_photo import ProfilePhoto

    fired: list[str] = []

    def _on_before_update(mapper: Any, connection: Any, target: Any) -> None:
        fired.append("before_update")

    event.listen(ProfilePhoto, "before_update", _on_before_update)
    try:
        profile_id = await _profile_id(session)
        photo = await _create_photo(session, profile_id)

        await apply_moderation_result(session, photo.uuid, status=PhotoStatus.APPROVED)
    finally:
        event.remove(ProfilePhoto, "before_update", _on_before_update)

    assert fired == ["before_update"]
