"""API обращений в поддержку (стори 9.5; AC-01…AC-04; Task 7.1–7.3, 7.5).

**Проверки идут через РЕАЛЬНЫЙ роутер и РЕАЛЬНУЮ БД** (урок CR 9.4: моки
доказывают попытку, а не результат). Ни один тест здесь не подменяет сервис
или репозиторий: `201` проверяется строкой в таблице, «только свои» — составом
ответа при ДВУХ пользователях с непустыми наборами, AC-03 — чтением строки,
созданной прямым `INSERT` (переходов статуса в 9.5 нет, У-4).

**Лимитер подменяется зависимостью, но арифметика в нём настоящая**
(§18.5, M8). Фейк не «всегда пропускает» и не «всегда 429»: он считает окно
по-настоящему, поэтому шестой запрос получает `429` именно потому, что первые
пять прошли. Замена лимитера на заглушку-константу сделала бы M8
непроверяемой.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.support import get_support_ticket_rate_limiter
from app.main import app
from app.repositories.support_tickets import SupportTicketsRepository
from app.services.support_ticket_service import TicketBodyRequiredError, create_ticket
from tests.integration.attendance_history_helpers import (
    build_client,
    cookie_headers,
    make_user,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"),
]

_SUPPORT_URL = "/api/v1/support/tickets"


class _FixedWindowLimiter:
    """M8: фейк с НАСТОЯЩЕЙ арифметикой окна, без Redis (§18.5).

    Копия правила `SupportTicketRateLimiter` (fixed-window, ключ — аккаунт)
    нужна намеренно: «всегда None» сделало бы мутацию M8 («убрать проверку
    лимитера в роутере») невидимой — тест остался бы зелёным при любом
    коде роутера. Здесь `hit` возвращает `retry_after` ровно на шестом
    запросе, и убрать проверку в роутере значит получить `201` вместо `429`.
    """

    def __init__(self, *, limit: int = 5, window_seconds: int = 3600) -> None:
        self._limit = limit
        self._window = window_seconds
        self._counts: dict[int, int] = {}

    async def hit(self, user_id: int) -> int | None:
        self._counts[user_id] = self._counts.get(user_id, 0) + 1
        if self._counts[user_id] <= self._limit:
            return None
        return self._window


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with build_client(session) as http_client:
        yield http_client


@pytest.fixture
def limiter() -> AsyncIterator[_FixedWindowLimiter]:
    """Подменяет зависимость лимитера на фейк с настоящей арифметикой.

    `dependency_overrides`, а не `monkeypatch` модуля: фабрика вызывается
    FastAPI на каждом запросе, и подмена модульной константы не дала бы
    изоляции между тестами (соседний тест увидел бы чужой счётчик).
    """
    fake = _FixedWindowLimiter()
    app.dependency_overrides[get_support_ticket_rate_limiter] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_support_ticket_rate_limiter, None)


async def _create(client: AsyncClient, token: str, body: str) -> dict[str, Any]:
    """`POST` одного обращения; падение на неожидаемом коде — сразу с телом."""
    response = await client.post(_SUPPORT_URL, headers=cookie_headers(token), json={"body": body})
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_ticket(
    session: AsyncSession,
    *,
    user_id: int,
    body: str,
    status: str = "open",
    answer: str | None = None,
    answered_at: datetime | None = None,
    created_at: datetime | None = None,
) -> str:
    """Прямой `INSERT` мимо API: единственный способ создать `answered`/`closed`.

    AC-02/AC-03 доказываются ЧТЕНИЕМ строк, которых API создать не может:
    переходов статуса в 9.5 нет (У-4), `answered` пишет 9.6. Если бы тест
    собирал такие строки через сервис, он проверял бы собственный сид, а не
    чтение.
    """
    result = await session.execute(
        text(
            "INSERT INTO support_tickets (user_id, body, status, answer, answered_at, created_at) "
            "VALUES (:user_id, :body, :status, :answer, "
            ":answered_at, COALESCE(:created_at, now())) "
            "RETURNING uuid"
        ),
        {
            "user_id": user_id,
            "body": body,
            "status": status,
            "answer": answer,
            "answered_at": answered_at,
            "created_at": created_at,
        },
    )
    return str(result.scalar_one())


# ——— AC-01: создание ———


async def test_create_returns_open_status(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter
) -> None:
    """AC-01, M3: `POST` → `201`, статус `open`, `uuid` в ответе и в БД.

    Мутация M3 («сервис не ставит `status='open'`») роняет первую проверку:
    `status` в ответе стал бы пустым/`NULL`, а не «open по умолчанию» —
    `server_default` подставил бы значение в БД, но не в `RETURNING`, если
    колонку убрать из `INSERT`.
    """
    user, token = await make_user(session)
    payload = await _create(client, token, "Не приходит уведомление о заявке")

    assert payload["status"] == "open"
    assert payload["answer"] is None
    assert payload["answered_at"] is None
    assert payload["body"] == "Не приходит уведомление о заявке"
    assert payload["uuid"]

    # D-9.5-6: внутренние поля наружу не выходят.
    assert "id" not in payload
    assert "user_id" not in payload
    assert "answered_by_id" not in payload

    # AC-01: строка реально в таблице, у ЭТОГО пользователя, в статусе `open`.
    row = (
        await session.execute(
            text("SELECT status, user_id FROM support_tickets WHERE uuid = :u"),
            {"u": payload["uuid"]},
        )
    ).one()
    assert row.status == "open"
    assert row.user_id == user.id

    # M3 (усиление после D-98): `'open'` стоит В ТЕКСТЕ `INSERT`, а не
    # наследуется от `server_default` текстом модели. Иначе мутация «убрать
    # явный статус» выживает: `DEFAULT` даёт то же значение. Проверка нужна
    # потому, что `server_default` — свойство схемы (её меняет миграция), а
    # AC-01 требует, чтобы `open` ставил КОД создания.
    assert "'open'" in SupportTicketsRepository._CREATE_SQL


async def test_create_returns_full_read_model(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter
) -> None:
    """AC-01, D-9.5-5: `201` отдаёт ПОЛНЫЙ `SupportTicketRead`, не `{uuid,status}`.

    Клиент вставляет строку из `201` в уже отрисованный список без второго
    запроса. Тест краснеет, если `201` вернут минимальным набором: списку
    нужны `body` и `created_at`, и без них элемент пришлось бы дочитывать.
    """
    _user, token = await make_user(session)
    payload = await _create(client, token, "Шаги: 1) открыть профиль 2) нажать сохранить")

    assert set(payload) == {"uuid", "body", "status", "created_at", "answer", "answered_at"}


# ——— AC-04: пустое тело ———


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "\t\n",
        # NBSP-строка: `btrim` в PostgreSQL её НЕ снимает (см. докстринг
        # `BODY_NOT_BLANK_SQL`), поэтому единственный рубеж — нормализация
        # в схеме. Мутация M4 (`min_length=0`) краснеет на всех четырёх.
        "\u00a0\u200b",
    ],
    ids=["empty", "spaces", "tabs-newlines", "nbsp-zwsp"],
)
async def test_empty_body_rejected(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter, body: str
) -> None:
    """AC-04, M4: пустое/пробельное тело → `422` от СХЕМЫ, не `500` от БД.

    Мутация M4 (`min_length=0`) роняет все четыре варианта: строка доходит
    до сервиса, где нормализация даёт пустоту, и вместо `422` приходит
    `body_required` — другой код, который клиент покажет как «заполните поле»
    только если он есть в контракте.
    """
    user, token = await make_user(session)

    response = await client.post(_SUPPORT_URL, headers=cookie_headers(token), json={"body": body})

    assert response.status_code == 422, response.text
    # M4 (усиление после D-98): отказ обязан прийти из СХЕМЫ (`invalid_request`
    # от общего обработчика валидации), а не из сервиса (`body_required`).
    # Без этой проверки мутация `min_length=0` выживала: сервис ловит пустое
    # тело второй линией и отдаёт тот же `422`, поэтому код ответа не
    # различал, ГДЕ сработал рубеж, а AC-04 требует именно схему.
    assert response.json()["code"] == "invalid_request", response.json()
    # AC-04: ничего не записалось — «пустое» не должно оставлять строку.
    count = (
        await session.execute(
            text("SELECT count(*) FROM support_tickets WHERE user_id = :u"), {"u": user.id}
        )
    ).scalar_one()
    assert count == 0


async def test_whitespace_only_body_rejected(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter
) -> None:
    """AC-04, M5: нормализация ДО проверки длины — иначе `'   '` пройдёт.

    Мутация M5 (убрать `mode="before"`) роняет этот тест: `min_length=1`
    видит в `'   '` три символа и пропускает значение, после чего отказ
    приходит из сервиса/БД, а не из схемы. Отдельный тест (не только
    параметризация выше) — потому что мутация адресует именно ПОРЯДОК.
    """
    _user, token = await make_user(session)

    response = await client.post(
        _SUPPORT_URL, headers=cookie_headers(token), json={"body": "  \n\t  "}
    )

    assert response.status_code == 422, response.text
    # M5 (усиление после D-98): отказ обязан прийти из СХЕМЫ (`invalid_request`
    # от общего обработчика валидации), а не из сервиса (`body_required`).
    # Без этой проверки мутация «убрать `mode=\"before\"`» выживала: код ответа
    # тот же 422, и тест не различал, ГДЕ сработал рубеж.
    assert response.json()["code"] == "invalid_request", response.json()


async def test_body_boundary_accepted_and_rejected(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter
) -> None:
    """AC-01, AC-04, D-9.5-4: граница 2000 принимается, 2001 — `422`.

    Граница проверяется ОБЕИМИ половинами (§18.11): без `2000 → 201` тест
    прошёл бы при `max_length=100`, а форма показывала бы счётчик `0 / 2000`
    и не давала бы отправить длинное письмо.
    """
    _user, token = await make_user(session)

    accepted = await client.post(
        _SUPPORT_URL, headers=cookie_headers(token), json={"body": "а" * 2000}
    )
    assert accepted.status_code == 201, accepted.text

    rejected = await client.post(
        _SUPPORT_URL, headers=cookie_headers(token), json={"body": "б" * 2001}
    )
    assert rejected.status_code == 422, rejected.text


async def test_service_rejects_empty_body_without_schema(
    session: AsyncSession,
) -> None:
    """AC-04, Task 3.5: вызов МИМО схемы тоже не создаёт пустое обращение.

    Схема — рубеж HTTP-транспорта, но сервис вызывается и напрямую (тесты,
    будущий потребитель 9.6). Без собственной нормализации `'   '` дошла бы
    до `INSERT` и упала `DBAPIError`-ом (`500`) вместо доменной ошибки.
    """
    user, _token = await make_user(session)

    with pytest.raises(TicketBodyRequiredError):
        await create_ticket(session, user_id=user.id, body="   ")


# ——— AC-02: только свои ———


async def test_list_returns_only_own(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter
) -> None:
    """AC-02, M1: у ДВУХ пользователей непустые наборы; чужое не приходит.

    Мутация M1 (убрать `WHERE user_id` в `list_by_user`) роняет этот тест:
    ответ содержал бы четыре обращения и `total=4`. Пустой набор у второго
    пользователя сделал бы тест зелёным при полностью удалённом предикате —
    именно поэтому у каждого свои строки.
    """
    owner, owner_token = await make_user(session)
    stranger, stranger_token = await make_user(session)

    for body in ("моё первое", "моё второе"):
        await _create(client, owner_token, body)
    for body in ("чужое первое", "чужое второе"):
        await _create(client, stranger_token, body)

    response = await client.get(_SUPPORT_URL, headers=cookie_headers(owner_token))
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["total"] == 2
    assert sorted(item["body"] for item in payload["items"]) == ["моё второе", "моё первое"]
    # Ни одно тело второго пользователя не попало в выдачу.
    assert not any("чужое" in item["body"] for item in payload["items"])

    # Второй пользователь видит ровно своё — предикат не «залип» на первом id.
    second = (await client.get(_SUPPORT_URL, headers=cookie_headers(stranger_token))).json()
    assert second["total"] == 2
    assert all("чужое" in item["body"] for item in second["items"])
    assert stranger.id != owner.id


async def test_list_query_contains_user_filter() -> None:
    """AC-02, M1 (вторая половина): предикат `user_id` есть в ТЕКСТЕ запроса.

    Составной тест на «удобных» данных пропустил бы вырожденный случай:
    если `list_by_user` фильтрует по `user_id`, но `count_by_user` — нет
    (или наоборот), состав ответа ещё может сойтись, а `total` соврёт.
    Проверка текста ловит удаление предиката НАПРЯМУЮ, до прогона данных.
    """
    list_sql = SupportTicketsRepository._LIST_SQL.format(keyset="")
    count_sql = SupportTicketsRepository._COUNT_SQL

    assert "user_id = :user_id" in list_sql, list_sql
    assert "user_id = :user_id" in count_sql, count_sql
    # D-9.5-11: порядок фиксирован и не приходит параметром.
    assert "ORDER BY created_at DESC, id DESC" in list_sql, list_sql


async def test_list_requires_authentication(client: AsyncClient) -> None:
    """AC-02, D-9.5-3: гард `CurrentUser` — без сессии `401`.

    Без гарда список вернул бы `500` (нет `user.id`) или пустой `200`,
    и неавторизованный клиент считал бы, что обращений нет.
    """
    response = await client.get(_SUPPORT_URL)

    assert response.status_code == 401, response.text


async def test_banned_user_gets_user_blocked(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, D-9.5-3, У-5: забаненный получает `403 user_blocked`.

    Следствие выбранного гарда (`CurrentUser`), а не дефект: публичный путь
    «написать» остаётся `/privacy#contact` (D-9.5-14). Тест фиксирует
    обещанное поведение, чтобы смена гарда на `ConsentUser` не прошла молча.
    """
    user, token = await make_user(session)
    # ORM-объект, а не `UPDATE`: `get_current_user` читает СТАТУС через
    # `UserRepository.get_by_uuid`, и правка мимо identity map оставила бы в
    # сессии старый `active` — тест проверял бы кеш, а не гард.
    user.status = "banned"
    await session.flush()

    response = await client.post(
        _SUPPORT_URL, headers=cookie_headers(token), json={"body": "помогите"}
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "user_blocked"


# ——— AC-03: ответ и дата видны ———


async def test_answered_ticket_shows_answer(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter
) -> None:
    """AC-03, M6: `answered` отдаёт `answer` и `answered_at` через реальный роутер.

    Строка создаётся ПРЯМЫМ `INSERT` (мимо API): переходов статуса в 9.5 нет,
    `answered` пишет 9.6. Мутация M6 (не отдавать `answer`/`answered_at`)
    роняет тест: пользователь увидел бы «Отвечено» без текста ответа —
    AC-03 невыполнен.
    """
    user, token = await make_user(session)
    await _insert_ticket(
        session,
        user_id=user.id,
        body="Не приходит код подтверждения",
        status="answered",
        answer="Проверьте, что номер не в стоп-листе оператора",
        answered_at=datetime(2026, 10, 25, 10, 0, tzinfo=UTC),
        created_at=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )

    payload = (await client.get(_SUPPORT_URL, headers=cookie_headers(token))).json()

    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["status"] == "answered"
    assert item["answer"] == "Проверьте, что номер не в стоп-листе оператора"
    assert item["answered_at"] is not None
    # §18.11: пара полей проверяется ЦЕЛИКОМ — дата без ответа была бы
    # дефектом бикондиционала `ck_support_tickets_answer_pair`.
    assert item["created_at"] is not None


async def test_open_ticket_has_null_answer_fields(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter
) -> None:
    """AC-03, §18.11: у `open` ОБА поля `null` — вторая половина пары.

    Тест краснеет, если `answer`/`answered_at` заполнят заглушкой (`""`,
    `created_at`): UI показал бы «отвечено» с пустым текстом на открытом
    обращении, и человек ждал бы ответа, которого нет.
    """
    _user, token = await make_user(session)
    await _create(client, token, "Ещё открытое обращение")

    item = (await client.get(_SUPPORT_URL, headers=cookie_headers(token))).json()["items"][0]

    assert item["status"] == "open"
    assert item["answer"] is None
    assert item["answered_at"] is None


# ——— AC-01: технический лимит ———


async def test_rate_limit_sixth_request_429(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter
) -> None:
    """AC-01, M8: 5 обращений проходят, шестое — `429` + `Retry-After`.

    Мутация M8 (убрать проверку лимитера в роутере) роняет тест: шестой
    запрос вернул бы `201`, то есть анти-абьюз не работал бы вовсе.
    Счётчик проверяется по коду ответа, а не по вызову фейка: вызов можно
    сделать и не вернуть `429` (дефект «проверили и забыли вернуть»).
    """
    _user, token = await make_user(session)

    for index in range(5):
        response = await client.post(
            _SUPPORT_URL, headers=cookie_headers(token), json={"body": f"обращение {index}"}
        )
        assert response.status_code == 201, response.text

    sixth = await client.post(_SUPPORT_URL, headers=cookie_headers(token), json={"body": "шестое"})

    assert sixth.status_code == 429, sixth.text
    assert sixth.json()["code"] == "rate_limited"
    assert int(sixth.headers["Retry-After"]) > 0
    # Пятое обращение записано, шестое — нет: лимит стоит ДО записи.
    count = (await session.execute(text("SELECT count(*) FROM support_tickets"))).scalar_one()
    assert count == 5


async def test_rate_limit_per_account_not_shared(
    client: AsyncClient, session: AsyncSession, limiter: _FixedWindowLimiter
) -> None:
    """AC-01, D-9.5-9: лимит считается ПО АККАУНТУ, не глобально.

    Второй пользователь после пяти обращений первого обязан получить `201`:
    глобальный счётчик (или ключ по IP) закрыл бы поддержку всем участникам
    с одного адреса — дефект, ради которого D-9.5-9 отверг IP как ключ.
    """
    _first, first_token = await make_user(session)
    _second, second_token = await make_user(session)

    for index in range(5):
        await client.post(
            _SUPPORT_URL, headers=cookie_headers(first_token), json={"body": f"первый {index}"}
        )

    response = await client.post(
        _SUPPORT_URL, headers=cookie_headers(second_token), json={"body": "второй"}
    )

    assert response.status_code == 201, response.text
