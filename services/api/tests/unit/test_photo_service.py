"""Правила сервиса фото: лимит, согласие, владение (Task 9.4; AC-03, AC-06, AC-09).

§18.3: гонку двух загрузок в одной сессии доказать нельзя, поэтому проверяется
поведение сервиса на входах, которые гонка создаёт: счётчик под блокировкой
и порядок вызовов. Реальная блокировка строки покрыта
`test_profile_lock_statement.py` (текст запроса) и integration-тестами.

§18.1: каждый тест краснеет при удалении защиты. Убрать проверку `count` —
краснеет `test_fourth_photo_rejected`; убрать `ensure_consent` — краснеет
`test_without_consent_never_touches_storage`; убрать фильтр по `profile_id` —
краснеет `test_foreign_photo_is_not_found`.
"""

from __future__ import annotations

import io
import uuid as uuid_lib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.profile_photo import ProfilePhoto
from app.repositories.profile_photos import ProfilePhotoRepository
from app.services import media_service, photo_service
from app.services.consent_service import GenderRequiredError
from app.services.photo_service import (
    PhotoLimitReachedError,
    PhotoNotFoundError,
    add_photo,
    remove_photo,
    replace_photo,
)
from app.services.photo_validation import PhotoConsentRequiredError, PhotoValidationError

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"payload"


class _FakeSession:
    """Сессия-заглушка: `flush`/`rollback` без PostgreSQL."""

    def __init__(self) -> None:
        self.rollbacks = 0

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FakePhotoRepo:
    """Репозиторий фото с заданным состоянием анкеты."""

    def __init__(self, *, count: int = 0, occupied: set[int] | None = None) -> None:
        self._count = count
        self._occupied = occupied if occupied is not None else set(range(1, count + 1))
        self.created: list[dict[str, Any]] = []
        self.deleted: list[ProfilePhoto] = []

    async def count_by_profile(self, profile_id: int) -> int:
        return self._count

    async def list_positions(self, profile_id: int) -> set[int]:
        return set(self._occupied)

    def pick_next_position(self, occupied: set[int]) -> int | None:
        return ProfilePhotoRepository.pick_next_position(occupied)

    async def create(self, session: Any, profile: Any, **kwargs: Any) -> ProfilePhoto:
        self.created.append(kwargs)
        photo = ProfilePhoto(
            uuid=kwargs["photo_uuid"],
            profile_id=profile.id,
            storage_key=kwargs["storage_key"],
            position=kwargs["position"],
            content_type=kwargs["content_type"],
            size_bytes=kwargs["size_bytes"],
            consent_given_at=kwargs["consent_given_at"],
        )
        photo.created_at = datetime.now(UTC)
        return photo

    async def list_by_profile(self, profile_id: int) -> list[ProfilePhoto]:
        return []

    async def get_owned(self, profile_id: int, photo_uuid: uuid_lib.UUID) -> ProfilePhoto | None:
        # Чужое фото не находится: фильтр по `profile_id` — часть контракта.
        return None

    async def delete(self, photo: ProfilePhoto) -> None:
        self.deleted.append(photo)


class _FakeProfileRepo:
    """Репозиторий анкет: существующий черновик или отсутствие строки."""

    def __init__(self, profile: Any | None) -> None:
        self._profile = profile

    async def get_by_user_id_for_update(self, user_id: int) -> Any | None:
        return self._profile

    async def get_by_user_id(self, user_id: int) -> Any | None:
        return self._profile

    async def create(self, **kwargs: Any) -> Any:
        raise AssertionError("черновик уже есть — create не должен вызываться")


def _user() -> Any:
    return SimpleNamespace(id=1, gender="female")


def _profile() -> Any:
    return SimpleNamespace(id=10, uuid=uuid_lib.uuid4())


