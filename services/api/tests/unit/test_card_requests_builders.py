"""Билдеры списка заявившихся (стори 5а.4, Task 10.1; AC-01, AC-04, AC-05).

§18.2: проверяется ТЕКСТ SQL — предикаты, `ORDER BY`, `LIMIT`, состав
`select`-списка. Тесты «функция вызвалась» тавтологичны и запрещены:
потеря `submitted_at` в `ORDER BY` или среза сообщества — реальные дефекты,
которые интеграционный тест объясняет косвенно (падением порядка), а здесь
видно прямо в запросе.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

from sqlalchemy.sql.selectable import ScalarSelect

from app.models.enums import JOIN_REQUEST_APPLICANT_STATUSES, JoinRequestStatus
from app.repositories.join_requests import JoinRequestRepository


class TestPageStatement:
    """AC-01, AC-04: страница заявившихся — порядок очереди и фильтр статусов."""

    def test_order_by_submitted_at_and_id(self) -> None:
        """AC-01 (ADR-05): порядок `submitted_at ASC, id ASC` — правило очереди.

        Краснеет, если убрать `id` из `ORDER BY`: при равных `submitted_at`
        порядок строк стал бы недетерминированным, и курсор (по `uuid`)
        терял бы или дублировал строки между страницами (мутация M1).
        """
        statement = JoinRequestRepository.build_card_requests_page_statement(
            card_id=1,
            city_slug="tomsk",
            statuses=JOIN_REQUEST_APPLICANT_STATUSES,
            after=None,
            limit=21,
        )
        sql = str(statement)

        assert "ORDER BY join_requests.submitted_at ASC" in sql
        assert "join_requests.id ASC" in sql

    def test_statuses_are_applied_as_parameter(self) -> None:
        """AC-04: набор статусов — из аргумента, а не константа внутри.

        Краснеет, если зашить `JOIN_REQUEST_VISIBLE_STATUSES` внутрь билдера:
        автор увидел бы `not_selected` (мутация M5), а открытый набор
        показывал бы `pending` вместо участников.
        """
        statement = JoinRequestRepository.build_card_requests_page_statement(
            card_id=7,
            city_slug="tomsk",
            statuses=(JoinRequestStatus.SELECTED,),
            after=None,
            limit=5,
        )
        # `IN (...)` компилируется в POSTCOMPILE-параметр: значение — СПИСОК,
        # а не строка. Проверяем именно его: подстановка литерала в текст
        # запроса (антипаттерн) прошла бы мимо этой проверки.
        params = statement.compile().params
        status_values = [
            str(item)
            for key, value in params.items()
            if key.startswith("status")
            for item in (value if isinstance(value, list) else [value])
        ]

        assert status_values == ["selected"]

    def test_card_id_and_city_slice(self) -> None:
        """AC-02: карточка по `card_id` и срез сообщества подзапросом.

        Краснеет, если снять срез сообщества: второй рубеж ADR-13 исчез бы,
        и ошибка сервиса отдала бы заявки чужого сообщества.
        """
        statement = JoinRequestRepository.build_card_requests_page_statement(
            card_id=42,
            city_slug="tomsk",
            statuses=JOIN_REQUEST_APPLICANT_STATUSES,
            after=None,
            limit=21,
        )
        sql = str(statement)

        assert "join_requests.card_id = " in sql
        assert "event_cards.city_slug = " in sql
        assert "FROM event_cards" in sql

    def test_limit_is_applied(self) -> None:
        """AC-05: `LIMIT` присутствует — без него страница выгрузила бы всё."""
        statement = JoinRequestRepository.build_card_requests_page_statement(
            card_id=1,
            city_slug="tomsk",
            statuses=JOIN_REQUEST_APPLICANT_STATUSES,
            after=None,
            limit=21,
        )

        assert "LIMIT " in str(statement)

    def test_joins_users_and_profiles(self) -> None:
        """AC-01, AC-06: имя и возраст — из JOIN'ов, а не вторым запросом.

        Краснеет, если убрать JOIN'ы: сборка элемента читала бы поля, которых
        нет в строке (`AttributeError`), либо пришлось бы делать N+1.
        """
        statement = JoinRequestRepository.build_card_requests_page_statement(
            card_id=1,
            city_slug="tomsk",
            statuses=JOIN_REQUEST_APPLICANT_STATUSES,
            after=None,
            limit=21,
        )
        sql = str(statement)

        assert "JOIN users" in sql
        assert "LEFT OUTER JOIN profiles" in sql

    def test_cursor_predicate_uses_scalar_id_subquery(self) -> None:
        """AC-01 (D-5a.4-2): курсор `(submitted_at, uuid)` → предикат `(submitted_at, id)`.

        Краснеет, если сравнивать кортеж напрямую с `uuid`: порядок по `uuid`
        не совпадает с порядком по `id`, и страницы теряли бы строки при
        равных `submitted_at` — главная ловушка этой стори.
        """
        after_uuid = uuid_lib.uuid4()
        statement = JoinRequestRepository.build_card_requests_page_statement(
            card_id=1,
            city_slug="tomsk",
            statuses=JOIN_REQUEST_APPLICANT_STATUSES,
            after=(datetime(2026, 10, 24, 12, 0, tzinfo=UTC), after_uuid),
            limit=21,
        )
        compiled = statement.compile()
        sql = str(compiled)

        # CR 5а.4, патч 4: прежняя версия этого теста закрепляла ДЕФЕКТНУЮ
        # форму — `(join_requests.submitted_at, join_requests.id) >` с
        # `ScalarSelect` в `compile().params`. Именно так SQLAlchemy рендерил
        # `tuple_(...) > (ts, scalar_subquery())`: подзапрос попадал в
        # bind-параметр, asyncpg получал объект `ScalarSelect` → `DBAPIError`
        # → `500` на каждой второй странице. Тест был зелёным, потому что
        # проверял форму, а не исполнял запрос.
        #
        # Теперь проверяется КОРРЕКТНАЯ форма: keyset разложен на ветви
        # `submitted_at > :ts OR (submitted_at = :ts AND id > (<подзапрос>))`,
        # и подзапрос-якорь обязан быть в ТЕКСТЕ запроса, а не в биндах.
        assert "join_requests.submitted_at > " in sql
        assert "join_requests.submitted_at = " in sql
        assert "join_requests.id > (SELECT join_requests.id" in sql
        assert "join_requests.uuid = " in sql

        # Ни одного `ScalarSelect` в bind-параметрах: попадание подзапроса
        # в параметр — ровно тот дефект, который давал `500` вживую.
        anchor_params = [
            value for value in compiled.params.values() if isinstance(value, ScalarSelect)
        ]
        assert anchor_params == [], f"подзапрос утёк в bind-параметр: {anchor_params}"


class TestCountStatement:
    """AC-05 (D-4.5-9): `total` без курсора и без JOIN'ов к `users`/`profiles`."""

    def test_count_has_no_cursor_and_no_user_joins(self) -> None:
        statement = JoinRequestRepository.build_card_requests_count_statement(
            card_id=1, statuses=JOIN_REQUEST_APPLICANT_STATUSES
        )
        sql = str(statement)

        assert "count(join_requests.id)" in sql
        assert "JOIN users" not in sql
        assert "profiles" not in sql
        assert "LIMIT" not in sql

    def test_count_applies_statuses(self) -> None:
        """AC-04: `total` считается по ТЕМ ЖЕ статусам, что и страница.

        Иначе «всего» включало бы `not_selected`, и автор видел бы отказы
        в счётчике, не видя их в списке.
        """
        statement = JoinRequestRepository.build_card_requests_count_statement(
            card_id=1, statuses=(JoinRequestStatus.SELECTED,)
        )
        # `IN (...)` — POSTCOMPILE-параметр со СПИСКОМ внутри (см. тест страницы).
        status_values = [
            str(item)
            for key, value in statement.compile().params.items()
            if key.startswith("status")
            for item in (value if isinstance(value, list) else [value])
        ]

        assert status_values == ["selected"]
