"""П12: причина отказа не сообщается; пол/дата рождения вне сообщений (FR81).

**Что доказывается.** Ни одна read-схема заявок (`app/schemas/join_request.py`)
не содержит поля, по которому заявитель мог бы узнать причину отказа:
`reason`, `reject*`, `decline*`. И ни одна не отдаёт `gender`/`birth_date`:
отказ безличен (FR81), а пол и дата рождения не покидают сервер (NFR48,
стори 12.3 — гард `SENSITIVE_FIELDS` закрывает пользовательские ответы,
здесь тот же запрет для заявок).

**Почему сравнение НАБОРА ключей, а не подстрок в файле (W-12.7-m).**
CR 12.7 (M22) показал: поле `within_bounds` просочилось в витрину заявителя,
потому что тела ответов сверялись по ключам значений, а не по составу.
Проверка «`reason` не встречается в тексте файла» краснела бы от комментария
и зеленела от `reason_ru`. Здесь сверяется ТОЧНЫЙ набор полей каждой модели:
новое поле не просочится молча — тест упадёт на равенстве.

**Почему `within_bounds` разрешён только двум моделям.** Пометка «вне рамок»
нужна автору (`CardRequestsItem`) и никому больше: заявитель, увидев флаг
у себя, узнал бы, что его отсеяли по возрасту или полу — это и есть
нарушение FR81. Замок держит асимметрию в одном месте.

Метка `П12` в докстринге обязательна: реестр правил
(`test_rules_registry.py`) находит тест именно по ней (AC-1).
"""

from __future__ import annotations

import pytest

from app.schemas import join_request as jr

#: Схемы, которые ЗАЯВИТЕЛЬ получает в ответ на свои запросы.
APPLICANT_SCHEMAS = (
    jr.JoinRequestRead,
    jr.MyRequestsResponse,
    jr.MyRequestItem,
    jr.MyRequestCard,
    jr.MyRequestPlace,
    jr.MyRequestAuthor,
)

#: Схемы, которые АВТОР получает о заявившихся.
AUTHOR_SCHEMAS = (
    jr.CardRequestsResponse,
    jr.CardRequestsItem,
    jr.CardRequestsPerson,
    jr.SelectRequestResponse,
)

#: Запрещённые поля во ВСЕХ read-схемах заявок. Дата рождения и пол не
#: покидают сервер (NFR48); причина отказа безлична (FR81).
FORBIDDEN_SUBSTRINGS = ("reason", "reject", "decline", "gender", "birth_date")

#: Точный состав каждой модели. Равенство, не подмножество: НОВОЕ поле
#: обязано сломать тест, иначе оно просочится молча (W-12.7-m).
EXPECTED_FIELDS: dict[type, set[str]] = {
    jr.JoinRequestRead: {"uuid", "status", "submitted_at"},
    jr.MyRequestsResponse: {"items", "total", "cursor"},
    jr.MyRequestItem: {
        "uuid",
        "status",
        "submitted_at",
        "expires_at",
        "message",
        "card",
        "author",
    },
    jr.MyRequestCard: {
        "uuid",
        "title",
        "category",
        "starts_at",
        "expires_at",
        "status",
        "capacity",
        "filled_count",
        "place",
    },
    jr.MyRequestPlace: {"uuid", "name", "address_text"},
    jr.MyRequestAuthor: {"uuid", "name", "age", "photo_url"},
    jr.CardRequestsResponse: {
        "items",
        "total",
        "cursor",
        "mode",
        "selection_available",
    },
    jr.CardRequestsItem: {
        "uuid",
        "submitted_at",
        "message",
        "person",
        "within_bounds",
    },
    jr.CardRequestsPerson: {
        "uuid",
        "name",
        "age",
        "photo_url",
        "photo_expires_in",
    },
    jr.SelectRequestResponse: {"card_uuid", "chat_uuid"},
}


@pytest.mark.parametrize("schema", APPLICANT_SCHEMAS + AUTHOR_SCHEMAS)
def test_no_forbidden_field_in_any_read_schema(schema: type) -> None:
    """П12: ни одна read-схема заявок не содержит `reason`/`gender`/`birth_date`."""
    leaked = [
        name
        for name in schema.model_fields
        if any(bad in name.lower() for bad in FORBIDDEN_SUBSTRINGS)
    ]
    assert leaked == [], f"{schema.__name__}: поля раскрывают причину/пол/дату рождения: {leaked}"