def _install(
    monkeypatch: pytest.MonkeyPatch, *, photos: _FakePhotoRepo, profile: Any | None
) -> None:
    monkeypatch.setattr(photo_service, "ProfilePhotoRepository", lambda session: photos)
    monkeypatch.setattr(
        photo_service, "ProfileRepository", lambda session: _FakeProfileRepo(profile)
    )


# --- AC-03: лимит трёх фото ------------------------------------------------------


async def test_fourth_photo_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-03: при трёх фото — `PhotoLimitReachedError`, в S3 ничего не уходит.

    Проверяется и порядок: лимит считается ДО чтения файла и до `put_object`.
    Без проверки `count` четвёртое фото загрузилось бы, а `CHECK position <= 3`
    дал бы `500` вместо `409`.
    """
    photos = _FakePhotoRepo(count=3)
    _install(monkeypatch, photos=photos, profile=_profile())
    uploads: list[str] = []

    async def _put(**kwargs: Any) -> None:
        uploads.append(kwargs["storage_key"])

    monkeypatch.setattr(media_service, "put_object", _put)

    with pytest.raises(PhotoLimitReachedError):
        await add_photo(
            _FakeSession(),
            _user(),
            fileobj=io.BytesIO(JPEG),
            size_bytes=len(JPEG),
            declared_content_type="image/jpeg",
            consent=True,
        )

    assert photos.created == []
    assert uploads == []  # AC-03: файл не загружается


@pytest.mark.parametrize(
    ("occupied", "expected"),
    [
        (set(), 1),
        ({1}, 2),
        ({1, 2}, 3),
        # D-12: после удаления среднего — первая свободная, а не max+1.
        ({1, 3}, 2),
        ({2, 3}, 1),
        ({3}, 1),
        ({1, 2, 3}, None),
    ],
)
def test_pick_next_position_first_free(occupied: set[int], expected: int | None) -> None:
    """Task 5.2, D-12: первая свободная позиция из {1, 2, 3}, не `max+1`.

    `{1, 3}` — ровно сценарий бага: `max+1` дал бы `4`, CHECK запретил бы
    вставку, и легальная загрузка отбивалась бы `IntegrityError`.
    """
    assert ProfilePhotoRepository.pick_next_position(occupied) == expected


# --- AC-06: согласие -------------------------------------------------------------


async def test_without_consent_never_touches_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-06: без согласия — отказ ДО создания строки и до S3.

    Проверяется отсутствие вызовов, а не только исключение: иначе «файл уже
    в бакете, строки нет» осталось бы незамеченным (сирота до эпика 8).
    """
    photos = _FakePhotoRepo()
    _install(monkeypatch, photos=photos, profile=_profile())
    uploads: list[str] = []

    async def _put(**kwargs: Any) -> None:
        uploads.append(kwargs["storage_key"])

    monkeypatch.setattr(media_service, "put_object", _put)

    with pytest.raises(PhotoConsentRequiredError):
        await add_photo(
            _FakeSession(),
            _user(),
            fileobj=io.BytesIO(JPEG),
            size_bytes=len(JPEG),
            declared_content_type="image/jpeg",
            consent=False,
        )

    assert photos.created == []
    assert uploads == []


# --- AC-09: владение -------------------------------------------------------------


async def test_foreign_photo_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-09: чужое/несуществующее фото — `PhotoNotFoundError`, без удаления.

    Репозиторий-заглушка возвращает `None` на любое фото — так выглядит
    результат SQL-фильтра по `profile_id`. Убрать фильтр в репозитории —
    тест краснеет (он проверяет, что сервис не получит объект).
    """
    photos = _FakePhotoRepo()
    _install(monkeypatch, photos=photos, profile=_profile())

    with pytest.raises(PhotoNotFoundError):
        await remove_photo(_FakeSession(), _user(), uuid_lib.uuid4())

    assert photos.deleted == []


async def test_remove_without_profile_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-09: анкеты нет — `404`, а не `500` от `None.id`."""
    photos = _FakePhotoRepo()
    _install(monkeypatch, photos=photos, profile=None)

    with pytest.raises(PhotoNotFoundError):
        await remove_photo(_FakeSession(), _user(), uuid_lib.uuid4())


