"""Пагинация и порядок списка обращений (стори 9.5; AC-02; Task 7.4).

**Порядок доказывается СОВПАДЕНИЕМ `created_at`** (§18.11). `now()` из
`server_default` даёт разное время, и тогда `ORDER BY created_at DESC`
неотличим от `ORDER BY id DESC` — мутация M2 (`ASC`) краснела бы, а подмена
ключа сортировки нет. Поэтому `created_at` задаётся данными: две строки с
ОДИНАКОВЫМ временем, и порядок между ними определяет второй ключ `id`.

**Строки создаются прямым `INSERT`**, а не через API: `created_at` — это
`server_default`, и API не даёт его назначить. Тест на API-созданных строках
проверял бы `now()`, а не сортировку.
"""

from __future__ import annotations

import os
import uuid as uuid_lib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.support import get_support_ticket_rate_limiter
from app.main import app
from app.services.reports_queue_cursor import encode_queue_cursor
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


class _AlwaysAllowLimiter:
    """Лимит не является предметом этих тестов — окно не должно мешать пагинации.

    Но подменяется ЯВНО, а не через реальный Redis: тест пагинации не должен
    падать от `429`, если в общем Redis остались ключи прошлого прогона
    (изоляция Redis — session-scoped, X-6.7-2).
    """

    async def hit(self, user_id: int) -> int | None:
        return None


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with build_client(session) as http_client:
        yield http_client


@pytest.fixture(autouse=True)
def limiter() -> AsyncIterator[None]:
    fake = _AlwaysAllowLimiter()
    app.dependency_overrides[get_support_ticket_rate_limiter] = lambda: fake
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_support_ticket_rate_limiter, None)


async def _seed(
    session: AsyncSession,
    *,
    user_id: int,
    body: str,
    created_at: datetime,
    status: str = "open",
) -> str:
    """Прямой `INSERT` с ЯВНЫМ `created_at` — иначе tie-группы не создать.

    `created_at` — объект `datetime`, не строка: asyncpg отвергает `str` для
    `timestamptz` (`DataError`), и тест падал бы на собственном сиде, ничего
    не сказав о сортировке.
    """
    result = await session.execute(
        text(
            "INSERT INTO support_tickets (user_id, body, status, created_at) "
            "VALUES (:user_id, :body, :status, :created_at) "
            "RETURNING uuid"
        ),
        {"user_id": user_id, "body": body, "created_at": created_at, "status": status},
    )
    return str(result.scalar_one())


