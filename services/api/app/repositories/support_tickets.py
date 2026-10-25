"""Запросы к `support_tickets` (стори 9.5; AC-01…AC-03).

project-context §6: здесь только SQL — ни правил домена, ни HTTP-кодов.
Единица работы — у сервиса, `commit` делает роут (§9).

**Почему `create` — сырой `INSERT ... RETURNING`.** Путь записи обращения не
несёт ORM-логики (нет связей, нет `onupdate`), а `RETURNING` отдаёт ровно то,
что нужно ответу `201`, одним запросом (P-8): иначе после вставки понадобился
бы `SELECT` за `uuid`/`created_at`, и между ними открылось бы окно.

**Почему `status='open'` литералом, а не параметром.** AC-01 и У-4:
создание всегда `open`; `answered` пишет 9.6, `closed` не реализуется нигде.
Параметр дал бы второй источник правды и позволил бы создать сразу
`answered` в обход бикондиционала `ck_support_tickets_answer_pair`.

**Почему `WHERE user_id = :user_id` в ОБОИХ запросах чтения.** AC-02 —
«только свои». Копия предиката в двух местах (`list` и `count`) намеренно НЕ
вынесена в строковую константу: общая константа склеила бы два разных
запроса, а расхождение здесь видно тестом «два пользователя» (M1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SupportTicketsRepository"]


class SupportTicketsRepository:
    """AC-01…AC-03: запись обращения и чтение своих обращений."""

    #: AC-01, D-9.5-5: `RETURNING` отдаёт полный набор полей ответа. `id`,
    #: `user_id` и `answered_by_id` НЕ возвращаются наружу (D-9.5-6) — их нет
    #: и в `SELECT`-списке ниже: колонка, которую никто не читает, всё равно
    #: попала бы в `dict` и рано или поздно в ответ.
    _CREATE_SQL = """
    INSERT INTO support_tickets (user_id, body, status)
    VALUES (:user_id, :body, 'open')
    RETURNING uuid, body, status, created_at, answer, answered_at
    """

    #: AC-02, AC-08: keyset по `(created_at, id) DESC`, лимит `:limit + 1`
    #: строк — лишняя строка и есть признак «есть следующая страница»
    #: (приём `moderation_reports_service`). `created_at` может совпасть у
    #: двух обращений, поэтому второй ключ `id` обязателен: без него keyset
    #: терял бы строки из tie-группы между страницами.
    #:
    #: `{keyset}` подставляется кодом, а не приходит параметром: текст SQL
    #: обязан быть статическим для плана, а значения — только bind-параметры.
    _LIST_SQL = """
    SELECT uuid, body, status, created_at, answer, answered_at
      FROM support_tickets
     WHERE user_id = :user_id
    {keyset}
     ORDER BY created_at DESC, id DESC
     LIMIT :limit
    """

    #: AC-02: тот же предикат, что у страницы, иначе `total` разошёлся бы
    #: с выдачей (показывал бы «всего 5», отдавая 2).
    _COUNT_SQL = """
    SELECT count(*) FROM support_tickets WHERE user_id = :user_id
    """

    @classmethod
    async def create(cls, session: AsyncSession, *, user_id: int, body: str) -> dict[str, Any]:
        """AC-01: создаёт обращение в статусе `open`; без `commit`.

        `flush` не нужен: `RETURNING` даёт значения сразу, а транзакцию
        закрывает роут (§9).
        """
        result = await session.execute(text(cls._CREATE_SQL), {"user_id": user_id, "body": body})
        return dict(result.mappings().one())

    @classmethod
    async def list_by_user(
        cls,
        session: AsyncSession,
        *,
        user_id: int,
        limit: int,
        cursor: tuple[datetime, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """AC-02, AC-08: страница своих обращений, новые сверху.

        Курсор — пара `(created_at, uuid)` из кодеков `reports_queue_cursor`;
        `uuid` превращается в `id` подзапросом, потому что порядок идёт по
        `id`, а курсор не должен нести внутренний `bigint` (§7). Подзапрос
        вернёт `NULL` при подделанном `uuid` — `(created_at, NULL)` не пройдёт
        keyset, то есть страница окажется пустой, а не «с начала».
        """
        keyset = ""
        params: dict[str, Any] = {"user_id": user_id, "limit": limit}
        if cursor is not None:
            cursor_created_at, cursor_uuid = cursor
            # CR 9.5 MEDIUM-1: `AND user_id = :user_id` В ПОДЗАПРОСЕ обязателен.
            # Без него курсор работает как оракул: row-comparison в PostgreSQL
            # останавливается на первом неравенстве, поэтому при совпадающем
            # `created_at` подделанный (в том числе ЧУЖОЙ) `uuid` давал `NULL`
            # во втором элементе и курсор вырождался в «date-only» — выдача
            # зависела от существования чужого обращения по его `uuid`.
            # Со своим `user_id` подзапрос находит только СВОИ строки, и
            # любой чужой/несуществующий `uuid` детерминированно даёт `NULL`.
            keyset = (
                "   AND (created_at, id) < "
                "(:cursor_created_at, (SELECT id FROM support_tickets "
                "WHERE uuid = :cursor_uuid AND user_id = :user_id))"
            )
            params["cursor_created_at"] = cursor_created_at
            params["cursor_uuid"] = cursor_uuid
        result = await session.execute(text(cls._LIST_SQL.format(keyset=keyset)), params)
        return [dict(row) for row in result.mappings().all()]

    @classmethod
    async def count_by_user(cls, session: AsyncSession, *, user_id: int) -> int:
        """AC-02: сколько всего своих обращений (не «сколько на странице»)."""
        result = await session.execute(text(cls._COUNT_SQL), {"user_id": user_id})
        return int(result.scalar_one())

    # ——— 9.6: очередь открытых и запись ответа ————————————————————————————

    #: AC-01, D-9.6-3: очередь — ТОЛЬКО `open`, «старые сверху»
    #: (`created_at ASC, id ASC`). Параметра статуса НЕТ намеренно: он открыл бы
    #: путь по неиндексированному полю и позволил бы вернуть отвеченные
    #: обращения в очередь открытых (против AC-04).
    #:
    #: `JOIN users`, а не второй запрос за автором (D-9.6-11): модератору нужен
    #: публичный адрес автора в КАЖДОМ элементе, и отдельный запрос сделал бы
    #: число SQL зависящим от `limit` (ось NFR39).
    #:
    #: `{keyset}` подставляется кодом: текст SQL обязан быть статическим для
    #: плана, значения — только bind-параметры.
    _QUEUE_SQL = """
    SELECT t.uuid, t.body, t.status, t.created_at, t.answer, t.answered_at,
           u.uuid AS author_uuid
      FROM support_tickets t
      JOIN users u ON u.id = t.user_id
     WHERE t.status = 'open'
    {keyset}
     ORDER BY t.created_at ASC, t.id ASC
     LIMIT :limit
    """

    #: AC-04, D-9.6-3: тот же предикат, что у страницы, иначе `total` разошёлся
    #: бы с выдачей (показывал бы «всего 5», отдавая 2).
    _COUNT_OPEN_SQL = """
    SELECT count(*) FROM support_tickets WHERE status = 'open'
    """

    #: AC-02, D-9.6-5, D-9.6-6: ОДИН `UPDATE`, записывающий статус и всю пару
    #: ответа атомарно. `ck_answer_pair` не даст частичной записи: `answered`
    #: без `answer`/`answered_at` отвергается БД, окна «статус есть, ответа
    #: нет» не существует.
    #:
    #: `AND status = 'open'` — CAS (M6): второй модератор получает `rowcount 0`
    #: (`None`), а не перезапись чужого ответа. Без этого предиката повторный
    #: ответ молча менял бы `answer`/`answered_at`/`answered_by_id`, и история
    #: «кто и когда ответил» описывала бы последнего, а не единственного.
    #:
    #: `UPDATE ... FROM users` + `RETURNING u.uuid AS author_uuid`: элемент
    #: ответа — та же схема, что элемент очереди (D-9.6-11), и адрес автора
    #: берётся тем же запросом. Второй `SELECT` за автором дал бы лишний
    #: round-trip на пути записи (ось NFR39).
    _REPLY_SQL = """
    UPDATE support_tickets AS t
       SET status = 'answered',
           answer = :answer,
           answered_at = now(),
           answered_by_id = :moderator_id
      FROM users AS u
     WHERE u.id = t.user_id
       AND t.uuid = :uuid
       AND t.status = 'open'
    RETURNING t.uuid, t.body, t.status, t.created_at, t.answer, t.answered_at,
              u.uuid AS author_uuid
    """

    #: AC-02, D-9.6-5, D-47: проверка под блокировкой строки. Без `FOR UPDATE`
    #: двое модераторов оба прочитали бы `open` и оба прошли предполётную
    #: проверку; CAS в `_REPLY_SQL` отдал бы одному `None`, но `404`/`409`
    #: различались бы по состоянию, прочитанному ДО конкурента.
    #:
    #: `user_id` читается здесь же: адресат уведомления определён под той же
    #: блокировкой, что и решение, — между чтением и записью его нельзя
    #: подменить (D-47).
    _SELECT_FOR_UPDATE_SQL = """
    SELECT status, user_id FROM support_tickets WHERE uuid = :uuid FOR UPDATE
    """

    @classmethod
    async def list_open(
        cls,
        session: AsyncSession,
        *,
        limit: int,
        cursor: tuple[datetime, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """AC-01, AC-04: страница очереди ОТКРЫТЫХ обращений, старые сверху.

        Курсор — пара `(created_at, uuid)` из кодека `reports_queue_cursor`
        (D-9.6-2: переиспользование, второй формат курсора не вводится).
        `uuid` превращается в `id` подзапросом, потому что порядок идёт по
        `(created_at, id)`, а курсор не должен нести внутренний `bigint` (§7).
        """
        keyset = ""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            cursor_created_at, cursor_uuid = cursor
            # CR 9.5 MEDIUM-1 → D-9.6-2, M3: `AND status = 'open'` В ПОДЗАПРОСЕ
            # обязателен. Без него курсор по строке, УШЕДШЕЙ из очереди (её
            # ответили), резолвился бы в `id` и сдвигал страницу не так, как
            # несуществующий `uuid` (тот даёт `NULL`). Разница в выдаче читала
            # бы состояние обращения через параметр пагинации — оракул утечки,
            # ровно тот класс дефекта, что закрыт в 9.5.
            keyset = (
                "   AND (t.created_at, t.id) > "
                "(:cursor_created_at, (SELECT id FROM support_tickets "
                "WHERE uuid = :cursor_uuid AND status = 'open'))"
            )
            params["cursor_created_at"] = cursor_created_at
            params["cursor_uuid"] = cursor_uuid
        result = await session.execute(text(cls._QUEUE_SQL.format(keyset=keyset)), params)
        return [dict(row) for row in result.mappings().all()]

    @classmethod
    async def count_open(cls, session: AsyncSession) -> int:
        """AC-01, AC-04: сколько всего ОТКРЫТЫХ (не «сколько на странице»)."""
        result = await session.execute(text(cls._COUNT_OPEN_SQL))
        return int(result.scalar_one())

    @classmethod
    async def select_for_update(
        cls, session: AsyncSession, *, ticket_uuid: Any
    ) -> dict[str, Any] | None:
        """AC-02, D-9.6-5: состояние обращения и его автор ПОД блокировкой.

        `None` — обращения нет (`404`). Возвращает `{status, user_id}`:
        `status` различает `open` и уже отвеченное (`409`), `user_id` —
        адресат уведомления, прочитанный в той же транзакции, что и решение.
        """
        result = await session.execute(text(cls._SELECT_FOR_UPDATE_SQL), {"uuid": ticket_uuid})
        row = result.mappings().one_or_none()
        return None if row is None else dict(row)

    @classmethod
    async def reply(
        cls,
        session: AsyncSession,
        *,
        ticket_uuid: Any,
        answer: str,
        moderator_id: int,
    ) -> dict[str, Any] | None:
        """AC-02, D-9.6-6: записывает ответ ОДНИМ `UPDATE`; без `commit`.

        `None` — CAS не сработал: обращение уже отвечено (или исчезло между
        блокировкой и записью). Это не ошибка SQL, а ожидаемый исход гонки,
        и различает его вызывающий (`409`).
        """
        result = await session.execute(
            text(cls._REPLY_SQL),
            {"uuid": ticket_uuid, "answer": answer, "moderator_id": moderator_id},
        )
        row = result.mappings().one_or_none()
        return None if row is None else dict(row)