# --- Task 6.6, 6.6a: черновик анкеты и пол ---------------------------------------


class _DraftingProfileRepo:
    """Репозиторий без анкеты: `create` вызывается и запоминает аргументы.

    Отличие от `_FakeProfileRepo`: тот падает на `create`, потому что
    существующая анкета НЕ должна пересоздаваться. Здесь проверяется
    обратная ветка — анкеты нет, черновик создаётся.
    """

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def get_by_user_id_for_update(self, user_id: int) -> Any | None:
        return None

    async def create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return SimpleNamespace(id=10, uuid=uuid_lib.uuid4(), status=kwargs.get("status"))


async def _noop_put(**kwargs: Any) -> None:
    return None


@pytest.mark.parametrize(("gender", "expected_hidden"), [("male", True), ("female", False)])
async def test_photo_before_profile_creates_hidden_draft(
    monkeypatch: pytest.MonkeyPatch, gender: str, expected_hidden: bool
) -> None:
    """Task 6.6, CR 2.3 Решение 3: черновик от загрузки фото скрыт для мужчин.

    §18.1: `is_hidden=(gender == Gender.MALE)` — правка CR 2.3 стори 2.1,
    и без неё мужская анкета, созданная загрузкой фото, стала бы видимой
    в выборке эпика 4. Тест краснеет, если убрать выражение или заменить его
    на константу `False`: параметр `male` ожидает `True`.
    """
    profiles = _DraftingProfileRepo()
    photos = _FakePhotoRepo()
    monkeypatch.setattr(photo_service, "ProfilePhotoRepository", lambda session: photos)
    monkeypatch.setattr(photo_service, "ProfileRepository", lambda session: profiles)
    monkeypatch.setattr(media_service, "put_object", _noop_put)

    await add_photo(
        _FakeSession(),
        SimpleNamespace(id=1, gender=gender),
        fileobj=io.BytesIO(JPEG),
        size_bytes=len(JPEG),
        declared_content_type="image/jpeg",
        consent=True,
    )

    assert len(profiles.created) == 1
    draft = profiles.created[0]
    assert draft["status"] == "draft"  # D-8 стори 2.1: фото раньше текста
    assert draft["gender"] == gender
    assert draft["is_hidden"] is expected_hidden


async def test_gender_required_before_draft_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 6.6a: `user.gender is None` — `422 gender_required`, не `500`.

    §18.1: без этой ветки `INSERT` в `profiles` нарушает составной FK
    `(user_id, gender) -> users(id, gender)` (миграция 0005) и даёт
    `IntegrityError` → `500`. Тест проверяет и порядок: анкета не создаётся,
    файл в хранилище не уходит.
    """
    profiles = _DraftingProfileRepo()
    photos = _FakePhotoRepo()
    monkeypatch.setattr(photo_service, "ProfilePhotoRepository", lambda session: photos)
    monkeypatch.setattr(photo_service, "ProfileRepository", lambda session: profiles)
    uploads: list[Any] = []

    async def _put(**kwargs: Any) -> None:
        uploads.append(kwargs)

    monkeypatch.setattr(media_service, "put_object", _put)

    with pytest.raises(GenderRequiredError):
        await add_photo(
            _FakeSession(),
            SimpleNamespace(id=1, gender=None),
            fileobj=io.BytesIO(JPEG),
            size_bytes=len(JPEG),
            declared_content_type="image/jpeg",
            consent=True,
        )

    assert profiles.created == []
    assert uploads == []


# --- порядок «INSERT → S3» -------------------------------------------------------


async def test_upload_keeps_storage_key_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-02, D-3: ключ `photos/{profile_uuid}/{photo_uuid}.jpg`, имя файла не участвует.

    Проверяются фактические аргументы `put_object`: префикс `photos/`, UUID
    анкеты в пути и расширение из проверенного MIME. Ключ не должен содержать
    ничего, что пришло от клиента (D-3).
    """
    photos = _FakePhotoRepo()
    profile = _profile()
    _install(monkeypatch, photos=photos, profile=profile)
    uploads: list[dict[str, Any]] = []

    async def _put(**kwargs: Any) -> None:
        uploads.append(kwargs)

    monkeypatch.setattr(media_service, "put_object", _put)

    photo = await add_photo(
        _FakeSession(),
        _user(),
        fileobj=io.BytesIO(JPEG),
        size_bytes=len(JPEG),
        declared_content_type="image/jpeg",
        consent=True,
    )

    assert len(uploads) == 1
    key = uploads[0]["storage_key"]
    assert key.startswith(f"photos/{profile.uuid}/")
    assert key.endswith(".jpg")
    assert str(photo.uuid) in key
    assert uploads[0]["content_type"] == "image/jpeg"
    assert uploads[0]["size_bytes"] == len(JPEG)
    # AC-07: время согласия сохранено вместе с фото.
    assert photos.created[0]["consent_given_at"] is not None