async def _page(client: AsyncClient, token: str, **params: Any) -> dict[str, Any]:
    response = await client.get(_SUPPORT_URL, headers=cookie_headers(token), params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_list_order_newest_first(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, M2: `created_at DESC, id DESC` — новые сверху.

    Мутация M2 (`ORDER BY created_at ASC`) роняет порядок тел: старейшее
    оказалось бы первым, и пользователь видел бы свою проблему внизу списка
    (а на больших объёмах — вообще не на первой странице).

    Две строки имеют ОДИНАКОВЫЙ `created_at`: между ними порядок задаёт `id`.
    Без этого вторая половина контракта (`id DESC`) не проверялась бы, и
    keyset мог бы терять строки tie-группы.
    """
    user, token = await make_user(session)
    await _seed(
        session, user_id=user.id, body="старое", created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    )
    await _seed(
        session,
        user_id=user.id,
        body="среднее-А",
        created_at=datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
    )
    await _seed(
        session,
        user_id=user.id,
        body="среднее-Б",
        created_at=datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
    )
    await _seed(
        session, user_id=user.id, body="новое", created_at=datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    )

    payload = await _page(client, token)

    bodies = [item["body"] for item in payload["items"]]
    assert bodies[0] == "новое", bodies
    assert bodies[-1] == "старое", bodies
    # Tie-группа: вставка «среднее-Б» была позже, значит её `id` больше и она
    # обязана идти ПЕРВОЙ при `id DESC`.
    assert bodies.index("среднее-Б") < bodies.index("среднее-А"), bodies


async def test_pagination_second_page(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, M7: `limit=2` → курсор, вторая страница без дублей и пропусков.

    Мутация M7 (`next_cursor = None` всегда) роняет первую половину: клиент
    не смог бы догрузить третье обращение — оно существует, но недостижимо.
    Вторая половина (состав второй страницы) ловит курсор, который «есть»,
    но не двигает чтение: без `WHERE (created_at, id) <` обе страницы
    вернули бы одно и то же.

    `created_at` у всех трёх строк РАЗНЫЙ: страница проверяет сам курсор,
    а tie-группа — отдельный тест выше (иначе два дефекта склеились бы и
    падение не сказало бы, какой именно).
    """
    user, token = await make_user(session)
    await _seed(
        session, user_id=user.id, body="первое", created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    )
    await _seed(
        session, user_id=user.id, body="второе", created_at=datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    )
    await _seed(
        session, user_id=user.id, body="третье", created_at=datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    )

    first = await _page(client, token, limit=2)

    assert [item["body"] for item in first["items"]] == ["третье", "второе"]
    assert first["total"] == 3, "total — сколько ВСЕГО, не сколько на странице"
    assert first["next_cursor"], "есть третья строка — курсор обязан быть"

    second = await _page(client, token, limit=2, cursor=first["next_cursor"])

    assert [item["body"] for item in second["items"]] == ["первое"]
    assert second["total"] == 3
    # Последняя страница — курсора нет (не пустая строка и не `"null"`).
    assert second["next_cursor"] is None


async def test_last_page_has_no_cursor(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, M7 (§18.11, вторая половина): курсор бывает и `null`, и строкой.

    Проверка только «курсор есть» прошла бы при реализации «курсор всегда», и
    клиент бесконечно запрашивал бы пустые страницы: `next_cursor` непустой →
    ещё запрос → снова непустой. Тест требует `null` на умещающейся странице.
    """
    user, token = await make_user(session)
    await _seed(
        session,
        user_id=user.id,
        body="единственное",
        created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    )

    payload = await _page(client, token, limit=20)

    assert len(payload["items"]) == 1
    assert payload["next_cursor"] is None


@pytest.mark.parametrize(
    "cursor",
    ["не-base64!", "eyJmb28iOiJiYXIifQ", ""],
    ids=["garbage", "wrong-keys", "empty"],
)
async def test_broken_cursor_returns_422(
    client: AsyncClient, session: AsyncSession, cursor: str
) -> None:
    """AC-02: битый курсор — `422 invalid_cursor`, не `500`.

    Курсор приходит от клиента, поэтому любая ошибка разбора — ошибка ввода.
    `500` обвинил бы сервер в значении, которое сервер не формировал, и клиент
    не узнал бы, что надо сбросить курсор. Пустая строка — тот же класс:
    явный мусор вместо отсутствия параметра.
    """
    _user, token = await make_user(session)

    response = await client.get(
        _SUPPORT_URL, headers=cookie_headers(token), params={"cursor": cursor}
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_cursor"


async def test_limit_out_of_range_rejected(client: AsyncClient, session: AsyncSession) -> None:
    """AC-02, D-9.5-8: `limit` вне 1…50 — `422` от валидации запроса.

    Без `le=50` один рендер вытянул бы весь список (`SELECT *` на каждый
    экран); без `ge=1` клиент мог бы запросить `limit=0` и получить пустую
    страницу с непустым `total` — необъяснимое состояние для UI.
    """
    _user, token = await make_user(session)

    too_big = await client.get(_SUPPORT_URL, headers=cookie_headers(token), params={"limit": 51})
    zero = await client.get(_SUPPORT_URL, headers=cookie_headers(token), params={"limit": 0})

    assert too_big.status_code == 422, too_big.text
    assert zero.status_code == 422, zero.text


async def test_no_store_header_on_both_routes(client: AsyncClient, session: AsyncSession) -> None:
    """AC-01, AC-02: `cache-control: no-store` на `201` И на `200`.

    Тело обращения — ПД, и прокси не должен кешировать ни создание, ни список.
    Проверка `201` отдельно намеренна: успешный ответ на запись легче всего
    забыть, а именно он содержит текст обращения.
    """
    user, token = await make_user(session)

    created = await client.post(
        _SUPPORT_URL, headers=cookie_headers(token), json={"body": "тело обращения"}
    )
    listed = await client.get(_SUPPORT_URL, headers=cookie_headers(token))

    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "no-store"
    assert user.id > 0


# ——— CR 9.5 MEDIUM-1: курсор не оракул существования чужих обращений ———


async def test_forged_foreign_uuid_does_not_open_tie_group(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 9.5 MEDIUM-1, M9: подделанный ЧУЖОЙ `uuid` не открывает tie-группу.

    **Порядок сида КРИТИЧЕН.** Своя строка создаётся ПЕРВОЙ (её `id` меньше),
    чужая — ВТОРОЙ (`id` больше). Тогда без `AND user_id` в подзапросе курсор
    возвращает `id` жертвы, и row-comparison
    `(tie, свой_id) < (tie, чужой_id)` даёт **TRUE** — своя строка попадает в
    выдачу, которая должна быть пустой. С несуществующим `uuid` подзапрос даёт
    `NULL`, сравнение — `NULL`, строк нет.

    Разница между этими двумя ответами и есть оракул существования чужого
    обращения по его `uuid`. Обратный порядок сида (жертва первой) сделал бы
    тест слепым: оба случая дали бы пусто и мутация M9 выжила бы — проверено
    прогоном.
    """
    viewer, viewer_token = await make_user(session)
    victim, _victim_token = await make_user(session)

    tie_at = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    own_uuid = await _seed(session, user_id=viewer.id, body="своё", created_at=tie_at)
    # Жертва ВТОРОЙ — её `id` больше своего, только так утечка возможна.
    foreign_uuid = await _seed(session, user_id=victim.id, body="чьё-то", created_at=tie_at)

    forged = encode_queue_cursor(tie_at, uuid_lib.UUID(foreign_uuid))
    nonexistent = encode_queue_cursor(tie_at, uuid_lib.uuid4())

    forged_page = await _page(client, viewer_token, cursor=forged)
    none_page = await _page(client, viewer_token, cursor=nonexistent)

    assert forged_page == none_page, (forged_page, none_page)
    assert forged_page["items"] == [], forged_page
    assert forged_page["total"] == 1

    # СВОЙ курсор обязан продолжать работать: фикс не должен ломать листание.
    # Своя строка единственная, поэтому следующая страница пуста, но курсор
    # принимается без ошибки (не `422`, не `500`).
    own_cursor = encode_queue_cursor(tie_at, uuid_lib.UUID(own_uuid))
    own_page = await _page(client, viewer_token, cursor=own_cursor)
    assert own_page["items"] == [], own_page


async def test_forged_uuid_cursor_does_not_leak_foreign_existence(
    client: AsyncClient, session: AsyncSession
) -> None:
    """CR 9.5 MEDIUM-1 (вторая половина): ответ не зависит от чужой строки.

    У пользователя есть СТАРЫЕ строки (они законно проходят курсор по первому
    ключу `created_at`), а в дате курсора — tie-группа. Сравниваются два
    ответа: с `uuid` СУЩЕСТВУЮЩЕГО чужого обращения и с несуществующим.

    Любое расхождение означало бы информационный канал: перебор `uuid`
    сообщал бы, существует ли чужое обращение и каков его порядок по `id`.
    Одинаковый ответ доказывает, что курсор несёт только позицию, а не факт
    существования чужих строк.
    """
    victim, _victim_token = await make_user(session)
    viewer, viewer_token = await make_user(session)

    await _seed(
        session,
        user_id=viewer.id,
        body="своё-старое-1",
        created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    )
    await _seed(
        session,
        user_id=viewer.id,
        body="своё-старое-2",
        created_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
    )
    tie_at = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    foreign_uuid = await _seed(session, user_id=victim.id, body="чужое", created_at=tie_at)

    forged = encode_queue_cursor(tie_at, uuid_lib.UUID(foreign_uuid))
    nonexistent = encode_queue_cursor(tie_at, uuid_lib.uuid4())

    forged_page = await _page(client, viewer_token, cursor=forged)
    none_page = await _page(client, viewer_token, cursor=nonexistent)

    assert forged_page == none_page, (forged_page, none_page)
    # Старые СВОИ строки законно попадают в обе выдачи: курсор — позиция,
    # а не граница «показать всё». Чужих тел в ответе нет.
    assert [item["body"] for item in forged_page["items"]] == ["своё-старое-2", "своё-старое-1"]
    assert forged_page["total"] == 2
