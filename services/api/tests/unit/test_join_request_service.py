"""Гарды подачи заявки на фейках (стори 5а.2, Task 7.3; AC-03…AC-07).

§18.2: без БД проверяется ПОРЯДОК отказов и выбор исключения. Сама запись,
гонка вставки и блокировка актора проверяются на живой PostgreSQL в
`tests/integration/test_join_request_races.py` — здесь они были бы тавтологией
(фейк повторял бы реализацию).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.enums import EventCardStatus
from app.services.join_request_service import (
    CapacityOneUsesRequestsError,
    CardNotAcceptingError,
    JoinRequestCardNotFoundError,
    JoinRequestLimitReachedError,
    JoinRequestOwnCardError,
    submit_join_request,
)

APPLICANT_ID = 7
CARD_UUID = uuid_lib.UUID("11111111-1111-1111-1111-111111111111")
COMMUNITY = "tomsk"


class _Settings:
    """Протокол `JoinRequestSettings` без чтения `.env`."""

    default_city_slug = COMMUNITY
    join_request_active_limit = 2


class _Card:
    """Минимум полей `EventCard`, который читает сервис (`is_request_mode` — сам)."""

    def __init__(
        self,
        *,
        author_id: int = 99,
        status: str = EventCardStatus.PUBLISHED,
        capacity: int = 1,
    ) -> None:
        self.id = 42
        self.uuid = CARD_UUID
        self.author_id = author_id
        self.status = status
        self.capacity = capacity
        # W-02 (стори 11.9): дата ОТНОСИТЕЛЬНАЯ момента вызова, а не литерал.
        # Литерал `2026-10-25` стал прошлым 26 октября, и три теста начали
        # падать на гарде истёкшего окна (5б.1) ДО проверяемого утверждения —
        # не потому, что фейк разошёлся с контрактом, а потому что календарь
        # прошёл. Отдельный тест истёкшего окна держит гард и остаётся как есть.
        self.expires_at = datetime.now(UTC) + timedelta(days=7)

    @property
    def is_request_mode(self) -> bool:
        """Тот же предикат, что у модели: `capacity == 1` (4.1, FR70)."""
        return self.capacity == 1


class _Repo:
    """Фейковый репозиторий: отдаёт заготовленную карточку и счётчик активных."""

    def __init__(
        self, *, card: _Card | None, active_count: int = 0, log: list[str] | None = None
    ) -> None:
        self.card = card
        self.active_count = active_count
        self.created: list[dict[str, Any]] = []
        # CR 5а.2, патч 2: журнал порядка обращений к БД.
        self._log = log

    async def get_card_for_request(
        self, card_uuid: uuid_lib.UUID, city_slug: str
    ) -> _Card | None:
        # CR 5а.2, патч 2: отметка о чтении карточки — без неё порядок
        # «блокировка ДО карточки» не наблюдаем: фейк не идёт в `session.execute`.
        if self._log is not None:
            self._log.append("card")
        return self.card

    async def count_active(self, applicant_id: int) -> int:
        return self.active_count

    async def create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        raise AssertionError("запись не должна происходить на этой ветке")


class _Session:
    """Фейковая сессия: блокировка и `commit` — заглушки.

    Блокировка (`SELECT ... FOR UPDATE`) — предмет интеграционного теста AC-09,
    поэтому здесь её результат не моделируется: важно лишь, что сервис её
    вызывает ДО чтения карточки.
    """

    def __init__(self, log: list[str] | None = None) -> None:
        self.executed: list[Any] = []
        self.committed = False
        self._log = log

    async def execute(self, statement: Any) -> Any:
        self.executed.append(statement)
        if self._log is not None:
            self._log.append("lock")

        class _Result:
            def scalar_one_or_none(self) -> None:
                return None

        return _Result()

    async def commit(self) -> None:
        self.committed = True


async def _submit(repo: _Repo, session: _Session) -> Any:
    return await submit_join_request(
        session,  # type: ignore[arg-type]
        applicant_id=APPLICANT_ID,
        card_uuid=CARD_UUID,
        message="привет",
        settings=_Settings(),
        repository=repo,  # type: ignore[arg-type]
    )


async def test_missing_card_raises_not_found() -> None:
    """AC-07 (ADR-13): нет карточки — `404`, и до вставки дело не доходит.

    Краснеет, если снять проверку `card is None` — сервис упал бы `AttributeError`
    (`500` вместо `404`) на первой же попытке подать заявку по удалённой карточке.
    """
    with pytest.raises(JoinRequestCardNotFoundError):
        await _submit(_Repo(card=None), _Session())


async def test_own_card_raises_forbidden() -> None:
    """AC-05: своя карточка — `403 JOIN_REQUEST_OWN_CARD`.

    Краснеет, если снять проверку `card.author_id == applicant_id`: автор
    подал бы заявку сам себе и занял бы собственное место.
    """
    with pytest.raises(JoinRequestOwnCardError):
        await _submit(_Repo(card=_Card(author_id=APPLICANT_ID)), _Session())


@pytest.mark.parametrize(
    "status",
    [
        EventCardStatus.DRAFT,
        EventCardStatus.CLOSED,
        EventCardStatus.FULL,
        EventCardStatus.FINISHED,
        EventCardStatus.CANCELLED,
    ],
)
async def test_not_published_card_is_rejected(status: str) -> None:
    """AC-05 (FR74): принимает только `published`; пять статусов — пять `409`.

    Краснеет, если убрать проверку статуса: заявка легла бы на черновик и на
    завершённую встречу, а автор увидел бы заявителей на закрытой карточке.
    """
    with pytest.raises(CardNotAcceptingError):
        await _submit(_Repo(card=_Card(status=status)), _Session())


async def test_expired_window_is_not_accepting() -> None:
    """5б.1 (NFR20): истёкшее окно решения закрывает приём заявок — `409`.

    **Зачем этот тест появился в CR 11.9.** Мутация H-7 («снять
    `if card.expires_at <= moment`») прошла ВСЕ тесты: 12 unit зелёных и
    14 integration (`test_card_decision_window_api.py`) тоже. То есть гард
    существовал в коде и не был доказан ничем — ровно тот класс, который
    P-25 называет «проверка формы вместо эффекта».

    **Краснеет от:** снятия гарда (`if False`), смены `<=` на `<` на границе
    `expires_at == moment`, и от переноса проверки ПОСЛЕ проверки режима —
    тогда истёкшая карточка отвечала бы `CAPACITY_ONE_USES_REQUESTS`.
    """
    card = _Card()
    # Ровно граница: `<=` обязан сработать. Карточка `capacity=1` — режим
    # заявок, значит без гарда тест получил бы `CapacityOneUsesRequestsError`,
    # а не `CardNotAcceptingError`.
    card.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(CardNotAcceptingError):
        await _submit(_Repo(card=card), _Session())


async def test_capacity_above_one_is_rejected() -> None:
    """AC-04 (FR70): `capacity=3` — не режим заявок, `409`.

    Краснеет, если проверять режим сравнением `capacity == 1` в сервисе и
    забыть про будущие режимы, либо снять проверку целиком: заявки появились бы
    у карточки открытого набора, где присоединение мгновенное (5а.5).
    """
    with pytest.raises(CapacityOneUsesRequestsError):
        await _submit(_Repo(card=_Card(capacity=3)), _Session())


async def test_limit_reached_carries_numbers_only() -> None:
    """AC-03: две активные заявки — `429` с `details.{limit, active_count}`.

    Краснеет, если снять `>= limit` (третья заявка прошла бы) или добавить
    в `details` `uuid` заявок (раскрытие состава чужих заявок и перебор).
    """
    with pytest.raises(JoinRequestLimitReachedError) as exc:
        await _submit(_Repo(card=_Card(), active_count=2), _Session())
    assert exc.value.details == {"limit": 2, "active_count": 2}


async def test_order_of_refusals_own_card_before_status() -> None:
    """AC-05: порядок `403` → `409` статус.

    Краснеет, если переставить проверки: своя карточка в статусе `draft`
    ответила бы `409 CARD_NOT_ACCEPTING`, и клиент пошёл бы «искать другую
    карточку» вместо того, чтобы просто не заявляться на свою.
    """
    card = _Card(author_id=APPLICANT_ID, status=EventCardStatus.DRAFT)
    with pytest.raises(JoinRequestOwnCardError):
        await _submit(_Repo(card=card), _Session())


async def test_order_of_refusals_status_before_mode() -> None:
    """AC-05: порядок `409` статус → `409` режим.

    Краснеет, если проверить режим раньше статуса: закрытая карточка
    открытого набора ответила бы `CAPACITY_ONE_USES_REQUESTS`, то есть
    «это не тот режим» вместо «набор закрыт».
    """
    card = _Card(status=EventCardStatus.CLOSED, capacity=3)
    with pytest.raises(CardNotAcceptingError):
        await _submit(_Repo(card=card), _Session())


async def test_lock_is_taken_before_card_read() -> None:
    """AC-09 (ADR-09): блокировка актора — ДО чтения карточки, а не «где-то».

    Краснеет, если перенести `lock_for_update` после `get_card_for_request`:
    две параллельные подачи прочли бы карточку одновременно и не встали бы в
    очередь на блокировке — лимит «2 активных» перестал бы держаться (AC-08).

    CR 5а.2, патч 2: прежняя версия проверяла только `executed[0]` и НЕ ловила
    перенос блокировки за чтение карточки — фейковый репозиторий не обращается
    к `session.execute`, поэтому блокировка оставалась нулевым элементом. Мутация
    «lock после чтения карточки» проходила и этот тест, и AC-09-тест на
    `before_cursor_execute` (там `FOR UPDATE` всё равно первый среди блокировок).
    Различитель — общий журнал порядка: `lock` обязан идти раньше `card`.
    """
    order: list[str] = []
    session = _Session(log=order)
    with pytest.raises(CapacityOneUsesRequestsError):
        await _submit(_Repo(card=_Card(capacity=2), log=order), session)
    assert session.executed, "блокировка актора не выполнена"
    first_sql = str(session.executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "users" in first_sql
    assert "FOR UPDATE" in first_sql
    assert order == ["lock", "card"], f"порядок обращений к БД: {order}"