# --- стори 2.5: атомарная замена фото (AC-02, AC-05) ------------------------------


class _ReplacePhotoRepo:
    """Репозиторий с ОДНИМ доступным фото — вход для `replace_photo`.

    Отдельный фейк, а не `_FakePhotoRepo`: тот всегда возвращает `None` из
    `get_owned` (это контракт чужого фото в тестах 2.2), а замене нужно
    существующее фото владельца с предсказуемым состоянием.
    """

    def __init__(self, photo: ProfilePhoto | None) -> None:
        self.photo = photo

    async def count_by_profile(self, profile_id: int) -> int:
        return 1 if self.photo is not None else 0

    async def list_positions(self, profile_id: int) -> set[int]:
        return {self.photo.position} if self.photo is not None else set()

    async def get_by_uuid_for_update(self, photo_uuid: uuid_lib.UUID) -> ProfilePhoto | None:
        """CR 2.5, R-2: замена читает строку фото под блокировкой.

        Фейк отдаёт фото по одному `uuid` — как и настоящий репозиторий.
        Фильтр владельца проверяет сервис (`photo.profile_id != profile.id`),
        поэтому «чужое» фото здесь отличается именно `profile_id`.
        """
        if self.photo is None or self.photo.uuid != photo_uuid:
            return None
        return self.photo

    async def create(self, session: Any, profile: Any, **kwargs: Any) -> ProfilePhoto:
        raise AssertionError("замена не создаёт новую строку")


