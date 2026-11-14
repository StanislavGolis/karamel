"""Сервис карточки события: создание, правка черновика, публикация.

Стори 4.2, Task 3–5; AC-01…AC-08. project-context §6: здесь правила домена,
запросы — в `EventCardRepository`, HTTP — в роутере.

Ключевые решения стори:

- **D-4.2-3:** `expires_at` считается ОДНОЙ функцией (`_compute_expires_at`)
  из двух мест — создания и публикации. Две копии разошлись бы при первой
  правке формулы окна (ADR-02: `LEAST(starts_at, now() + окно)`).
- **Порядок проверок при создании:** место → время → запись. Место — единственная
  проверка с запросом в БД; время считается в памяти. При двух нарушениях
  одновременно ответ `PLACE_NOT_AVAILABLE` (зафиксировано тестом).
- **Правка только черновика** (D-4.2-2): FR99 требует «нет присоединений»,
  а для этого нужны `join_requests`/`filled_count` (5.x); полное правило — 4.6.
- **Чужая карточка = несуществующая** (ADR-13): `get_owned` фильтрует по
  `author_id`, поэтому обе дают один `404 card_not_found`, без `403`.
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    ACTIVE_STATUSES,
    ARCHIVABLE_STATUSES,
    EDITABLE_STATUSES,
    Activity,
    CardKind,
    CardVerificationStatus,
    EventCardStatus,
    PlaceCategory,
)
from app.models.event_card import CARD_REQUEST_MODE_CAPACITY, EventCard
from app.repositories.event_cards import EventCardRepository
from app.repositories.join_requests import JoinRequestRepository
from app.repositories.profile_photos import ProfilePhotoRepository
from app.repositories.profiles import ProfileRepository
from app.repositories.users import UserRepository
from app.schemas.event_card import EventCardAuthorProfile, EventCardCreate, EventCardUpdate
from app.services.age_service import compute_age, today_or_now
from app.services.card_content_rules import CardContent, evaluate_card_content
from app.services.card_decision_service import DecisionOutcome, close_due_card
from app.services.card_feed_cursor import decode_feed_cursor, encode_feed_cursor
from app.services.card_title import generate_card_title
from app.services.event_card_place import ensure_place_available
from app.services.media_service import (
    MediaStorageError,
    MediaUnavailableError,
    get_signed_url,
)
from app.services.notification_service import (
    notify_company_assembled,
    notify_request_selected,
)
from app.services.system_message_service import (
    SystemMessageKind,
    append_for_card,
    publish_system_message,
)

logger = structlog.get_logger(__name__)

# AC-4, D-12.7-2 стори 12.7 (ADR-13): заведомо несуществующий `card_id` для
# выравнивания стоимости пути «карточки нет» с путём «карточка есть».
# Приём CR 6.3 F-1 (`chat_participants_service._MISSING_CARD_ID`), повторён
# локально, а не импортирован: сервис карточки не должен зависеть от сервиса
# состава — это связало бы два домена ради одной константы-нуля.
# `0` не может быть `id` настоящей карточки (`BIGSERIAL` начинается с 1).
_MISSING_CARD_ID = 0


def _system_message_settings() -> Any:
    """Стори 6.7: настройки шифрования переписки для системных сообщений.

    Глобальный `Settings` намеренно, а не протокол операции: ключ переписки —
    общая инфраструктура приложения, а не параметр отмены карточки, и
    добавление поля в `CardSettings` заставило бы править каждый стаб ради
    значения, которого в них нет.
    """
    from app.core.config import settings as app_settings

    return app_settings


# AC-01, FR71 (стори 4.4): набор активных статусов определён в `models/enums.py`,
# потому что его читает репозиторий, а сервис уже импортирует репозиторий
# (обратный импорт дал бы цикл). Здесь — реэкспорт: правило домена
# принадлежит сервису, и внешний код импортирует его отсюда.
__all__ = ("ACTIVE_STATUSES",)

# AC-07 (ADR-13): код без раскрытия существования — `403` отличил бы
# «чужая карточка» от «нет такой».
CARD_NOT_FOUND_CODE = "card_not_found"
# AC-03 (FR69): прошедшее время — семантика домена, `422`.
CARD_STARTS_AT_IN_PAST_CODE = "STARTS_AT_IN_PAST"
# Стори 12.5, AC-01 (FR116, ADR-21): горизонт автора — не дальше 72 часов.
# Горизонт — валидация на ВХОДЕ, не TTL: поля `published_until` нет, воркера,
# снимающего карточки по времени публикации, тоже нет.
# Верхний регистр — §4 `project-context`; эпик писал нижним (`starts_at_too_far`),
# расхождение — долг `W-12.5-b` (эпик привести к проекту, не наоборот).
CARD_STARTS_AT_TOO_FAR_CODE = "STARTS_AT_TOO_FAR"
# Стори 12.5, AC-01: предел горизонта в часах — ОДНО место на сервере.
# Литерал `72` в других файлах запрещён: число разъедется молча (урок `W-11.7-c`).
CARD_MAX_HORIZON_HOURS = 72
# AC-05 (D-4.2-2): править можно только черновик.
CARD_NOT_DRAFT_CODE = "CARD_NOT_DRAFT"
# AC-06 (FR113): отклонённая карточка — причина обязательна в `details`.
CARD_REJECTED_CODE = "CARD_REJECTED"
# AC-06 (FR101): публикация непроверенного текста запрещена.
CARD_VERIFICATION_PENDING_CODE = "VERIFICATION_PENDING"
# AC-05: окно решения обязательно в режиме заявок (FR70/FR79). Схема 4.1
# отбивает это на создании, но `PATCH` может сменить вместимость 2–6 → 1.
CARD_DECISION_WINDOW_REQUIRED_CODE = "decision_window_required"
# Стори 12.6, AC-6.4 (FR119, ADR-19): перевёрнутая пара границ возраста.
# Нижний регистр — как у соседнего `decision_window_required` (код правила
# формы, а не доменного состояния). Без этого кода и ветки в `_domain_error`
# `IntegrityError` от CHECK `ck_event_cards_age_range` стал бы `500` —
# ровно тот дефект, что был найден в 12.5 (`STARTS_AT_TOO_FAR` → `raise error`).
CARD_AGE_RANGE_INVALID_CODE = "age_range_invalid"
# Стори 4.4, Task 7 (AC-07, FR71): отказ по лимиту активных карточек.
# §7.2 закрепляет `429` за лимитами, `409` — за конфликтом состояния.
CARD_LIMIT_REACHED_CODE = "CARD_LIMIT_REACHED"

# Стори 4.6, Task 1 (AC-01, FR99): первое присоединение уже есть — правка
# запрещена, доступна только отмена. `409`, не `422`: карточка существует и
# валидна, конфликт в её состоянии (§7.2).
CARD_ALREADY_JOINED_CODE = "CARD_ALREADY_JOINED"
# AC-01: терминальные состояния (`closed`/`finished`/`cancelled`) не правятся.
# Отдельный код от `CARD_NOT_DRAFT`: клиент показывает РАЗНЫЕ действия —
# «снимите присоединение» против «эта карточка закрыта» (урок 4.3: `CARD_REJECTED`
# vs `VERIFICATION_PENDING`).
CARD_NOT_EDITABLE_CODE = "CARD_NOT_EDITABLE"
# AC-02, FR101: правка опубликованной не прошла автопроверку. Не `pending`:
# `pending` вернул бы непроверенный текст в ленту либо снял карточку у
# участников (D-4.6-3). Ответ — отказ без мутаций.
CARD_EDIT_REQUIRES_REVIEW_CODE = "CARD_EDIT_REQUIRES_REVIEW"
# AC-03, FR74: закрыть можно только опубликованную карточку.
CARD_NOT_PUBLISHED_CODE = "CARD_NOT_PUBLISHED"
# AC-04, FR100: отмена невозможна из `finished` (история) и `cancelled`.
CARD_NOT_CANCELLABLE_CODE = "CARD_NOT_CANCELLABLE"
# AC-09, X-22: публикация при заполненной вместимости. Без этой проверки
# `CHECK ck_event_cards_full_capacity` давал бы `IntegrityError` → `500`.
CARD_NO_SEATS_CODE = "CARD_NO_SEATS"


class CardSettings(Protocol):
    """Минимум настроек, который нужен сервису (не весь `Settings`).

    Протокол, а не импорт `Settings`: unit-тест подставляет три поля без
    чтения `.env` (тот же приём, что в `PlaceService` после 3.1).
    """

    default_city_slug: str
    city_slugs: tuple[str, ...]
    card_default_decision_window_minutes: int
    # Стори 4.3 (AC-02, FR91): стоп-слова автопроверки. В протоколе, а не
    # в `Settings` напрямую: unit-тест подставляет кортеж без чтения `.env`.
    card_content_stop_words: tuple[str, ...]
    # Стори 4.4 (AC-01, AC-02, FR71): предел активных карточек автора.
    # В протоколе по тому же приёму: unit-тест лимита подставляет `1`/`2`
    # и не читает `.env` (иначе тест зависел бы от локального файла).
    card_active_limit: int
    # Стори 12.7 (AC-5, D-12.7-4, NFR23): общий TTL подписи на фото.
    # Поле есть в `Settings` (`s3_signed_url_ttl`, config.py:379); в протоколе
    # оно отсутствовало, и mypy считал `_author_photo_ttl` возвращающей `Any`.
    # Объявлено здесь, а не прочитано через `Settings` напрямую: unit-тест
    # подставляет число без чтения `.env` — тот же приём, что выше.
    s3_signed_url_ttl: int


class CardNotFoundError(Exception):
    """AC-07 (ADR-13): карточки нет ИЛИ она чужая — ответ один."""

    code = CARD_NOT_FOUND_CODE


class CardStartsAtInPastError(Exception):
    """AC-03 (FR69): время начала уже прошло."""

    code = CARD_STARTS_AT_IN_PAST_CODE


class CardStartsAtTooFarError(Exception):
    """AC-01 (FR116, ADR-21): время начала дальше горизонта автора (72 ч).

    Отдельный класс от `CardStartsAtInPastError`: причины разные, и текст для
    человека разный («в прошлом» vs «слишком далеко»). Проверки стоят парой,
    порядок — прошлое ПЕРВЫМ (прошедшая дата информативнее «слишком далеко»).
    """

    code = CARD_STARTS_AT_TOO_FAR_CODE


class CardNotDraftError(Exception):
    """AC-05: карточка не в `draft` — правка/публикация недопустима."""

    code = CARD_NOT_DRAFT_CODE


class CardRejectedError(Exception):
    """AC-06 (FR113): карточка отклонена модератором — нужна причина."""

    code = CARD_REJECTED_CODE

    def __init__(self, reason: str | None) -> None:
        super().__init__(CARD_REJECTED_CODE)
        self.reason = reason


class CardVerificationPendingError(Exception):
    """AC-06 (FR101): публикация до завершения проверки невозможна."""

    code = CARD_VERIFICATION_PENDING_CODE


class CardDecisionWindowRequiredError(Exception):
    """AC-05 (FR70/FR79): режим заявок требует явного окна решения."""

    code = CARD_DECISION_WINDOW_REQUIRED_CODE


class CardAgeRangeInvalidError(Exception):
    """AC-6.4 стори 12.6 (FR119): нижняя граница возраста больше верхней.

    Отдельный класс, а не `ValueError` из CHECK: `422` с машинным кодом
    `age_range_invalid` позволяет клиенту показать текст у поля возраста.
    Проверка стоит и в схеме (когда обе границы в одном запросе), и здесь
    (когда `PATCH` даёт перевёрнутую пару относительно текущего значения).
    """

    code = CARD_AGE_RANGE_INVALID_CODE


class CardLimitReachedError(Exception):
    """AC-01, AC-07 (FR71, NFR30): у автора уже лимит активных карточек.

    `details` несёт `limit` и `active_count` — данные для сообщения клиенту.
    `uuid` активных карточек НЕ передаются (OQ-8): они раскрыли бы состав
    ленты автора и позволили бы перебор. `429`, а не `409`: §7.2 закрепляет
    `429` за лимитами, `409` — за конфликтом состояния карточки.
    """

    code = CARD_LIMIT_REACHED_CODE

    def __init__(self, *, limit: int, active_count: int) -> None:
        super().__init__(CARD_LIMIT_REACHED_CODE)
        self.limit = limit
        self.active_count = active_count

    @property
    def details(self) -> dict[str, int]:
        """AC-07: только числа — без `uuid` и без чужих данных."""
        return {"limit": self.limit, "active_count": self.active_count}


class CardAlreadyJoinedError(Exception):
    """AC-01 (FR99): первая заявка/присоединение уже есть — править нельзя.

    На этом шаге «присоединение» = `filled_count > 0` (D-4.6-1): таблицы
    `join_requests` нет до 5а. Когда появятся заявки, условие расширится
    одним предикатом в гарде, а не второй проверкой рядом (иначе два
    источника правды об одном факте).
    """

    code = CARD_ALREADY_JOINED_CODE


class CardNotEditableError(Exception):
    """AC-01: карточка в терминальном состоянии (`closed`/`finished`/`cancelled`).

    Отдельно от `CardNotDraftError`: тот остаётся у публикации (нельзя
    опубликовать не-черновик), а здесь запрет правки закрытой карточки.
    Один код на два разных действия заставил бы клиент гадать.
    """

    code = CARD_NOT_EDITABLE_CODE


class CardEditRequiresReviewError(Exception):
    """AC-02 (FR101): правка опубликованной не прошла автопроверку.

    Ни одно поле не присваивается: карточка в ленте остаётся прежней, пока
    автор не переформулирует. Альтернатива (`pending` + снятие с ленты)
    ломала бы участников, которые уже видели встречу (D-4.6-3).
    """

    code = CARD_EDIT_REQUIRES_REVIEW_CODE


class CardNotPublishedError(Exception):
    """AC-03 (FR74): закрыть набор можно только у опубликованной карточки."""

    code = CARD_NOT_PUBLISHED_CODE


class CardNotCancellableError(Exception):
    """AC-04 (FR100): отмена невозможна — встреча прошла или уже отменена."""

    code = CARD_NOT_CANCELLABLE_CODE


class CardNoSeatsError(Exception):
    """AC-09 (X-22): публикация при `filled_count >= capacity`.

    Достижимо только при данных 5.x (заявки), но проверка живёт в сервисе
    с этой стори: `500` от CHECK — дефект, а не контракт.
    """

    code = CARD_NO_SEATS_CODE


def _compute_expires_at(*, starts_at: datetime, window_minutes: int, now: datetime) -> datetime:
    """AC-04, ADR-02, D-4.2-3: `LEAST(starts_at, now + окно)` — единственная формула.

    `min`, а не безусловное `now + окно`: окно не может закрыться позже начала
    встречи (CHECK `ck_event_cards_expires_at`), иначе решение принималось бы
    по уже прошедшему событию.

    Вызывается из ДВУХ мест (создание и публикация) — D-4.2-3: одна функция,
    чтобы формула не разъехалась между путями.
    """
    return min(starts_at, now + timedelta(minutes=window_minutes))


def _resolve_city_slug(settings: CardSettings) -> str:
    """AC-01, ADR-01: слаг из конфига для ЗАПИСИ. Чтение — `user.city_slug`.

    Стори 10.1, AC-05, задача 4.4: функция вызывается ТОЛЬКО из `create_card`.
    Чтение карточки (`list_cards`, `get_card`) берёт слаг из
    `viewer_city_slug` — то есть из `users.city_slug` зрителя.
    Второй источник на чтении вернул бы «невидимое сообщество»: карточка
    создана в конфиге, а ищется по колонке пользователя (CR 10.1, DN-1).

    Сверка с `city_slugs` обязательна: опечатка в `.env` создала бы
    карточку в «невидимом сообществе», которой нет ни в одной ленте
    (`WHERE city_slug = ...` не совпадёт) — тот же дефект, что закрыт
    в 3.1 AC-03 для мест.
    """
    slug = settings.default_city_slug
    if slug not in settings.city_slugs:
        raise ValueError(
            f"default_city_slug {slug!r} отсутствует в city_slugs: "
            "карточка попала бы в невидимое сообщество"
        )
    return slug


def _resolve_decision_window(
    *, capacity: int, requested: int | None, settings: CardSettings
) -> int:
    """AC-03, FR70: окно из клиента в режиме заявок, из конфига — в открытом наборе.

    `capacity=1` без явного окна — ошибка: заявки висели бы бессрочно.
    При 2–6 окно клиентом не запрашивается (FR70), значение — из конфига
    (OQ-1: 2880 = 48 ч).
    """
    if capacity == CARD_REQUEST_MODE_CAPACITY:
        if requested is None:
            raise CardDecisionWindowRequiredError()
        return requested
    if requested is not None:
        return requested
    return settings.card_default_decision_window_minutes


def run_auto_check(card: EventCard, *, settings: CardSettings, now: datetime) -> None:
    """AC-01, AC-06 (FR101, ADR-08): автопроверка текста перед записью.

    ADR-08: детерминированные правила переводят `pending → approved` синхронно
    при создании; в `pending` остаются только сработавшие правила. Поэтому:

    - правила не сработали → `approved`, `verified_at=now`, `verified_by IS NULL`
      (NULL = автоматическая проверка, ADR-08);
    - сработало хотя бы одно → `pending`, поля проверки `NULL`. Автоматического
      `rejected` НЕТ (OQ-2): FR113 требует причину от человека, NFR54 — санкцию
      человеком.

    `rejection_reason` сбрасывается всегда: причина относилась к предыдущему
    тексту и после правки вводила бы в заблуждение (AC-06, D-4.3-3).

    `commit` НЕ вызывается: функция — часть транзакции вызывающего сервиса.
    """
    result = evaluate_card_content(
        CardContent(
            title=card.title,
            description=card.description,
            category=card.category,
        ),
        stop_words=settings.card_content_stop_words,
    )
    card.rejection_reason = None
    if result.passed:
        card.verification_status = CardVerificationStatus.APPROVED
        card.verified_at = now
        # ADR-08: NULL = автопроверка, не человек.
        card.verified_by = None
    else:
        card.verification_status = CardVerificationStatus.PENDING
        card.verified_at = None
        card.verified_by = None


async def create_card(
    session: AsyncSession,
    *,
    author_id: int,
    payload: EventCardCreate,
    settings: CardSettings,
    now: datetime | None = None,
    repository: EventCardRepository | None = None,
) -> EventCard:
    """AC-01…AC-04, AC-08: создаёт черновик карточки.

    Порядок: место → время → `expires_at` → `INSERT` → `commit`. Место первым
    осознанно: это единственная проверка с запросом в БД, и при двух нарушениях
    одновременно клиент получает `PLACE_NOT_AVAILABLE` (AC-02 приоритетнее
    AC-03 — зафиксировано тестом 8.3).

    `now` — параметр, а не `datetime.now(UTC)` внутри: один и тот же момент
    используется и для сравнения `starts_at`, и для `expires_at`, иначе два
    вызова разошлись бы в микросекунды и тест стал бы невоспроизводимым
    (Task 3.3). В рабочем коде `now` не передаётся.

    `repository` — только для тестов (подмена без БД), как в `event_card_place`.
    """
    moment = now if now is not None else datetime.now(UTC)
    city_slug = _resolve_city_slug(settings)

    # AC-02: место проверяется первым — до вставки и до проверки времени.
    #
    # CR 4.2 (найдено мутацией 1, которая ВЫЖИЛА против этого вызова): здесь
    # стояли ДВА обращения к месту — `ensure_place_available` и
    # `_resolve_place_id`. Второе использует тот же билдер предиката и бросает
    # ту же `PlaceNotAvailableError`, поэтому удаление первого ничего не меняло
    # в поведении: проверка проходила, а лишний запрос в БД оставался.
    # Оставлено ОДНО обращение — `_resolve_place_id`: оно и проверяет место,
    # и даёт `place_id` для FK. Порядок «место → время» сохранён.

    # AC-02, AC-03: место резолвится ПЕРВЫМ — до проверки времени.
    # Единственное обращение к `places`: даёт `place_id` для FK и бросает
    # `PlaceNotAvailableError` тем же билдером предиката, что гард 4.1.
    #
    # AC-3 стори 13.2: нужен не только `id`, но и `category` — предлог и падеж
    # заголовка выбираются по КАТЕГОРИИ МЕСТА (ADR-26). Второго запроса нет:
    # та же строка уже прочитана гардом доступности.
    place = await _resolve_place(session, place_uuid=payload.place_uuid, city_slug=city_slug)
    place_id = place.id

    # AC-03: сравнение строго с серверным моментом; `<=` тоже отказ —
    # «сейчас» уже не «будущее».
    if payload.starts_at <= moment:
        raise CardStartsAtInPastError()

    # AC-01 стори 12.5 (FR116, ADR-21): горизонт автора — не дальше 72 часов.
    # Порядок «прошлое → далеко» обязателен: прошедшая дата информативнее
    # «слишком далеко» (U-5). Граница ВКЛЮЧИТЕЛЬНАЯ (`<=`): ровно 72 часа —
    # ещё допустимо, иначе тесты с `days=3` покраснели бы не по делу.
    if payload.starts_at > moment + timedelta(hours=CARD_MAX_HORIZON_HOURS):
        raise CardStartsAtTooFarError()

    window = _resolve_decision_window(
        capacity=payload.capacity, requested=payload.decision_window_minutes, settings=settings
    )
    expires_at = _compute_expires_at(starts_at=payload.starts_at, window_minutes=window, now=moment)

    # AC-3 стори 13.2 (FR130, ADR-26): заголовок собирается сервером из
    # активности и места. Схема уже отвергла присланный `title` (AC-7),
    # поэтому `payload.title` для не-легаси активности всегда `None`.
    #
    # Собирается ДО `run_auto_check`: автопроверка читает `card.title`, и
    # `None` дал бы `failed_rules=('length',)` на каждой карточке — то есть
    # весь поток уходил бы в ручную модерацию (ADR-26, «три уровня защиты»).
    generated_title = generate_card_title(
        activity=payload.activity,
        place_category=PlaceCategory(place.category),
        place_name=place.name,
    )

    card = EventCard(
        author_id=author_id,
        city_slug=city_slug,
        title=generated_title,
        description=payload.description,
        category=str(payload.category),
        # AC-3 стори 12.4 (FR117, FR118): тип и активность приходят от автора
        # явно и сохраняются как есть. Вывода из `capacity` нет намеренно —
        # это была бы вторая правда о смысле встречи (ADR-20, AC-4).
        kind=str(payload.kind),
        activity=str(payload.activity),
        # AC-2 стори 12.6 (FR119, ADR-19): рамки аудитории сохраняются как
        # есть. `None` — рамка не задана (мягкий фильтр: карточка видна всем).
        # Приведение enum→str то же, что у `kind`/`activity`: в БД строка,
        # CHECK держит белый список.
        gender_filter=str(payload.gender_filter) if payload.gender_filter is not None else None,
        age_min=payload.age_min,
        age_max=payload.age_max,
        starts_at=payload.starts_at,
        capacity=payload.capacity,
        decision_window_minutes=window,
        expires_at=expires_at,
        # AC-01, ADR-08: исходное состояние. Автопроверка контента — 4.3;
        # до неё `approved` выставляется вручную (D-4.2-5).
        status=EventCardStatus.DRAFT,
        verification_status=CardVerificationStatus.PENDING,
        filled_count=0,
    )
    card.place_id = place_id

    # AC-01 (стори 4.3, FR101, ADR-08): автопроверка текста ДО записи.
    # Ставит `approved` + `verified_at`/`verified_by`, если правила чисты, или
    # оставляет `pending` — тогда карточка попадёт в очередь модератора.
    run_auto_check(card, settings=settings, now=moment)

    repo = repository if repository is not None else EventCardRepository(session)
    repo.add(card)
    await session.commit()
    return card


async def _resolve_place(
    session: AsyncSession, *, place_uuid: uuid_lib.UUID, city_slug: str
) -> Any:
    """Стори 13.2 (AC-3): доступное место ЦЕЛИКОМ — нужна `category`.

    Не `Place` в аннотации: модуль `models.place` не импортируется в сервис
    верхним уровнем (там уже есть `event_card_place`, который сам его тянет),
    а импорт ради аннотации создал бы цикл `services` → `models` внутри
    одного модуля с `event_card_place`. Возвращается ORM-объект, читаются
    только `id` и `category` — тип проверяется тестом, а не аннотацией.

    `PlaceNotAvailableError` — та же, что из `_resolve_place_id`: предикат
    один (`build_availability_statement`), поэтому и ошибка одна.
    """
    from app.repositories.places import PlaceRepository
    from app.services.event_card_place import PlaceNotAvailableError

    place = await PlaceRepository(session).find_available_for_card(
        place_uuid=place_uuid, city_slug=city_slug
    )
    if place is None:
        raise PlaceNotAvailableError(place_uuid)
    return place


async def _resolve_place_id(
    session: AsyncSession, *, place_uuid: uuid_lib.UUID, city_slug: str
) -> int:
    """AC-02: внутренний `place_id` для FK — из ТОЙ ЖЕ проверки доступности.

    Второго предиката нет: и `ensure_place_available` (4.1), и этот резолв
    используют один билдер `build_availability_statement`. Иначе проверка
    и заполнение FK разошлись бы — место могло бы пройти гард и не найтись
    при резолве (или наоборот).

    `PlaceNotAvailableError` вместо `AssertionError`: гард уже отработал, но
    между двумя запросами место могли скрыть. Ответ тот же `422`, что и на
    первом шаге — клиент не видит разницы.
    """
    from app.repositories.places import PlaceRepository
    from app.services.event_card_place import PlaceNotAvailableError

    place = await PlaceRepository(session).find_available_for_card(
        place_uuid=place_uuid, city_slug=city_slug
    )
    if place is None:
        raise PlaceNotAvailableError(place_uuid)
    return place.id


async def update_card(
    session: AsyncSession,
    *,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    payload: EventCardUpdate,
    settings: CardSettings,
    now: datetime | None = None,
    repository: EventCardRepository | None = None,
) -> EventCard:
    """AC-05, AC-02…AC-04, AC-07: правка черновика.

    Порядок: заблокировать автора → найти свою карточку ПОД БЛОКИРОВКОЙ →
    проверить статус → проверить новое место (если меняется) → пересчитать
    окно и `expires_at` → `commit`.

    «Меняются только переданные поля» реализуется через `None` как «не трогать»:
    итоговые значения считаются из СМЕСИ payload и текущей строки (Task 4.6).
    Иначе `PATCH` только `title` сбросил бы окно решения на дефолт.

    **Блокировки (X-6.1-1, chore P-7; ADR-09).** Шаг 1 — `users` автора,
    шаг 2 — `event_cards` под `FOR UPDATE`. До правки карточка читалась БЕЗ
    блокировки, и гард AC-08 (`filled_count > 0` запрещает выход из режима
    заявок) проверялся по снимку: параллельный `join_open_card` между чтением
    и `commit` переводил карточку с участником в открытый набор — ровно то,
    что AC-08 запрещает. Порядок `users → event_cards` обязателен: обратный
    дал бы встречную очередь с `join_open_card`/`select_join_request`
    и дедлок под нагрузкой.
    """
    moment = now if now is not None else datetime.now(UTC)
    repo = repository if repository is not None else EventCardRepository(session)

    # X-6.1-1, ADR-09: строка автора блокируется ПЕРВОЙ, до чтения карточки.
    await repo.lock_author(author_id)

    # X-6.1-1: карточка читается ПОД БЛОКИРОВКОЙ — иначе `filled_count`
    # и `pending` проверяются по снимку, который «join» успевает изменить.
    card = await repo.get_owned(author_id, card_uuid, for_update=True)
    if card is None:
        # AC-07: чужая и несуществующая неразличимы.
        raise CardNotFoundError()
    # AC-01 (FR99, стори 4.6): править можно черновик и опубликованную карточку,
    # ПОКА нет ни одного присоединения. Порядок проверок важен: терминальные
    # состояния отвечают «эта карточка закрыта» (`CARD_NOT_EDITABLE`), а
    # заполненная опубликованная — «уже есть участник» (`CARD_ALREADY_JOINED`).
    # Обратный порядок дал бы `CARD_ALREADY_JOINED` на архивной карточке, где
    # `filled_count` уже не имеет значения, и клиент показал бы неверное действие.
    if card.status in EDITABLE_STATUSES:
        # AC-08 стори 6.1 (OQ-5b.3-3, решение «б»): выход из режима заявок
        # (`capacity 1 → 2..6`) запрещён и при живом `pending`, а не только при
        # `filled_count > 0`. Иначе заявитель, ждавший решения, остался бы
        # с заявкой на карточке, которая уже не собирает заявки: `pending`
        # перестал бы быть осмысленным состоянием (FR76/FR77), а автор
        # получил бы «открытый набор» с чужой заявкой в очереди.
        #
        # Смотрим ТОЛЬКО на `pending` (терминальные `not_selected`/`expired`/
        # `cancelled` места не занимают — ADR-09, NFR29), и ТОЛЬКО на переход
        # ИЗ режима заявок: `PATCH` без смены режима (например, только
        # `title` при `capacity=1` и живой заявке) обязан оставаться
        # возможным, иначе автор не смог бы поправить опечатку.
        leaving_request_mode = (
            card.capacity == CARD_REQUEST_MODE_CAPACITY
            and payload.capacity is not None
            and payload.capacity > CARD_REQUEST_MODE_CAPACITY
        )
        pending_blocks = leaving_request_mode and await JoinRequestRepository(
            session
        ).has_pending_for_card(card_id=card.id)
        if card.filled_count > 0 or pending_blocks:
            # D-4.6-1: «первое присоединение» = `filled_count > 0` до появления
            # `join_requests` (5а). После — условие расширено предикатом
            # `pending` (стори 6.1, AC-08).
            raise CardAlreadyJoinedError()
    elif card.status != EventCardStatus.DRAFT:
        raise CardNotEditableError()

    # AC-3 стори 13.2 (V-2): смена места ИЛИ активности обязана пересобрать
    # заголовок, иначе он останется от прежнего места/активности. Место
    # читается один раз и здесь же, поэтому переменная живёт до блока
    # пересборки ниже.
    changed_place: Any = None
    if payload.place_uuid is not None:
        # AC-02: место проверяется тем же гардом, что при создании.
        await ensure_place_available(
            session, place_uuid=payload.place_uuid, city_slug=card.city_slug
        )
        changed_place = await _resolve_place(
            session, place_uuid=payload.place_uuid, city_slug=card.city_slug
        )
        card.place_id = changed_place.id

    if payload.starts_at is not None:
        # AC-03: проверка «не в прошлом» только для нового значения.
        if payload.starts_at <= moment:
            raise CardStartsAtInPastError()
        # AC-01 стори 12.5: тот же горизонт, что и на создании, — иначе
        # `PATCH` стал бы обходом правила («создать нельзя, сдвинуть можно»).
        # Внутри `if`: правка заголовка без времени не должна требовать `starts_at`.
        if payload.starts_at > moment + timedelta(hours=CARD_MAX_HORIZON_HOURS):
            raise CardStartsAtTooFarError()
        card.starts_at = payload.starts_at

    # Task 4.6: итоговая вместимость — новая, если передана, иначе текущая.
    capacity = payload.capacity if payload.capacity is not None else card.capacity
    # Окно: явно переданное, иначе текущее. `None` от клиента = «не трогать».
    requested_window = payload.decision_window_minutes
    capacity_changed_to_request_mode = (
        capacity == CARD_REQUEST_MODE_CAPACITY and card.capacity != CARD_REQUEST_MODE_CAPACITY
    )
    if capacity_changed_to_request_mode and requested_window is None:
        # Переход 2–6 → 1 без явного окна: текущее окно открытого набора
        # (дефолт конфига) НЕ подходит — режим заявок требует осознанного выбора
        # (FR79: «решу до такого-то часа»). Схема 4.1 отбивает это же на создании.
        #
        # CR 4.2 (найдено тестом 4.8): условие «capacity == 1» без проверки
        # СМЕНЫ режима ломало `PATCH` только `title` у карточки, которая уже
        # была на 1 место: её существующее окно — законное значение, а сервис
        # требовал его заново и отвечал `422` на правку заголовка.
        raise CardDecisionWindowRequiredError()
    if requested_window is None and card.capacity == capacity:
        # CR 4.2, патч 5 (AC-05): режим НЕ менялся — сохраняется текущее окно.
        # Дефект, найденный ревью на исполнении: `PATCH` только `title` у карточки
        # открытого набора с ЯВНЫМ окном 120 минут сбрасывал его на дефолт 2880,
        # потому что `_resolve_decision_window` видел `None` и подставлял
        # настройку. AC-05 требует «меняются только переданные поля», а окно
        # не передавалось. Дефолт допустим ТОЛЬКО при переходе 1 → 2–6
        # (клиентского окна у открытого набора нет — FR70).
        requested_window = card.decision_window_minutes
    window = _resolve_decision_window(
        capacity=capacity, requested=requested_window, settings=settings
    )

    # AC-3 стори 13.2 (V-2, FR130, ADR-26): заголовок следует за активностью и
    # местом. Собирается ПОСЛЕ присвоения `activity` и `place_id` выше, но ДО
    # автопроверки ниже — иначе проверялся бы прежний заголовок.
    #
    # AC-6: для легаси `other` заголовок НЕ пересобирается. Генератор на
    # `Activity.OTHER` бросает `ValueError`, и вызывать его здесь значило бы
    # делать нередактируемыми все старые карточки.
    if changed_place is not None or payload.activity is not None:
        # CR 13.2 H-1 (ВЫСОКИЙ, блокер): активность берётся из `payload`, если
        # передана. Присвоение `card.activity` стоит НИЖЕ (после автопроверки),
        # и чтение `Activity(card.activity)` видело СТАРОЕ значение: `PATCH`
        # `{"activity": "cinema"}` при `coffee` оставлял заголовок «Кофе в кафе».
        #
        # Почему не перенести блок ниже присвоения: автопроверка (:735-753)
        # читает `card.title` и обязана видеть НОВЫЙ заголовок — иначе
        # опубликованная карточка проверялась бы по старому тексту.
        effective_activity = (
            payload.activity if payload.activity is not None else Activity(card.activity)
        )
        if effective_activity is not Activity.OTHER:
            # Место для шаблона: только что выбранное либо текущее по `place_id`.
            # Второй запрос нужен лишь при смене ОДНОЙ активности — при смене
            # места строка уже прочитана гардом.
            title_place = changed_place
            if title_place is None:
                from app.repositories.places import PlaceRepository

                title_place = await PlaceRepository(session).get_by_id(card.place_id)
            if title_place is not None:
                card.title = generate_card_title(
                    activity=effective_activity,
                    place_category=PlaceCategory(title_place.category),
                    place_name=title_place.name,
                )

    # AC-02 (FR101, D-4.6-3): у ОПУБЛИКОВАННОЙ карточки новый текст проверяется
    # ДО присвоения. Неудача — отказ без единой мутации: карточка в ленте
    # остаётся прежней. Вернуть её в `pending` нельзя (непроверенный текст
    # попал бы в ленту, а снятие с ленты сломало бы участников).
    if card.status in EDITABLE_STATUSES:
        candidate_description = (
            payload.description if "description" in payload.model_fields_set else card.description
        )
        candidate = CardContent(
            # AC-7 стори 13.2: `payload.title` не существует — поле удалено из
            # `EventCardUpdate`. Проверяется текущий заголовок карточки, уже
            # пересобранный выше, если менялись место или активность.
            title=card.title,
            description=candidate_description,
            category=str(payload.category) if payload.category is not None else card.category,
        )
        if not evaluate_card_content(candidate, stop_words=settings.card_content_stop_words).passed:
            raise CardEditRequiresReviewError()

    card.capacity = capacity
    card.decision_window_minutes = window
    # AC-7 стори 13.2 (FR130): присвоения `card.title` из `payload` здесь нет —
    # автор заголовок не присылает. Заголовок пересобран выше, если менялись
    # место или активность; в остальных случаях он не трогается.
    # AC-08 (X-19, стори 4.6): различие по ФАКТУ передачи поля, а не по значению.
    # `"description" in model_fields_set` — клиент прислал поле; пустая строка
    # после `normalize_bio` означает «стереть» → `None`. Отсутствие поля или
    # явный `null` — «не трогать» (поведение 4.2).
    if "description" in payload.model_fields_set and payload.description is not None:
        card.description = payload.description or None
    if payload.category is not None:
        card.category = str(payload.category)
    # AC-3 стори 12.4: тип и активность меняются по общему правилу PATCH
    # («не передан = не трогать», см. докстринг `EventCardUpdate`).
    # Валидатор `other` уже отработал на схеме — сюда легаси-значение
    # попасть не может, поэтому повторной проверки нет (один источник).
    if payload.kind is not None:
        card.kind = str(payload.kind)
    if payload.activity is not None:
        card.activity = str(payload.activity)
    # AC-2 стори 12.6 (FR119): рамки меняются по общей PATCH-семантике, но
    # с одним отличием от `kind`/`activity`: «снять рамку» — это ЯВНЫЙ `null`,
    # а не «не передал». Поэтому различие по `model_fields_set`, а не по
    # значению: `payload.age_min is None` не отличает «не трогать» от
    # «стереть». Тот же приём, что у `description` (X-19, стори 4.6).
    #
    # Тип `age_min`/`age_max` — `int | None`, поэтому приведение к `str` не
    # нужно; `gender_filter` — enum, в БД строка (CHECK держит белый список).
    if "gender_filter" in payload.model_fields_set:
        card.gender_filter = (
            str(payload.gender_filter) if payload.gender_filter is not None else None
        )
    if "age_min" in payload.model_fields_set:
        card.age_min = payload.age_min
    if "age_max" in payload.model_fields_set:
        card.age_max = payload.age_max
    # CR 12.6 H-1: правка рамок — «появился шанс на решение».
    #
    # Карточка, у которой рамки отсекли всех, помечена `audience_skipped_at`
    # и выпадает из очереди воркера (иначе повтор каждый прогон + ложный
    # алерт, замер CR H-1). Снятие/смена рамки делает прежнее решение
    # недействительным: те же `pending` могут теперь проходить, поэтому
    # метка снимается — иначе карточка осталась бы вне автовыбора НАВСЕГДА,
    # и автор, расширивший рамки, не получил бы выбора вовсе.
    #
    # Ставится `None` здесь, а не отдельным `UPDATE`: строка карточки уже под
    # `FOR UPDATE` в этой транзакции (ADR-09), второй запрос был бы лишним.
    if (
        "gender_filter" in payload.model_fields_set
        or "age_min" in payload.model_fields_set
        or "age_max" in payload.model_fields_set
    ):
        card.audience_skipped_at = None
    # AC-6.4: схема отбила только случай, когда ОБЕ границы пришли одним
    # запросом. Перевёрнутая пара относительно ТЕКУЩЕГО значения проверяется
    # здесь, на смеси payload и строки: иначе `CHECK` дал бы `IntegrityError`
    # → `500` (класс дефекта 12.5). Тот же код ответа, что у схемы.
    if card.age_min is not None and card.age_max is not None and card.age_min > card.age_max:
        raise CardAgeRangeInvalidError()

    # AC-04, D-4.2-3: у ЧЕРНОВИКА `expires_at` пересчитывается всегда — меняется
    # либо `starts_at`, либо окно, либо и то и другое.
    #
    # CR 4.6, патч 1 (AC-01, ADR-02, NFR14): у ОПУБЛИКОВАННОЙ карточки окно
    # решения — обязательство перед участниками, и правка текста его НЕ
    # продлевает. Дефект, найденный ревью на исполнении: пересчёт от `now`
    # сдвигал `expires_at` вперёд ровно на время, прошедшее с публикации
    # (воспроизведено: карточка, опубликованная сутки назад, получала +24 ч
    # окна от одного `PATCH {"title": ...}`). Участник, рассчитавший «решу до
    # 18:00», получал другую границу без предупреждения.
    #
    # Поэтому для не-черновика берётся МИНИМУМ из текущего значения и нового
    # расчёта: окно можно сократить (например, перенеся встречу раньше), но
    # нельзя растянуть. `min` же держит CHECK `expires_at <= starts_at`, если
    # автор приблизил время встречи.
    computed_expires = _compute_expires_at(
        starts_at=card.starts_at, window_minutes=window, now=moment
    )
    if card.status == EventCardStatus.DRAFT:
        card.expires_at = computed_expires
    else:
        card.expires_at = min(card.expires_at, computed_expires)

    # AC-06 (стори 4.3, FR113, D-4.3-3): правка перезапускает автопроверку.
    # Это снимает `rejected` с исправленного черновика: без перезапуска
    # отклонённая карточка осталась бы отклонённой навсегда, а лимит активных
    # карточек (4.4) наказал бы автора дважды за одну ошибку.
    if card.status == EventCardStatus.DRAFT:
        # AC-06 (4.3, FR113): черновик перезапускает автопроверку — это снимает
        # `rejected` с исправленного текста. У опубликованной карточки проверка
        # уже прошла выше (AC-02) и статус проверки НЕ меняется: `approved`
        # остаётся `approved`, иначе карточка исчезла бы из ленты на время
        # повторной модерации.
        run_auto_check(card, settings=settings, now=moment)

    await session.commit()
    return card


def _raise_publish_conflict(card: EventCard, now: datetime) -> NoReturn:
    """AC-06 (X-21, Task 5): причина отказа CAS по ПЕРЕЧИТАННОЙ карточке.

    `rowcount == 0` означает, что предикат `UPDATE` не совпал, но не говорит,
    ПОЧЕМУ. Повторное чтение даёт актуальное состояние, и ветви идут от самого
    информирующего к общему (тот же приём, что `CARD_REJECTED` vs
    `VERIFICATION_PENDING` в 4.2):

    1. `status != draft` — карточку уже опубликовали/закрыли (AC-06):
       `CardNotDraftError`, `409`;
    2. `verification == rejected` — модератор отклонил между проверкой и
       `UPDATE`: `CardRejectedError` с причиной (FR113);
    3. `verification != approved` — вернулась в проверку: `CardVerificationPendingError`;
    4. `starts_at <= now` — время встречи прошло: `CardStartsAtInPastError`;
    5. иначе — `CardNotDraftError` (недостижимо, но без «голого» `500`).

    Порядок важен: отклонённая карточка тоже может иметь `status=draft`,
    и проверка статуса первой дала бы `CARD_NOT_DRAFT` вместо причины отказа
    модератора.
    """
    if card.status != EventCardStatus.DRAFT:
        raise CardNotDraftError()
    if card.verification_status == CardVerificationStatus.REJECTED:
        raise CardRejectedError(card.rejection_reason)
    if card.verification_status != CardVerificationStatus.APPROVED:
        raise CardVerificationPendingError()
    if card.starts_at <= now:
        raise CardStartsAtInPastError()
    raise CardNotDraftError()


async def publish_card(
    session: AsyncSession,
    *,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    settings: CardSettings,
    now: datetime | None = None,
    repository: EventCardRepository | None = None,
) -> EventCard:
    """AC-01, AC-03, AC-04, AC-06, AC-07: `draft → published` под лимитом.

    Порядок (Task 6, ADR-09) — важен каждый шаг:

    1. `SELECT users.id ... FOR UPDATE` ПЕРВЫМ: точка сериализации лимита.
       Все публикации автора встают в очередь здесь, поэтому `COUNT` ниже
       видит уже зафиксированный результат предыдущей;
    2. `get_owned` с `populate_existing=True` — актуальное состояние карточки;
    3. быстрые проверки состояния (`draft` → `rejected` → `approved` →
       `starts_at`): отказ по лимиту НЕ должен маскировать настоящую причину.
       Черновик, который и так не публикуется, не получает `429` — иначе
       клиент пошёл бы «освобождать слот», хотя проблема в другом;
    4. `COUNT` активных ПОД блокировкой → `>= limit` → `CardLimitReachedError`;
    5. `expires_at` через `_compute_expires_at` (D-4.2-3, одна формула);
    6. CAS `publish_if_draft`; `rowcount == 0` → перечитать → резолвер;
    7. `commit`.

    Без блокировки `users` обе транзакции видели бы `count = limit - 1`
    и публиковали две карточки (AC-03). Без CAS проигравший перезаписал бы
    `published_at` первого (AC-06).
    """
    moment = now if now is not None else datetime.now(UTC)
    repo = repository if repository is not None else EventCardRepository(session)

    # AC-04, ADR-09: строка автора блокируется ПЕРВОЙ, до любого чтения
    # `event_cards`. Обратный порядок (`cards` раньше `users`) — дедлок
    # с путями, которые блокируют `users` первыми (лайки, фото).
    await repo.lock_author(author_id)

    card = await repo.get_owned(author_id, card_uuid)
    if card is None:
        raise CardNotFoundError()
    if card.status != EventCardStatus.DRAFT:
        # AC-06: повторная публикация — конфликт состояния, не успех:
        # второй `published_at` исказил бы историю.
        raise CardNotDraftError()

    if card.verification_status == CardVerificationStatus.REJECTED:
        # FR113: причина обязательна — клиент показывает «Исправить».
        raise CardRejectedError(card.rejection_reason)
    if card.verification_status != CardVerificationStatus.APPROVED:
        # FR101: непроверенный текст не публикуется (OQ-1 закрыт вариантом a).
        raise CardVerificationPendingError()

    # CR 4.2, патч 3 (AC-06 + FR73): черновик, пролежавший до начала встречи,
    # публиковать нельзя — иначе `expires_at` оказался бы в прошлом.
    # Проверка ДО `COUNT` намеренно: прошедшая встреча не занимает слот и не
    # должна съедать чужой лимит (Task 6, Dev Notes).
    if card.starts_at <= moment:
        raise CardStartsAtInPastError()

    # AC-09 (X-22, стори 4.6): публикация при заполненной вместимости — `409`,
    # а не `500` от CHECK. Сейчас `filled_count` всегда 0 (присоединений нет),
    # но путь доступен из админского скрипта и станет штатным в 5.x, когда
    # заявки начнут занимать места. Проверка стоит ДО подсчёта лимита: карточка,
    # которую нельзя опубликовать по существу, не должна тратить слот и
    # маскировать настоящую причину кодом `429`.
    if card.filled_count >= card.capacity:
        raise CardNoSeatsError()

    # AC-01, AC-03: подсчёт ПОД блокировкой автора. Индекс ускоряет запрос,
    # но гонку закрывает именно `FOR UPDATE` (ADR-09).
    active_count = await repo.count_active(author_id)
    if active_count >= settings.card_active_limit:
        raise CardLimitReachedError(limit=settings.card_active_limit, active_count=active_count)

    expires_at = _compute_expires_at(
        starts_at=card.starts_at,
        window_minutes=card.decision_window_minutes,
        now=moment,
    )

    # AC-06 (X-21): условная запись. `FOR UPDATE` на карточке не берём:
    # это нарушило бы порядок `users → cards` (ADR-09).
    updated = await repo.publish_if_draft(
        card_id=card.id,
        author_id=author_id,
        now=moment,
        expires_at=expires_at,
    )
    if updated == 0:
        # Проигравший CAS: состояние изменилось между проверкой и записью.
        # Перечитываем и отдаём ТОЧНУЮ причину (Task 5).
        fresh = await repo.get_owned(author_id, card_uuid)
        if fresh is None:
            raise CardNotFoundError()
        _raise_publish_conflict(fresh, moment)

    # `card` в identity map осталась со старыми значениями: CAS шёл мимо ORM
    # (`synchronize_session=False`). Перечитываем, чтобы вернуть актуальный
    # объект вызывающему коду и не отдать клиенту `draft` после публикации.
    published = await repo.get_owned(author_id, card_uuid)
    if published is None:
        raise CardNotFoundError()

    # ADR-11: WS и уведомления публикуются ПОСЛЕ commit и реализуются в 4.3/7.x.
    await session.commit()
    return published


# ============================================================================
# Лента карточек (стори 4.5, Task 4; AC-01…AC-08)
# ============================================================================

# AC-08 (D-4.5-10): размер страницы зафиксирован. §7.2 не содержит `limit`
# в контракте, а клиентский параметр дал бы второй источник правды (размер
# первой страницы для LCP) и ветку «слишком большой `limit`», которую никто
# не тестирует. Тот же выбор сделан для `PLACES_PAGE_SIZE` (3.3).
CARDS_PAGE_SIZE = 20

# AC-05 (стори 4.6, NFR20): размер батча воркера `card_windows`. Ограничение
# обязательно: без него прогон выбирал бы все просроченные карточки сразу,
# удерживал блокировки (`FOR UPDATE SKIP LOCKED`) до конца и рос в памяти.
# 200 — на порядок больше ожидаемого числа начавшихся встреч в минуту, поэтому
# обычный прогон забирает всё за один заход, а пик не превращается в вечность.
CARDS_WORKER_BATCH = 200


@dataclass(frozen=True)
class CardFeedPlace:
    """AC-08 стори 5а.4 (X-36): место карточки, когда оно АКТИВНО.

    Отдельный объект, а не три поля с `| None` у каждого: скрытое место
    означает «места нет целиком», и три независимых `None` допускали бы
    невозможное состояние «uuid есть, имени нет». Схема получает либо объект,
    либо `None` — третьего не дано.
    """

    uuid: uuid_lib.UUID
    name: str
    address_text: str


@dataclass(frozen=True)
class CardFeedItem:
    """AC-06: одна карточка ленты — доменный объект, не Pydantic-схема.

    §6 project-context: сервис не знает про HTTP-форму ответа; роутер делает
    `EventCardRead.model_validate(item)`. Если бы сервис возвращал схему,
    второе представление ресурса (мобильный клиент) потребовало бы правки
    сервиса, а не только схемы.

    Поля — плоские, включая распакованные из JOIN'ов (`author_uuid`,
    `place_name`). Схема собирает из них вложенные объекты; держать
    вложенность здесь значило бы повторить структуру ответа в двух слоях.
    """

    # ORM-объект карточки: схема читает из него `uuid`, `status`, `created_at`
    # и остальные поля напрямую (`from_attributes`), не через копию.
    card: EventCard
    # AC-04 стори 5б.1: серверный якорь отсчёта — ОДИН `moment` на ответ,
    # тот же, по которому выполнялись ленивые переходы. Второй `datetime.now()`
    # в роутере дал бы расхождение с `expires_at` на время запроса.
    server_time: datetime
    author_uuid: uuid_lib.UUID
    author_name: str | None
    author_age: int | None
    # AC-08 стори 5а.4 (X-36): `None` — место скрыто модератором. Три поля
    # с `| None` допускали бы невозможное «uuid есть, имени нет», поэтому
    # объект либо есть целиком, либо его нет (см. `CardFeedPlace`).
    place: CardFeedPlace | None
    # AC-09 стори 5б.2 (D-5b.2-7, FR104): признак ЗРИТЕЛЯ — он в составе
    # (`selected`). Заполняется сервисом, а не схемой: значение приходит из
    # `join_requests` и требует запроса. Дефолт `False` — наблюдатель, самое
    # частое состояние; `list_cards` перезаписывает его пакетно, `get_card` —
    # точечно. Поле НЕ участвует в безличности (AC-03): проигравший и
    # наблюдатель оба получают `false`, различие было бы утечкой `not_selected`.
    viewer_participates: bool = False
    # AC-2, AC-5 стори 11.5 (FR82): «моя заявка» — признак ЗРИТЕЛЯ для карточки
    # режима заявок. Считается из `join_requests` (`status='pending'`), поэтому
    # живёт в доменном объекте рядом с `viewer_participates`, а не в схеме:
    # значение требует запроса. `list_cards` заполняет пакетно, `get_card` —
    # точечно.
    #
    # `False`/`None` — легальные состояния (наблюдатель, автор, `selected`),
    # а не «неизвестно»: третье значение заставило бы клиент ветвиться.
    viewer_requested: bool = False
    # AC-5 (FR82): `uuid` заявки — адрес отмены (`DELETE /requests/{uuid}`).
    # Пусто, когда заявки нет: «нечего отменять» выражено отсутствием адреса.
    viewer_request_uuid: uuid_lib.UUID | None = None


@dataclass(frozen=True)
class CardFeedPage:
    """AC-07, AC-08: страница ленты — `items`, `total`, непрозрачный `cursor`.

    `total` — «сколько всего под фильтром» (D-4.5-9), не «сколько осталось».
    Расхождение с `len(items)` в `READ COMMITTED` допустимо (AC-07):
    параллельная публикация между двумя `execute` меняет счётчик, но не
    границы страниц.

    `next_cursor` — `None`, если следующей страницы нет. Собирается ТОЛЬКО
    при наличии лишней строки (`len(rows) > limit`) и от последней УЖЕ
    усечённой строки: курсор от выброшенной сдвинул бы границу и потерял бы
    одну карточку.
    """

    items: list[CardFeedItem]
    total: int
    next_cursor: str | None


def _feed_item_from_row(row: Any, *, server_time: datetime) -> CardFeedItem:
    """AC-06: распаковывает строку JOIN'а в доменный объект.

    `row` — SQLAlchemy `Row` с восемью позициями: карточка, `author_uuid`,
    `author_name`, `author_age`, `place_uuid`, `place_name`,
    `place_address_text`. Порядок задан билдером запроса и общий для ленты
    и детального экрана (`build_get_visible_statement`), поэтому распаковка
    одна: две копии разошлись бы при первом добавлении поля.

    `place_name`/`place_address_text` берутся из `LEFT JOIN places`, но
    карточка без места невозможна (FK `NOT NULL`): `None` здесь означал бы
    нарушенный инвариант, и молчаливая подстановка пустой строки скрыла бы
    его. `Place` физически удалить нельзя (`ON DELETE RESTRICT`), скрытие —
    `is_active=false`, строка остаётся.
    """
    (
        card,
        author_uuid,
        author_name,
        author_age,
        author_birth_date,
        place_uuid,
        place_name,
        place_address_text,
        place_is_active,
    ) = row
    # AC-08 стори 5а.4 (X-36): скрытое место (`is_active=false`) в ответ не
    # попадает вовсе. `None`, а не объект с пустыми строками: клиент обязан
    # различать «места нет» и «место без адреса», а подстановка `""` скрыла бы
    # действие модератора (project-context §4: `null`, не `""`).
    place: CardFeedPlace | None = (
        CardFeedPlace(uuid=place_uuid, name=place_name, address_text=place_address_text)
        if place_is_active
        else None
    )
    return CardFeedItem(
        card=card,
        server_time=server_time,
        author_uuid=author_uuid,
        author_name=author_name,
        # Стори 12.2, AC-3: наружу — вычисленный возраст. `birth_date`
        # приоритетнее legacy-`age`: у новых учёток колонка `age` — `NULL`.
        author_age=compute_age(author_birth_date, author_age, today_or_now()),
        place=place,
    )


async def list_cards(
    session: AsyncSession,
    *,
    category: PlaceCategory | None = None,
    has_seats: bool = False,
    cursor: str | None = None,
    kind: CardKind | None = None,
    activity: Activity | None = None,
    limit: int = CARDS_PAGE_SIZE,
    settings: CardSettings,
    viewer_city_slug: str,
    viewer_id: int | None = None,
    now: datetime | None = None,
) -> CardFeedPage:
    """AC-01, AC-04, AC-05, AC-07, AC-08: страница публичной ленты сообщества.

    Порядок шагов намеренный (тот же, что в `list_places` 3.3):

    1. Слаг берётся из `viewer_city_slug` — `users.city_slug`
       зрителя (AC-05 стори 10.1, ADR-01/ADR-13): параметра в контракте нет,
       и клиент не может запросить чужое сообщество. До 10.1 значение бралось
       из конфига; теперь ось показа — сообщество пользователя;
    2. курсор декодируется ДО запроса: битое значение обязано стоить ноль
       запросов к БД (`422 invalid_cursor`, а не `500`);
    3. страница запрашивается с `limit + 1` строкой — так «есть ли следующая»
       определяется одним запросом, без отдельного `COUNT` с курсором;
    4. `total` считается последним: он не влияет на `items`, а его расхождение
       с страницей допустимо (AC-07).

    `now` — параметр для тестов: реальное время подставляется один раз, и
    граница `starts_at >= now` не «плывёт» между страницей и `COUNT`.
    """
    city_slug = viewer_city_slug
    moment = now if now is not None else datetime.now(UTC)

    after: tuple[datetime, uuid_lib.UUID] | None = None
    if cursor is not None:
        # AC-08: разбор бросает `InvalidCursorError` (класс из `place_cursor`,
        # не копия) — роутер отображает его в `422 invalid_cursor` той же
        # веткой, что выдача мест.
        after = decode_feed_cursor(cursor)

    # AC-05: категория приходит уже типизированной `PlaceCategory` из роутера;
    # сравнение идёт со строкой (`StrEnum` — подкласс `str`). `None` — без
    # фильтра, а не «все категории одним списком»: пустая выдача и отсутствие
    # фильтра обязаны различаться (AC-09, `EmptyState`).
    category_value: str | None = category if category is not None else None
    # AC-5 стори 12.4: `StrEnum` — подкласс `str`, поэтому сравнение идёт
    # со значением колонки напрямую. `None` — «фильтра нет», а не «все
    # значения списком»: пустая выдача и отсутствие фильтра различаются
    # (AC-09, `EmptyState`).
    kind_value: str | None = str(kind) if kind is not None else None
    activity_value: str | None = str(activity) if activity is not None else None

    repo = EventCardRepository(session)
    rows, total = await repo.list_feed_page(
        city_slug=city_slug,
        now=moment,
        category=category_value,
        has_seats=has_seats,
        kind=kind_value,
        activity=activity_value,
        after=after,
        # AC-08: лишняя строка — признак следующей страницы, а не часть выдачи.
        limit=limit + 1,
    )

    has_next = len(rows) > limit
    page_rows = rows[:limit]
    items = [_feed_item_from_row(row, server_time=moment) for row in page_rows]

    # AC-09 стори 5б.2 (D-5b.2-7, FR104): признак участия зрителя — ОДНИМ
    # пакетным запросом на всю страницу. N+1 недопустим: 20 карточек дали бы
    # 20 дополнительных `SELECT` на самый частый GET продукта (тот же довод,
    # что у подписи фото в 5а.3). Запрос идёт ПОСЛЕ сборки `items`, потому что
    # ему нужны `card.id` уже отобранных строк — иначе пришлось бы читать
    # карточки дважды.
    if items and viewer_id is not None:
        # AC-09 5б.2 + AC-2 11.5: ОБА признака зрителя — ОДНИМ запросом.
        # Раздельные `SELECT` нарушали контракт анти-оракула
        # (`test_oracle_exact_query_count`): число SQL на `GET` зафиксировано,
        # и лишний запрос краснит тест даже при верном JSON.
        #
        # N+1 недопустим: 20 карточек дали бы 20 запросов на самый частый GET.
        # Предикаты разные по смыслу: `selected` — участие (место занято),
        # `pending` — «моя заявка» (место НЕ занято, до решения автора зритель
        # наблюдатель). Сливать их в один флаг нельзя.
        state = await JoinRequestRepository(session).viewer_request_state(
            applicant_id=viewer_id, card_ids=[item.card.id for item in items]
        )
        items = [
            replace(
                item,
                viewer_participates=item.card.id in state.participating,
                viewer_requested=item.card.id in state.pending,
                viewer_request_uuid=state.pending.get(item.card.id),
            )
            for item in items
        ]

    next_cursor: str | None = None
    if has_next and page_rows:
        # AC-08: курсор от последней УЖЕ усечённой строки. Курсор от выброшенной
        # (двадцать первой) сдвинул бы границу на одну карточку — она пропала бы.
        last_item = items[-1]
        next_cursor = encode_feed_cursor(last_item.card.starts_at, last_item.card.uuid)

    # commit() не вызывается: чтение (D-4 стори 2.2). `populate_existing`
    # в репозитории тоже нет — объекты из выдачи не правятся.
    return CardFeedPage(items=items, total=total, next_cursor=next_cursor)


async def get_card(
    session: AsyncSession,
    *,
    card_uuid: uuid_lib.UUID,
    viewer_id: int,
    viewer_city_slug: str,
    settings: CardSettings,
    now: datetime | None = None,
) -> CardFeedItem:
    """AC-02, AC-03: карточка по `uuid` с ленивым закрытием окна.

    Порядок проверок намеренный (Dev Notes стори):

    1. **Видимость (AC-02)** — до любых мутаций: `draft` чужого автора даёт
       `404`, а не «закрываем окно у невидимой карточки». Иначе ленивое
       закрытие стало бы способом менять чужие черновики по перебору `uuid`;
    2. **Ленивое закрытие окна (AC-03, ADR-03)** — CAS; `rowcount == 0`
       не ошибка (окно закрыли параллельно: воркер или другой запрос);
    3. **Перечитывание** — после `UPDATE` мимо ORM объект в identity map
       держит старый `status`, и клиент получил бы `published` на ответ, где
       в БД уже `closed` (урок CR 2.1, патч 11).

    **Видимость (§7.2, D-4.5-6):** не-автору доступны все статусы, КРОМЕ
    `draft`. `finished`/`cancelled` — история (FR73): «карточка остаётся
    доступной участникам», и скрывать её от перехода по старой ссылке нельзя.

    **Чужое сообщество — `404`, не `403`** (ADR-13): `403` отличил бы
    «существует в другом городе» от «нет такой», то есть раскрыл бы
    существование карточки, которую участник не должен видеть.
    """
    city_slug = viewer_city_slug
    moment = now if now is not None else datetime.now(UTC)

    repo = EventCardRepository(session)
    row = await repo.get_visible_card(card_uuid=card_uuid, city_slug=city_slug)
    if row is None:
        # AC-02: нет карточки ИЛИ она в другом сообществе — ответ один.
        raise CardNotFoundError()

    item = _feed_item_from_row(row, server_time=moment)
    card = item.card

    # AC-02, D-4.5-6: черновик видит только автор. Проверка в Python, а не
    # в SQL: `author_id` в условии запроса скрыл бы карточку от участника
    # ленты, а ему нужны `published`/`full`/`finished` — то есть фильтр
    # «свой ИЛИ не draft» выражается только сравнением после выборки.
    if card.status == EventCardStatus.DRAFT and card.author_id != viewer_id:
        raise CardNotFoundError()

    # AC-03, ADR-03: ленивое закрытие окна. CAS; повторный вызов обновит
    # 0 строк и останется `closed` — идемпотентность бесплатна, потому что
    # сам статус и есть флаг.
    # CR 4.5, патч 1: коммит ТОЛЬКО при фактической мутации. До правки
    # `session.commit()` стоял безусловно в конце функции, то есть КАЖДЫЙ
    # просмотр карточки (а это самый частый GET продукта) открывал и
    # фиксировал транзакцию записи: лишний WAL, лишний round-trip, удержание
    # соединения из пула. При этом в 99% случаев мутации нет — окно ещё
    # открыто, карточка не `published` или уже закрыта. Проверка «была ли
    # мутация» нужна именно здесь: перенос `commit` в роутер потребовал бы
    # вернуть вызывающему коду флаг, то есть второй источник правды.
    mutated = False
    # AC-08 стори 6.2 (TD п.6, ADR-13; CR-2, blind #7): `chat_uuid`, открытый
    # ленивым автовыбором. Лог пишется ПОСЛЕ `commit` — иначе откат оставил бы
    # в журнале «чат открыт» для чата, которого нет.
    opened_chat_uuid: uuid_lib.UUID | None = None

    async def _reread_current() -> CardFeedItem:
        """AC-03, AC-05: перечитывание после CAS мимо ORM (`synchronize_session=False`).

        Одна функция на два перехода: закрытие окна и архив. Две копии
        разошлись бы при первом добавлении третьего перехода, и клиент
        получил бы статус из identity map вместо строки БД (урок CR 2.1).
        """
        nonlocal mutated
        mutated = True
        fresh_row = await repo.get_visible_card(card_uuid=card_uuid, city_slug=city_slug)
        if fresh_row is None:
            # Карточку удалили между чтением и мутацией — невозможно
            # (физического удаления нет, R-04), но честный `404` лучше
            # `AttributeError` на `None`.
            raise CardNotFoundError()
        # AC-04 стори 5б.1: якорь тот же `moment`, что у переходов, — иначе
        # клиент получил бы `server_time` позже `expires_at`, по которому уже
        # принято решение о закрытии.
        return _feed_item_from_row(fresh_row, server_time=moment)

    if card.status == EventCardStatus.PUBLISHED and card.expires_at <= moment:
        # AC-06 стори 5б.3 (D-5b.3-3): ТО ЖЕ ядро, что у воркера. До правки
        # здесь стоял `close_if_due` — чистый CAS `published -> closed` без
        # анализа заявок: наблюдатель, открывший карточку первым, закрыл бы
        # набор с живыми `pending`, и автовыбор не состоялся бы никогда
        # (FR80, AC-04). Исход зависел бы от трафика, а «≤5 мин» (NFR21)
        # выполнялось бы только при работающем воркере.
        decision = await close_due_card(
            session, card_id=card.id, author_id=card.author_id, now=moment
        )
        if decision.outcome is DecisionOutcome.SELECTED and decision.chat_uuid is not None:
            # AC-08 стори 6.2 (TD п.6, ADR-13; CR-2, blind #7): `chat.opened`
            # пишет владелец транзакции ПОСЛЕ `commit` (ниже по функции).
            # Здесь только фиксируется факт; запись выполняется после
            # `await session.commit()`, иначе откат оставил бы в журнале
            # «чат открыт» для чата, которого нет. `applicant_id` не пишется
            # никогда — выбор делает система (FR81).
            opened_chat_uuid = decision.chat_uuid
        if decision.outcome in (
            DecisionOutcome.SELECTED,
            DecisionOutcome.CLOSED_NO_CHOICE,
        ):
            item = await _reread_current()
            card = item.card

    if card.status in ARCHIVABLE_STATUSES and card.starts_at <= moment:
        # AC-05 (FR73, NFR21, D-4.6-6): ленивый архив по наступлении
        # `starts_at`. Тот же аргумент, что у окна: воркер `card_windows`
        # может стоять, а карточка обязана исчезнуть из ленты и стать
        # историей в момент, когда встреча началась, — не «когда-нибудь».
        # Порядок после закрытия намеренный: `closed` тоже архивируется,
        # поэтому карточка с истёкшим окном проходит ДВА CAS за один `GET`.
        await repo.finish_card(card_id=card.id, now=moment)
        item = await _reread_current()

    if mutated:
        # ADR-11: WS-событие `card.closed` публикуется после commit и
        # относится к 6.x/7.x (долг X-32).
        await session.commit()

    if opened_chat_uuid is not None:
        # Стори 9.2 (Task 4.4, AC-2/AC-3, ADR-11): уведомления — ПОСЛЕ
        # `commit` (блок `if mutated` выше). Четвёртый путь сборки: пропуск
        # его оставил бы FR45 невыполненным ровно для того, кто открыл
        # карточку первым (дефект класса X-6.5-1).
        #
        # Адресат `request.selected` читается по `card_id`: `DecisionResult`
        # ядра отдаёт только `chat_uuid`, а менять его нельзя — ядро зовут три
        # владельца транзакции (Task 4.3).
        #
        # `CLOSED_NO_CHOICE` сюда не доходит: `opened_chat_uuid` заполняется
        # только при `SELECTED` (AC-4).
        # **CR 9.2 r1, HIGH-1: ВЕСЬ блок в `try/except`** (D-9.2-12). До фикса
        # чтение адресата стояло вне защиты: близ БД после успешного `commit`
        # давал `500` тому, кто просто открыл карточку, — при уже собранной
        # компании и заполненном `chat_uuid`. Повторить `GET` бессмысленно:
        # карточка `full`, автовыбор больше не сработает.
        try:
            applicant = await JoinRequestRepository(session).get_selected_applicant(card_id=card.id)
            if applicant is not None:
                applicant_uuid, applicant_id = applicant
                await notify_request_selected(
                    session,
                    applicant_uuid=applicant_uuid,
                    applicant_id=applicant_id,
                    card_uuid=card_uuid,
                    chat_uuid=opened_chat_uuid,
                )
            author = await UserRepository(session).get_by_id(card.author_id)
            if author is not None:
                await notify_company_assembled(
                    session,
                    author_uuid=author.uuid,
                    author_id=author.id,
                    card_uuid=card_uuid,
                    chat_uuid=opened_chat_uuid,
                )
        except Exception as exc:  # noqa: BLE001 — домен закоммичен (D-9.2-12)
            # Без `card.author_id`: безличность FR81 действует и на ветке
            # ошибки — `card_uuid` уже общий ресурс, личность не нужна.
            logger.error(
                "notification.create.failed",
                card_uuid=str(card_uuid),
                error=type(exc).__name__,
            )

        # AC-08 стори 6.2 (TD п.6, ADR-13; CR-2): запись ТОЛЬКО после
        # успешного `commit`. В журнале `card_uuid` и `chat_uuid`, никаких
        # идентификаторов заявителей/автора — FR81 (безличность).
        logger.info(
            "chat.opened",
            card_uuid=str(card_uuid),
            chat_uuid=str(opened_chat_uuid),
        )

    # AC-09 стори 5б.2 (D-5b.2-7, FR104): признак участия зрителя — точечный
    # запрос на одну карточку. Пакетный приём нужен ленте (20 карточек); здесь
    # второго `SELECT` на страницу нет, и «оптимизировать» его нечем.
    # AC-09 5б.2 + AC-2 11.5: оба признака зрителя — ОДНИМ запросом.
    # Число SQL на `GET /cards/{uuid}` зафиксировано контрактом анти-оракула
    # (`test_oracle_exact_query_count`): два раздельных `SELECT` краснили бы
    # его при верном JSON. Тот же метод, что в ленте, — иначе состояние
    # карточки в ленте разошлось бы с её же экраном.
    state = await JoinRequestRepository(session).viewer_request_state(
        applicant_id=viewer_id, card_ids=[item.card.id]
    )
    item = replace(
        item,
        viewer_participates=item.card.id in state.participating,
        viewer_requested=item.card.id in state.pending,
        viewer_request_uuid=state.pending.get(item.card.id),
    )
    return item


# AC-4, AC-4a, AC-4b стори 12.7 (FR120, П11, ADR-22): статусы, в которых
# анкета автора ДОСТУПНА через карточку. `closed` входит намеренно (D-12.7-3,
# FR74): набор закрыт вручную, но встреча состоится, и анкета нужна, чтобы
# прийти на неё. Без этой половины «всегда 404» прошло бы AC-4 незамеченным
# (P-21: пара обязательна).
AUTHOR_PROFILE_VISIBLE_STATUSES: tuple[EventCardStatus, ...] = (
    EventCardStatus.PUBLISHED,
    EventCardStatus.FULL,
    EventCardStatus.CLOSED,
    # AC-4c, D-12.7-3: черновик. Для постороннего он уже невидим (`get_card`
    # отдаёт `CardNotFoundError`), а АВТОР обязан видеть свою анкету на своём
    # черновике — иначе получил бы `404` от собственного профиля, поведение,
    # которого никто не проектировал.
    EventCardStatus.DRAFT,
)


def _author_photo_ttl(*, starts_at: datetime, now: datetime, settings: CardSettings) -> int:
    """AC-5, D-12.7-4: TTL подписи не переживает карточку.

    `max(1, ...)` — не косметика (R3): воркер архивации может отстать, и
    `starts_at` окажется в прошлом. Отрицательный `ExpiresIn` ушёл бы в
    `boto3` и дал бы `500` вместо `404`/короткой ссылки. Верхняя граница —
    общий TTL хранилища: удлинять подпись сверх принятого в продукте
    (`NFR23`) нельзя, даже если встреча через неделю.

    Значение ОДНО для `ExpiresIn` подписи и для `expires_in` в ответе
    (D-12.7-4): две формулы разошлись бы при первой правке.
    """
    remaining = int((starts_at - now).total_seconds())
    return max(1, min(settings.s3_signed_url_ttl, remaining))


async def get_card_author_profile(
    session: AsyncSession,
    *,
    card_uuid: uuid_lib.UUID,
    viewer_id: int,
    viewer_city_slug: str,
    settings: CardSettings,
    now: datetime | None = None,
) -> EventCardAuthorProfile:
    """AC-4, AC-4a, AC-4b, AC-4c, AC-5, AC-2: анкета автора в контексте карточки.

    ЕДИНСТВЕННАЯ точка чтения чужой анкеты (D-12.7-1, ADR-22): постоянной
    витрины `/profiles/{uuid}` нет, а `GET /cards/{uuid}` на архиве обязан
    оставаться `200` (FR73, замок `test_cards_state_api.py:492-512`). Поэтому
    анкета — под-маршрут карточки, и её видимость равна видимости карточки.

    Порядок шагов — контракт:

    1. `get_card` (4.5): срез сообщества, `draft` чужого автора → `404`,
       ленивые переходы окна и архива. Второй запрос видимости не заводится;
    2. **статус ДО анкеты и ДО подписи** (Task 2.3, U15): на архиве не должно
       быть ни запроса к фото, ни вызова `get_signed_url`. Подпись — внешний
       эффект, и «сначала подписали, потом отказали» тратило бы ресурс на
       каждый перебор;
    3. анкета и главное одобренное фото — по одному запросу;
    4. сборка: `age` ТОЛЬКО `compute_age` (AC-2, 12.2), `birth_date` наружу
       не уходит никогда.

    **Три причины — одно тело** (D-12.7-2, ADR-13): нет карточки · чужой
    город · архив дают `CardNotFoundError` → `404 card_not_found`. Нового
    знания ответ не даёт: статус архивной карточки участник и так видит в
    `GET /cards/{uuid}`.

    **Анти-оракул по числу запросов** (D-12.7-2, приём CR 6.3 F-1):
    `get_card` на несуществующей карточке выходит раньше, чем считает
    `viewer_request_state`, поэтому путей с разной стоимостью было бы два.
    Выравнивание — тем же запросом с заведомо несуществующим `card_id`
    (`_MISSING_CARD_ID`): результат заведомо пуст и не проверяется, задача —
    не дать времени ответа зависеть от существования карточки.
    """
    moment = now if now is not None else datetime.now(UTC)

    try:
        item = await get_card(
            session,
            card_uuid=card_uuid,
            viewer_id=viewer_id,
            viewer_city_slug=viewer_city_slug,
            settings=settings,
            now=moment,
        )
    except CardNotFoundError:
        # D-12.7-2: «нет карточки», «чужой город» и «чужой черновик» выходят
        # здесь. Выравнивание до стоимости пути с найденной карточкой:
        # тот же запрос признаков зрителя, заведомо пустой.
        await JoinRequestRepository(session).viewer_request_state(
            applicant_id=viewer_id, card_ids=[_MISSING_CARD_ID]
        )
        raise

    # AC-4, AC-4a: архив → 404. `finished`/`cancelled` — терминальные
    # состояния (FR73): встреча прошла или отменена, анкета больше не нужна.
    # Проверка ДО чтения анкеты и ДО подписи (Task 2.3, U15).
    if item.card.status not in AUTHOR_PROFILE_VISIBLE_STATUSES:
        raise CardNotFoundError()

    profile = await ProfileRepository(session).get_by_user_id(item.card.author_id)
    storage_keys = await ProfilePhotoRepository(session).list_primary_approved_by_user_ids(
        [item.card.author_id]
    )

    # AC-2, 12.3: возраст ТОЛЬКО вычисленный. `birth_date` приоритетнее
    # legacy-`age`: у новых учёток колонка `age` — `NULL`.
    age = None if profile is None else compute_age(profile.birth_date, profile.age, today_or_now())

    # AC-5: TTL считается ОДИН раз и уходит и в подпись, и в ответ.
    ttl = _author_photo_ttl(starts_at=item.card.starts_at, now=moment, settings=settings)
    storage_key = storage_keys.get(item.card.author_id)
    photo_url: str | None = None
    if storage_key is not None:
        try:
            photo_url = get_signed_url(storage_key, ttl=ttl)
        except (MediaStorageError, MediaUnavailableError):
            # Приём `_sign_photos` (5а.4): сбой хранилища не валит экран.
            # URL не логируется никогда (D-11) — только факт сбоя.
            logger.warning("card_author_profile.photo.sign_failed", user_id=item.card.author_id)

    return EventCardAuthorProfile(
        uuid=item.author_uuid,
        # Имя: сначала из JOIN'а `get_card` (`author_name`), затем из анкеты.
        # Оба источника допустимы: `LEFT JOIN profiles` в карточке уже
        # посчитан, и второй запрос нужен только ради `city`/`bio`.
        name=(
            item.author_name
            if item.author_name is not None
            else (profile.name if profile is not None else None)
        ),
        age=age if age is not None else item.author_age,
        city=None if profile is None else profile.city,
        bio=None if profile is None else profile.bio,
        # AC-5, Task 3.3 (D-5a.4-6): ссылка и срок — вместе или никак.
        # `expires_in` при `photo_url is None` был бы обещанием ссылки,
        # которой нет.
        photo_url=photo_url,
        photo_expires_in=ttl if photo_url is not None else None,
    )


async def close_card(
    session: AsyncSession,
    *,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    now: datetime | None = None,
    repository: EventCardRepository | None = None,
) -> EventCard:
    """AC-03 (FR74): автор закрывает набор вручную — `published → closed`.

    Порядок: найти свою карточку → CAS → при 0 строк объяснить причину по
    перечитанной строке. Проверка статуса в Python НЕ заменяет CAS: между
    проверкой и `UPDATE` карточку могли закрыть/отменить, и два `200` на один
    переход сломали бы контракт «`closed_at` выставляется ровно один раз».

    `close` на `full` запрещён (D-4.6-4): мест нет, набор уже остановлен
    автоматикой, и `closed` исказил бы причину остановки.
    """
    moment = now if now is not None else datetime.now(UTC)
    repo = repository if repository is not None else EventCardRepository(session)

    card = await repo.get_owned(author_id, card_uuid)
    if card is None:
        # ADR-13: чужая и несуществующая неразличимы.
        raise CardNotFoundError()

    updated = await repo.close_by_author(card_id=card.id, author_id=author_id, now=moment)
    if updated == 0:
        # Проигравший CAS: состояние изменилось между чтением и записью.
        fresh = await repo.get_owned(author_id, card_uuid)
        if fresh is None:
            raise CardNotFoundError()
        # Ветка одна: и «уже закрыта», и «отменена», и «full» требуют от
        # клиента одного — перечитать карточку. Отдельные коды на каждую
        # причину раздули бы контракт без пользы (`CARD_NOT_PUBLISHED`
        # достаточно: действие «закрыть» неприменимо).
        raise CardNotPublishedError()

    closed = await repo.get_owned(author_id, card_uuid)
    if closed is None:
        raise CardNotFoundError()
    await session.commit()
    return closed


async def cancel_card(
    session: AsyncSession,
    *,
    author_id: int,
    card_uuid: uuid_lib.UUID,
    now: datetime | None = None,
    repository: EventCardRepository | None = None,
) -> EventCard:
    """AC-04 (FR100, R-04): мягкая отмена карточки автором.

    `DELETE /cards/{uuid}` и `POST /cards/{uuid}/cancel` вызывают ЭТУ функцию
    (D-4.6-2): физического удаления в проекте нет, и два пути с разной
    семантикой разошлись бы при первом же баге.

    `now` не используется в предикате (отмена не зависит от времени), но
    принимается для единообразия с остальными операциями и чтобы тесты могли
    фиксировать момент.
    """
    # `now` не участвует в предикате отмены (она не зависит от времени), но
    # остаётся в подписи: тесты фиксируют момент, и все операции стори
    # принимают его одинаково (иначе вызовы расходились бы по форме).
    del now
    repo = repository if repository is not None else EventCardRepository(session)

    card = await repo.get_owned(author_id, card_uuid)
    if card is None:
        raise CardNotFoundError()

    updated = await repo.cancel_by_author(card_id=card.id, author_id=author_id)
    if updated == 0:
        fresh = await repo.get_owned(author_id, card_uuid)
        if fresh is None:
            raise CardNotFoundError()
        # AC-04: `finished` (история) и `cancelled` (повтор) — единственные
        # статусы, где CAS не срабатывает.
        raise CardNotCancellableError()

    cancelled = await repo.get_owned(author_id, card_uuid)
    if cancelled is None:
        raise CardNotFoundError()

    # Стори 6.7 (AC-03, FR100, D-6.7-7): системное сообщение об отмене
    # карточки — в ТОЙ ЖЕ транзакции, что и `UPDATE` статуса. Чат есть только
    # у собранной карточки (`get_by_event_card_id` → `None` у несобранной):
    # объяснять отмену некому и негде, и сообщения не создаётся.
    # `append_for_card` сам решает, есть ли чат и есть ли хранилище: у
    # несобранной карточки чата нет (FR30), а заглушка сессии в unit-тестах
    # матрицы переходов не умеет исполнять запросы.
    cancel_system_message = await append_for_card(
        session,
        card_id=cancelled.id,
        kind=SystemMessageKind.CARD_CANCELLED,
        settings=_system_message_settings(),
    )

    await session.commit()

    # Стори 6.7 (AC-03, ADR-11): публикация ТОЛЬКО после commit. Событие в
    # канал чата — это и есть «уведомление всем участникам» (FR100): у
    # компании один чат на карточку (ADR-04), и подписка на него уже есть.
    if cancel_system_message is not None:
        sys_chat_uuid, sys_uuid, sys_body, sys_created_at = cancel_system_message
        await publish_system_message(
            chat_uuid=sys_chat_uuid,
            message_uuid=sys_uuid,
            body=sys_body,
            created_at=sys_created_at,
        )
    return cancelled


async def finish_started_cards(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = CARDS_WORKER_BATCH,
    repository: EventCardRepository | None = None,
) -> int:
    """AC-05 (FR73, NFR20): архивирует начавшиеся встречи, возвращает число.

    Один прогон обрабатывает до `limit` карточек. Выборка идёт под
    `FOR UPDATE SKIP LOCKED`, поэтому два воркера не берут одну строку, а
    `rowcount == 0` после CAS — не ошибка (карточку закрыли/отменили
    параллельно). Возвращаемое число — то, что ФАКТИЧЕСКИ переведено
    в `finished`, а не «сколько выбрано»: по нему воркер логирует прогресс
    и решает, идти ли на следующий батч в этом же прогоне.

    `commit` — один на батч: каждая карточка — отдельный CAS, но фиксировать
    их поштучно значило бы N round-trip'ов на ровном месте. Потеря батча при
    падении безопасна: следующий прогон выберет те же строки (идемпотентность
    ADR-03).
    """
    moment = now if now is not None else datetime.now(UTC)
    repo = repository if repository is not None else EventCardRepository(session)

    card_ids = await repo.due_finish_ids(now=moment, limit=limit)
    finished = 0
    for card_id in card_ids:
        finished += await repo.finish_card(card_id=card_id, now=moment)
    if finished:
        await session.commit()
    return finished
