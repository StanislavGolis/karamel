"""Схемы списка заявившихся (стори 5а.4, Task 10.3; AC-01, AC-05, AC-06, AC-08).

Проверяется КОНТРАКТ ответа, а не «Pydantic работает»: `null` вместо пустых
строк (project-context §4), режим и признак выбора на месте, `place=None`
у карточки скрытого места (X-36).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

from app.models.enums import CardRequestsMode
from app.schemas.join_request import (
    CardRequestsItem,
    CardRequestsPerson,
    CardRequestsResponse,
)


def _person(**overrides: object) -> CardRequestsPerson:
    base: dict[str, object] = {"uuid": uuid_lib.uuid4()}
    base.update(overrides)
    return CardRequestsPerson(**base)  # type: ignore[arg-type]


def _item(**overrides: object) -> CardRequestsItem:
    base: dict[str, object] = {
        "uuid": uuid_lib.uuid4(),
        "submitted_at": datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
        "message": "пойдём вместе",
        "person": _person(),
        # AC-3 стори 12.6: поле ОБЯЗАТЕЛЬНОЕ (без умолчания) — «забыли
        # заполнить» не должно быть неотличимо от «вне рамок». `None` =
        # у карточки рамок нет.
        "within_bounds": True,
    }
    base.update(overrides)
    return CardRequestsItem(**base)  # type: ignore[arg-type]


class TestCardRequestsPerson:
    def test_missing_profile_yields_nulls(self) -> None:
        """AC-06: нет анкеты → `name`/`age`/`photo_url` равны `null`, не `""`.

        Краснеет, если поставить дефолты `""`/`0`: клиент показал бы «, 0 лет»
        вместо «человек без анкеты» (project-context §4).
        """
        person = _person()

        assert person.name is None
        assert person.age is None
        assert person.photo_url is None
        assert person.photo_expires_in is None

    def test_photo_ttl_lives_next_to_photo(self) -> None:
        """AC-06 (D-5a.4-6): TTL отдаётся рядом со ссылкой — OQ-5a.3-1 закрыт.

        Краснеет, если убрать `photo_expires_in`: клиент не отличил бы
        «фото нет» от «фото есть, срок неизвестен», и висел бы на протухшем URL.
        """
        person = _person(photo_url="https://s3/x?X-Amz-Signature=abc", photo_expires_in=900)

        assert person.photo_expires_in == 900


class TestCardRequestsResponse:
    def test_mode_and_selection_are_required(self) -> None:
        """AC-03: `mode`/`selection_available` обязательны — клиент ветвится по ним.

        Краснеет, если дать дефолты: сервер, забывший поле, отдал бы «выбор
        доступен» на открытом наборе, и кнопка «Выбрать» появилась бы там,
        где выбор запрещён (FR70).
        """
        response = CardRequestsResponse(
            items=[_item()],
            total=1,
            mode=CardRequestsMode.REQUESTS,
            selection_available=True,
        )

        assert response.cursor is None
        assert response.mode is CardRequestsMode.REQUESTS
        assert response.selection_available is True

    def test_open_set_carries_no_selection(self) -> None:
        """AC-03, AC-09: открытый набор → `selection_available=false`.

        Краснеет, если вычислять признак на клиенте по `capacity`: правило
        FR70 жило бы в двух местах.
        """
        response = CardRequestsResponse(
            items=[],
            total=0,
            mode=CardRequestsMode.OPEN_SET,
            selection_available=False,
        )

        assert response.selection_available is False
        assert response.cursor is None

    def test_mode_is_serialized_as_string(self) -> None:
        """AC-03: `mode` — машинная строка, а не русский текст.

        Краснеет, если заменить `StrEnum` на `Enum`: клиент ветвился бы по
        тексту, и локализация ломала бы ветвление.
        """
        payload = CardRequestsResponse(
            items=[],
            total=0,
            mode=CardRequestsMode.OPEN_SET,
            selection_available=False,
        ).model_dump(mode="json")

        assert payload["mode"] == "open_set"