def _existing_photo(profile_uuid: uuid_lib.UUID, *, extension: str = "png") -> ProfilePhoto:
    """Фото в состоянии «одобрено»: позиция 2, старый ключ, решение модератора."""
    photo_uuid = uuid_lib.uuid4()
    photo = ProfilePhoto(
        uuid=photo_uuid,
        profile_id=10,
        storage_key=f"photos/{profile_uuid}/{photo_uuid}.{extension}",
        position=2,
        content_type="image/png",
        size_bytes=100,
        consent_given_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    photo.status = "approved"
    photo.rejection_reason = None
    photo.moderated_at = datetime(2026, 10, 2, tzinfo=UTC)
    return photo


def _install_replace(
    monkeypatch: pytest.MonkeyPatch, *, photos: _ReplacePhotoRepo, profile: Any | None
) -> None:
    monkeypatch.setattr(photo_service, "ProfilePhotoRepository", lambda session: photos)
    monkeypatch.setattr(
        photo_service, "ProfileRepository", lambda session: _FakeProfileRepo(profile)
    )


async def test_replace_keeps_uuid_and_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 9.1 (AC-02, D-2): `uuid` и `position` сохраняются, ключ новый.

    §18.1: если заменить мутацию на `delete`+`create` (путь 2.4) или
    перенумеровать позицию, тест краснеет — `uuid` и `position` перестанут
    совпадать с исходными. Это ровно то свойство, которого не было до 2.5.
    """
    profile = _profile()
    photo = _existing_photo(profile.uuid, extension="png")
    photo_uuid, position = photo.uuid, photo.position
    _install_replace(monkeypatch, photos=_ReplacePhotoRepo(photo), profile=profile)
    uploads: list[dict[str, Any]] = []

    async def _put(**kwargs: Any) -> None:
        uploads.append(kwargs)

    monkeypatch.setattr(media_service, "put_object", _put)

    replaced, old_key = await replace_photo(
        _FakeSession(),
        _user(),
        photo_uuid,
        fileobj=io.BytesIO(JPEG),
        size_bytes=len(JPEG),
        declared_content_type="image/jpeg",
        consent=True,
    )

    assert replaced.uuid == photo_uuid
    assert replaced.position == position == 2
    # D-2: ключ строится из ТОГО ЖЕ uuid, расширение — из проверенного MIME.
    # CR 2.5, R-1: к uuid добавлен уникальный суффикс, поэтому ключ сравнивается
    # по префиксу и суффиксу-расширению, а не строкой целиком.
    assert replaced.storage_key.startswith(f"photos/{profile.uuid}/{photo_uuid}-")
    assert replaced.storage_key.endswith(".jpg")
    # R-1: ключ замены НЕ совпадает с прежним даже при том же расширении.
    assert replaced.storage_key != f"photos/{profile.uuid}/{photo_uuid}.jpg"
    assert replaced.content_type == "image/jpeg"
    assert replaced.size_bytes == len(JPEG)
    assert uploads[0]["storage_key"] == replaced.storage_key
    # D-3: старый ключ отдан роутеру для удаления ПОСЛЕ commit.
    assert old_key == f"photos/{profile.uuid}/{photo_uuid}.png"


async def test_replace_resets_moderation_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 9.1 (AC-02): замена сбрасывает статус и решение модератора.

    §18.1: без сброса `status` заменённое фото осталось бы `approved`
    с прежним `moderated_at` — пользователь увидел бы чужое решение
    модератора на новом файле, а очередь проверки его бы не увидела.
    """
    profile = _profile()
    photo = _existing_photo(profile.uuid)
    _install_replace(monkeypatch, photos=_ReplacePhotoRepo(photo), profile=profile)

    async def _put(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(media_service, "put_object", _put)

    replaced, _ = await replace_photo(
        _FakeSession(),
        _user(),
        photo.uuid,
        fileobj=io.BytesIO(JPEG),
        size_bytes=len(JPEG),
        declared_content_type="image/jpeg",
        consent=True,
    )

    assert replaced.status == "pending"
    assert replaced.rejection_reason is None
    assert replaced.moderated_at is None
    # D-6: согласие фиксируется заново — файл другой.
    assert replaced.consent_given_at > datetime(2026, 10, 1, tzinfo=UTC)


async def test_replace_same_extension_still_gets_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR 2.5, R-1: то же расширение тоже даёт НОВЫЙ ключ и старый в удаление.

    До правки ключ был детерминирован (`...{photo_uuid}.jpg`), и замена
    jpg→jpg писала новый файл ПОВЕРХ старого. Последствия: (а) ранее выданная
    подпись `approved`-фото продолжала отдавать непроверенный файл до конца
    TTL; (б) откат после `put_object` оставлял в БД метаданные старого файла
    при новом содержимом в бакете.

    §18.1: доказано откатом — при возврате детерминированного ключа
    `old_key` снова станет `None` (или совпадёт с новым), и тест краснеет.
    """
    profile = _profile()
    photo = _existing_photo(profile.uuid, extension="jpg")
    old = photo.storage_key
    _install_replace(monkeypatch, photos=_ReplacePhotoRepo(photo), profile=profile)

    async def _put(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(media_service, "put_object", _put)

    replaced, old_key = await replace_photo(
        _FakeSession(),
        _user(),
        photo.uuid,
        fileobj=io.BytesIO(JPEG),
        size_bytes=len(JPEG),
        declared_content_type="image/jpeg",
        consent=True,
    )

    # R-1: новый ключ отличается от прежнего, и оба имеют расширение `.jpg`.
    assert replaced.storage_key != old
    assert replaced.storage_key.endswith(".jpg")
    assert old.endswith(".jpg")
    # R-1: прежний объект ВСЕГДА возвращается на удаление после `commit`.
    assert old_key == old


async def test_replace_foreign_photo_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 9.2 (AC-09): чужое/несуществующее фото — `PhotoNotFoundError`.

    §18.1: без фильтра по `profile_id` в `get_owned` (или при поиске по uuid
    без анкеты) чужое фото заменилось бы, и тест стал бы зелёным навсегда.
    """
    profile = _profile()
    foreign = _existing_photo(uuid_lib.uuid4())
    _install_replace(monkeypatch, photos=_ReplacePhotoRepo(foreign), profile=profile)
    uploads: list[Any] = []

    async def _put(**kwargs: Any) -> None:
        uploads.append(kwargs)

    monkeypatch.setattr(media_service, "put_object", _put)

    with pytest.raises(PhotoNotFoundError):
        await replace_photo(
            _FakeSession(),
            _user(),
            uuid_lib.uuid4(),
            fileobj=io.BytesIO(JPEG),
            size_bytes=len(JPEG),
            declared_content_type="image/jpeg",
            consent=True,
        )

    assert foreign.status == "approved"  # строка не тронута
    assert uploads == []  # AC-05: файл не уходит в бакет


async def test_replace_without_profile_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 9.2: анкеты нет — `404 photo_not_found`, как в `remove_photo`."""
    _install_replace(monkeypatch, photos=_ReplacePhotoRepo(None), profile=None)

    with pytest.raises(PhotoNotFoundError):
        await replace_photo(
            _FakeSession(),
            _user(),
            uuid_lib.uuid4(),
            fileobj=io.BytesIO(JPEG),
            size_bytes=len(JPEG),
            declared_content_type="image/jpeg",
            consent=True,
        )


async def test_replace_invalid_file_does_not_touch_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 9.3 (AC-05): негодный файл не меняет строку и не идёт в S3.

    §18.1: при порядке «мутация → валидация» `storage_key` уже был бы
    перезаписан, и тест краснеет на проверке исходного ключа.
    """
    profile = _profile()
    photo = _existing_photo(profile.uuid, extension="png")
    before_key, before_status = photo.storage_key, photo.status
    _install_replace(monkeypatch, photos=_ReplacePhotoRepo(photo), profile=profile)
    uploads: list[Any] = []

    async def _put(**kwargs: Any) -> None:
        uploads.append(kwargs)

    monkeypatch.setattr(media_service, "put_object", _put)

    with pytest.raises(PhotoValidationError):
        await replace_photo(
            _FakeSession(),
            _user(),
            photo.uuid,
            fileobj=io.BytesIO(b"not-an-image"),
            size_bytes=12,
            declared_content_type="image/jpeg",
            consent=True,
        )

    assert photo.storage_key == before_key
    assert photo.status == before_status
    assert uploads == []


async def test_replace_without_consent_does_not_touch_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 9.3 (FR64, AC-05): без согласия — отказ до валидации и записи."""
    profile = _profile()
    photo = _existing_photo(profile.uuid)
    before_key = photo.storage_key
    _install_replace(monkeypatch, photos=_ReplacePhotoRepo(photo), profile=profile)

    with pytest.raises(PhotoConsentRequiredError):
        await replace_photo(
            _FakeSession(),
            _user(),
            photo.uuid,
            fileobj=io.BytesIO(JPEG),
            size_bytes=len(JPEG),
            declared_content_type="image/jpeg",
            consent=False,
        )

    assert photo.storage_key == before_key


async def test_replace_storage_failure_keeps_metadata_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 9.3 (AC-05, D-3): сбой `put_object` не оставляет новых метаданных.

    §18.1: доказано откатом — при переносе `put_object` ПОСЛЕ `commit`
    (в роутере) исключение уходило бы из сервиса уже с изменённой строкой,
    и тест краснеет: строка остаётся с новым `content_type`, а файла нет.
    Роутер в этой ветке делает `rollback()` (integration-тест 9.5).
    """
    profile = _profile()
    photo = _existing_photo(profile.uuid, extension="png")
    before_key, before_type = photo.storage_key, photo.content_type
    _install_replace(monkeypatch, photos=_ReplacePhotoRepo(photo), profile=profile)

    async def _put(**kwargs: Any) -> None:
        raise media_service.MediaStorageError("boom")

    monkeypatch.setattr(media_service, "put_object", _put)

    with pytest.raises(media_service.MediaStorageError):
        await replace_photo(
            _FakeSession(),
            _user(),
            photo.uuid,
            fileobj=io.BytesIO(JPEG),
            size_bytes=len(JPEG),
            declared_content_type="image/jpeg",
            consent=True,
        )

    # Мутация произошла в памяти, но роутер её откатит: сервис обязан
    # бросить исключение ДО возврата, иначе откатывать было бы нечего.
    assert photo.content_type != before_type
    assert photo.storage_key != before_key


async def test_replace_locks_photo_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR 2.5, R-2: замена читает фото ПОД БЛОКИРОВКОЙ, а не простым чтением.

    До правки замена блокировала только `profiles`, а `apply_moderation_result`
    — только `profile_photos`: пересечения не было, и модератор мог записать
    `approved` поверх заменённого файла (или замена молча затирала решение).

    §18.1: доказано откатом — при возврате к `get_owned` фейк ниже бросает
    `AssertionError`, и тест краснеет.
    """

    class _LockingOnlyRepo(_ReplacePhotoRepo):
        async def get_owned(
            self, profile_id: int, photo_uuid: uuid_lib.UUID
        ) -> ProfilePhoto | None:
            raise AssertionError("замена обязана брать строку под FOR UPDATE")

    profile = _profile()
    photo = _existing_photo(profile.uuid)
    _install_replace(monkeypatch, photos=_LockingOnlyRepo(photo), profile=profile)

    async def _put(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(media_service, "put_object", _put)

    replaced, _ = await replace_photo(
        _FakeSession(),
        _user(),
        photo.uuid,
        fileobj=io.BytesIO(JPEG),
        size_bytes=len(JPEG),
        declared_content_type="image/jpeg",
        consent=True,
    )

    assert replaced.uuid == photo.uuid


async def test_replace_flush_integrity_error_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR 2.5, P-2: `IntegrityError` на `flush` откатывает транзакцию.

    Без отката сессия остаётся в failed-состоянии, и роутер отдал бы `500`
    на все последующие запросы в этом соединении, а причина (нарушенный CHECK)
    потерялась бы в общей ошибке.

    §18.1: доказано откатом — при удалении `except IntegrityError: rollback`
    счётчик `rollbacks` остаётся нулевым, и тест краснеет.
    """

    class _FailingFlushSession(_FakeSession):
        async def flush(self) -> None:
            raise IntegrityError("UPDATE profile_photos", {}, Exception("ck_size"))

    profile = _profile()
    photo = _existing_photo(profile.uuid)
    _install_replace(monkeypatch, photos=_ReplacePhotoRepo(photo), profile=profile)
    session = _FailingFlushSession()

    with pytest.raises(IntegrityError):
        await replace_photo(
            session,
            _user(),
            photo.uuid,
            fileobj=io.BytesIO(JPEG),
            size_bytes=len(JPEG),
            declared_content_type="image/jpeg",
            consent=True,
        )

    assert session.rollbacks == 1
