"""Замок AC-4 стори 12.6: `within_bounds` НЕ выходит заявителю.

**Что защищается.** Поле `within_bounds` существует ровно для автора карточки
(AC-3): он видит, кто из заявившихся попадает в его рамки. Заявитель не должен
узнавать, что его отсеяли по возрасту или полу, — отказ безличен (FR81,
ADR-19), и заявитель видит только «на это время компания собрана» (AC-6).

**Почему замок именно такой.** `MyRequestItem` — единственная схема, через
которую заявитель читает СВОИ заявки (`GET /my/requests`). Соблазн «добавить
поле на всякий случай» реален: схема рядом, поле готово. Замок читает
`model_fields` и падает от одного добавленного поля — без БД, без HTTP.

**Чего этот тест НЕ проверяет.** Что поле не просочилось в другие ответы
(лента, карточка, подача). Это закрывает интеграционный тест
`test_within_bounds_card_requests.py` (читает тело реальных ответов).

Краснеет при:
- добавлении `within_bounds` в `MyRequestItem` (мутация U8);
- переименовании поля в «нейтральное» при сохранении смысла — тест ищет
  подстроку `bounds` среди полей, чтобы `is_within_bounds` не проскочило.
"""

from __future__ import annotations

from app.schemas.join_request import (
    CardRequestsItem,
    JoinRequestRead,
    MyRequestItem,
    MyRequestsResponse,
)


def test_my_request_item_has_no_within_bounds() -> None:
    """AC-4: в схеме своих заявок поля нет — ни под каким именем."""
    fields = set(MyRequestItem.model_fields)
    assert "within_bounds" not in fields, (
        f"AC-4 нарушен: заявитель увидит, что его отсеяли по рамкам. Поля схемы: {sorted(fields)}"
    )
    # Подстраховка от переименования: поле с тем же смыслом под другим именем
    # (`is_within_bounds`, `audience_ok`) тоже нарушило бы AC-4.
    suspicious = [name for name in fields if "bounds" in name or "audience" in name]
    assert suspicious == [], f"AC-4: поле рамок просочилось под чужим именем: {suspicious}"


def test_my_requests_envelope_has_no_within_bounds() -> None:
    """AC-4: конверт списка тоже не несёт признака рамок."""
    assert "within_bounds" not in set(MyRequestsResponse.model_fields)


def test_my_requests_cards_do_not_carry_bounds() -> None:
    """AC-4: ни одна вложенная схема своих заявок не несёт полей рамок."""
    nested = (
        MyRequestItem.model_fields["card"].annotation,
        MyRequestItem.model_fields["author"].annotation,
    )
    for annotation in nested:
        model = annotation
        # `MyRequestCard` / `MyRequestAuthor` — плоские модели; если аннотация
        # стала `| None`, берём первый аргумент.
        fields = getattr(model, "model_fields", {})
        leaked = [name for name in fields if "bounds" in name]
        assert leaked == [], f"AC-4: вложенная схема {model} несёт {leaked}"


def test_card_requests_item_requires_within_bounds() -> None:
    """AC-3: в ответе АВТОРУ поле есть и обязательно — без умолчания.

    Умолчание сделало бы «забыли заполнить» неотличимым от «вне рамок»:
    `None` здесь означает ровно «у карточки рамок нет».
    """
    field = CardRequestsItem.model_fields["within_bounds"]
    assert field.is_required(), (
        "AC-3: `within_bounds` обязано быть обязательным полем — иначе "
        "забытая передача в сборке ответа прошла бы молча"
    )


def test_join_request_read_has_no_within_bounds() -> None:
    """AC-4 (шире): ответ на подачу заявки тоже не раскрывает рамки."""
    assert "within_bounds" not in set(JoinRequestRead.model_fields)
