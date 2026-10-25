"""Билдеры запросов ленты и схемы ответа (стори 4.5, Task 8.2, 8.3).

§18.2: проверяется ТЕКСТ запроса без БД — так ловятся снятие среза
сообщества, подмена набора статусов и потеря tie-breaker'а в курсоре.
Каждый ассерт краснеет от конкретной правки в репозитории.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime

from app.models.enums import FEED_STATUSES, EventCardStatus
from app.repositories.event_cards import EventCardRepository
from app.schemas.event_card import EventCardRead, EventCardRef

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


def _sql(statement: object) -> str:
    """Текст запроса с подставленными параметрами — для поиска подстрок."""
    return str(statement)


def test_feed_statement_filters_by_community_status_and_future() -> None:
    """AC-01: срез сообщества, набор статусов ленты и `starts_at >= now`.

    Краснеет от снятия любого из трёх условий:
    - без `community_slug` лента показывала бы чужие города;
    - без `status IN` в выдачу попали бы `draft`/`cancelled`;
    - без `starts_at >=` — прошедшие встречи.
    """
    statement = EventCardRepository.build_feed_statement(
        community_slug="tomsk", now=_NOW, category=None, has_seats=False, after=None, limit=21
    )
    sql = _sql(statement)

    assert "event_cards.community_slug = " in sql
    assert "event_cards.status IN " in sql
    assert "event_cards.starts_at >= " in sql
    # Значения набора уходят postcompile-параметрами (`.in_()`), поэтому
    # в тексте их нет — набор проверяется отдельным тестом ниже.


def test_feed_statement_uses_only_published_and_full() -> None:
    """AC-01: набор статусов — ровно `published`/`full`, не «всё кроме draft».

    Краснеет, если заменить `FEED_STATUSES` на `!= 'draft'`: `closed`,
    `finished` и `cancelled` попали бы в ленту.
    """
    assert set(FEED_STATUSES) == {EventCardStatus.PUBLISHED, EventCardStatus.FULL}


def test_feed_statement_orders_by_starts_at_then_uuid() -> None:
    """AC-08: сортировка — РОВНО ключ курсора `(starts_at, uuid)`.

    Краснеет, если убрать tie-breaker `uuid`: при равных `starts_at` порядок
    станет неопределённым, и keyset-сравнение начнёт терять строки.
    """
    statement = EventCardRepository.build_feed_statement(
        community_slug="tomsk", now=_NOW, category=None, has_seats=False, after=None, limit=21
    )
    sql = _sql(statement)

    assert "ORDER BY event_cards.starts_at ASC, event_cards.uuid ASC" in sql


def test_feed_statement_applies_keyset_tuple() -> None:
    """AC-08: keyset — сравнение КОРТЕЖА, а не двух `OR`-ветвей.

    Краснеет, если заменить `tuple_(...) > (...)` на сравнение одного
    `starts_at`: строки с равным `starts_at` и меньшим `uuid` дублировались бы
    на следующей странице, а с большим — пропускались.
    """
    after = (_NOW, uuid_lib.UUID("00000000-0000-0000-0000-000000000001"))
    statement = EventCardRepository.build_feed_statement(
        community_slug="tomsk",
        now=_NOW,
        category=None,
        has_seats=False,
        after=after,
        limit=21,
    )
    sql = _sql(statement)

    assert "(event_cards.starts_at, event_cards.uuid) > " in sql


def test_feed_statement_without_cursor_has_no_keyset() -> None:
    """AC-08: первая страница не содержит keyset-условия.

    Краснеет, если подставлять фиктивный курсор по умолчанию: первая страница
    начиналась бы не с начала ленты.
    """
    statement = EventCardRepository.build_feed_statement(
        community_slug="tomsk", now=_NOW, category=None, has_seats=False, after=None, limit=21
    )

    assert "(event_cards.starts_at, event_cards.uuid) > " not in _sql(statement)


def test_feed_statement_has_seats_is_arithmetic() -> None:
    """AC-04: `has_seats` — `filled_count < capacity`, не `status = 'full'`.

    Краснеет от замены на `status <> 'full'`: карточка `closed` с местами
    прошла бы фильтр, а карточка `full` с рассинхроном — выпала бы.
    """
    statement = EventCardRepository.build_feed_statement(
        community_slug="tomsk", now=_NOW, category=None, has_seats=True, after=None, limit=21
    )
    sql = _sql(statement)

    assert "event_cards.filled_count < event_cards.capacity" in sql


def test_feed_statement_without_has_seats_has_no_arithmetic() -> None:
    """AC-04: без `has_seats` фильтра по местам нет.

    Краснеет, если условие добавить всегда: занятые карточки исчезли бы из
    ленты для всех, а не только для тех, кто включил фильтр.
    """
    statement = EventCardRepository.build_feed_statement(
        community_slug="tomsk", now=_NOW, category=None, has_seats=False, after=None, limit=21
    )

    assert "filled_count < event_cards.capacity" not in _sql(statement)


def test_feed_statement_applies_category() -> None:
    """AC-05: категория — параметр запроса, а не подстановка строки."""
    statement = EventCardRepository.build_feed_statement(
        community_slug="tomsk",
        now=_NOW,
        category="cinema",
        has_seats=False,
        after=None,
        limit=21,
    )
    sql = _sql(statement)

    assert "event_cards.category = " in sql
    # Значение ушло параметром: в тексте его нет (защита от подстановки).
    assert "'cinema'" not in sql


def test_feed_count_statement_has_no_cursor_and_has_filters() -> None:
    """AC-07: `total` считается по фильтрам, но БЕЗ курсора.

    Краснеет, если добавить курсор в `COUNT`: `total` уменьшался бы от
    страницы к странице, и клиент не смог бы показать «сколько всего».
    `has_seats` обязан быть и здесь — иначе `total` и `items` считались бы
    по разным предикатам.
    """
    statement = EventCardRepository.build_feed_count_statement(
        community_slug="tomsk", now=_NOW, category="cinema", has_seats=True
    )
    sql = _sql(statement)

    assert "count(event_cards.id)" in sql
    assert "(event_cards.starts_at, event_cards.uuid) > " not in sql
    assert "event_cards.filled_count < event_cards.capacity" in sql
    assert "event_cards.category = " in sql


def test_visible_statement_filters_by_uuid_and_community_but_not_author() -> None:
    """AC-02: карточка ищется по `uuid` в срезе сообщества, БЕЗ `author_id`.

    Краснеет в двух направлениях:
    - без `community_slug` участник получал бы карточку чужого города;
    - с `author_id` карточку не смог бы открыть никто, кроме автора, и лента
      вела бы в `404` на каждую чужую встречу.
    """
    card_uuid = uuid_lib.uuid4()
    statement = EventCardRepository.build_get_visible_statement(
        card_uuid=card_uuid, community_slug="tomsk"
    )
    sql = _sql(statement)

    assert "event_cards.uuid = " in sql
    assert "event_cards.community_slug = " in sql
    assert "event_cards.author_id = " not in sql


def test_close_due_statement_is_cas_with_status_and_expiry() -> None:
    """AC-03: CAS содержит `status='published'` И `expires_at <= now`.

    Краснеет от снятия любого из двух условий:
    - без `status` повторное закрытие проходило бы (не идемпотентно);
    - без `expires_at` окно закрывалось бы раньше срока, пока автор выбирает.
    """
    statement = EventCardRepository.build_close_due_statement(card_id=42, now=_NOW)
    sql = _sql(statement)

    assert sql.startswith("UPDATE event_cards")
    assert "event_cards.status = " in sql
    assert "event_cards.expires_at <= " in sql
    assert "closed_at=" in sql.replace(" ", "")


def test_card_read_inherits_ref_and_adds_only_new_fields() -> None:
    """AC-06, X-13, D-4.5-1: `EventCardRead` НАСЛЕДУЕТ `EventCardRef`.

    Краснеет, если схему сделать самостоятельной (копия четырёх полей):
    при первой правке одного из них два представления разойдутся, и
    `/cards` начнёт отдавать `status`, несовместимый с `/cards/{uuid}`.
    Проверяется и обратное: 4 поля НЕ переопределены (иначе тип мог бы
    сузиться до несовместимого).
    """
    assert issubclass(EventCardRead, EventCardRef)
    for field_name in ("uuid", "status", "verification_status", "created_at"):
        assert (
            EventCardRead.model_fields[field_name].annotation
            == EventCardRef.model_fields[field_name].annotation
        )


def test_card_read_exposes_contract_fields_without_internal_ids() -> None:
    """AC-06, D-2: форма §7.2 есть, внутренних bigint и причины отказа нет.

    Краснеет, если добавить `place_id`/`author_id`/`verified_by` (утечка
    внутренних адресов) или `rejection_reason` (сообщение автору, не ленте).
    """
    fields = set(EventCardRead.model_fields)
    expected = {
        "uuid",
        "status",
        "verification_status",
        "created_at",
        "title",
        "description",
        "category",
        "starts_at",
        "expires_at",
        # Стори 5б.1 (AC-04, NFR14): серверный якорь отсчёта. Не внутренний
        # адрес и не сообщение автору — поэтому в списке запрещённых его нет.
        "server_time",
        "capacity",
        "filled_count",
        "place",
        "author",
        "chat_uuid",
        # Стори 5б.2 (AC-09, D-5b.2-7): признак участия ЗРИТЕЛЯ. Публичное поле
        # контракта (FR104), а не внутренний адрес — поэтому оно здесь, а не
        # в списке запрещённых ниже.
        "viewer_participates",
    }

    assert fields == expected
    assert {"place_id", "author_id", "verified_by", "rejection_reason"}.isdisjoint(fields)


def test_card_read_author_fields_are_optional() -> None:
    """AC-06: `name`/`age` автора допускают `None` (анкеты может не быть).

    Краснеет, если объявить их обязательными: карточка автора без онбординга
    перестала бы отдаваться, а `LEFT JOIN` в запросе даёт `NULL`.
    """
    from app.schemas.event_card import EventCardAuthor

    assert EventCardAuthor.model_fields["name"].annotation == (str | None)
    assert EventCardAuthor.model_fields["age"].annotation == (int | None)


def test_chat_uuid_defaults_to_none() -> None:
    """AC-06, D-4.5-7: `chat_uuid` есть в контракте и по умолчанию `null`.

    Краснеет, если убрать поле (клиент переписывался бы дважды) или сделать
    его обязательным (до эпика 6 значение взять неоткуда).
    """
    assert EventCardRead.model_fields["chat_uuid"].default is None