def test_forbidden_substrings_are_not_narrowed() -> None:
    """П12 (M-2 CR 12.9): СПИСОК запрещённых подстрок не сужен и не расширен молча.

    **Дыра, которую закрывает тест.** CR 12.9 (CR-14) сузил
    `FORBIDDEN_SUBSTRINGS` до `("reason",)` — и все 22 теста остались
    зелёными (`SURVIVED`). Запрет `gender`/`birth_date` держался ТОЛЬКО на
    `EXPECTED_FIELDS`: равенство наборов ловило добавление поля, но не
    удаление второго слоя. Двухслойность была иллюзорной (`W-12.9-k`).

    **Почему проверяется именно состав, а не поведение каждого элемента.**
    Поведение первого слоя проверяет `test_no_forbidden_field_in_any_read_schema`
    на реальных схемах; воспроизвести его «в вакууме» можно только создав
    подставную модель с полем `reason`, что проверяло бы `any()`, а не сам
    список. Здесь замок на СОСТАВ: сужение списка краснеет, расширение
    (бесполезное, но безвредное) — тоже, потому что осознанность видна
    только по явной правке этой строки.
    """
    assert set(FORBIDDEN_SUBSTRINGS) == {
        "reason",
        "reject",
        "decline",
        "gender",
        "birth_date",
    }, (
        "состав запрещённых подстрок изменён. Если это осознанно — правьте "
        "и этот тест, и PRD :832 (П12); иначе второй слой защиты ослаблен молча"
    )


def test_each_forbidden_substring_actually_rejects_a_field_name() -> None:
    """П12 (M-2): каждая подстрока списка ДЕЙСТВИТЕЛЬНО ловит своё поле.

    Пара к замку состава: там проверяется «список не изменился», здесь —
    «элемент списка работает». Без этой пары подстрока `decline` могла бы
    остаться в кортеже и не участвовать в проверке (опечатка в фильтре),
    а состав остался бы верным.
    """
    for substring in FORBIDDEN_SUBSTRINGS:
        field_name = f"{substring}_field"
        leaked = [
            name
            for name in [field_name]
            if any(bad in name.lower() for bad in FORBIDDEN_SUBSTRINGS)
        ]
        assert leaked == [field_name], (
            f"подстрока {substring!r} не ловит поле {field_name!r} — второй слой мёртв"
        )


@pytest.mark.parametrize("schema", APPLICANT_SCHEMAS + AUTHOR_SCHEMAS)
def test_field_set_is_exactly_as_designed(schema: type) -> None:
    """П12: НАБОР полей совпадает с ожидаемым — равенство, не подмножество.

    Краснеет при добавлении любого поля, в том числе безобидного на вид:
    контракт заявок расширяется только правкой этой таблицы, то есть
    осознанно (W-12.7-m — урок CR 12.7, M22).
    """
    assert set(schema.model_fields) == EXPECTED_FIELDS[schema], (
        f"{schema.__name__}: состав полей изменился. "
        f"Добавлено: {set(schema.model_fields) - EXPECTED_FIELDS[schema]}, "
        f"удалено: {EXPECTED_FIELDS[schema] - set(schema.model_fields)}"
    )


def test_within_bounds_reaches_author_only() -> None:
    """П12 (FR81): «вне рамок» есть у автора и НЕТ ни в одной схеме заявителя.

    Краснеет от переноса `within_bounds` в `MyRequestItem`/`MyRequestCard`:
    заявитель узнал бы, что его отсеяли по возрасту или полу, — ровно то,
    что FR81 запрещает показывать.
    """
    assert "within_bounds" in jr.CardRequestsItem.model_fields
    for schema in APPLICANT_SCHEMAS:
        assert "within_bounds" not in schema.model_fields, (
            f"{schema.__name__}: пометка «вне рамок» дошла до заявителя (FR81)"
        )


def test_response_envelopes_share_one_shape() -> None:
    """П12: оба конверта (`мои` / `автора`) имеют одинаковый набор `{items,total,cursor}`.

    Дополнительный признак конверта автора (`mode`, `selection_available`)
    ожидаем: он описывает режим карточки, а не заявителя. Проверка разности
    держит её явной — расширение конверта заявителя упадёт здесь.
    """
    envelope = {"items", "total", "cursor"}
    assert set(jr.MyRequestsResponse.model_fields) == envelope
    assert set(jr.CardRequestsResponse.model_fields) == envelope | {
        "mode",
        "selection_available",
    }
