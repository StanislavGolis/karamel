"""Конфигурация приложения через pydantic-settings (переменные окружения).

Правила:
- `.env` читается из корня процесса и из корня репозитория (в контейнере WORKDIR=/app).
- `env` нормализуется и ограничен набором значений: `ENV=Production` не открывает /docs.
- Критичные переменные валидируются в `model_validator`: вне dev запрещён localhost в DATABASE_URL.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Путь к .env строится безопасно: в контейнере файл лежит в /app/app/core/config.py,
# где родителей всего четыре, и жёсткий parents[4] падал бы с IndexError на старте.
_THIS = Path(__file__).resolve()
_CANDIDATES = [Path(".env"), Path("/app/.env")]
if len(_THIS.parents) > 4:
    _CANDIDATES.append(_THIS.parents[4] / ".env")  # локально: корень репозитория
_ENV_FILES = tuple(str(p) for p in _CANDIDATES)

# AC-03 (Task 2.4): тот же шаблон стоит в `ck_places_community_slug`. Две копии
# намеренно: конфиг проверяет `.env` до подключения к БД, а CHECK держит
# инвариант для вставок сырым SQL и из воркера. Расхождение шаблонов дало бы
# `500` на сохранении — Pydantic пропустил, PostgreSQL отверг.
_COMMUNITY_SLUG_RE = re.compile(r"[a-z0-9_-]{3,64}")

# Стори 4.4, Task 1 (AC-02, FR71): диапазон лимита активных карточек автора.
# FR71 говори�� «параметр 1–2» — вне этого диапазона значение бессмысленно:
# `0` запретил бы публикацию всем, `3` нарушил бы требование ленты. Константы
# живут здесь, а не в модели: `config.py` не импортирует SQLAlchemy на старте
# (тот же приём, что `CARD_DEFAULT_DECISION_WINDOW_MINUTES`).
CARD_ACTIVE_LIMIT_MIN = 1
CARD_ACTIVE_LIMIT_MAX = 2

# Стори 5а.2, Task 3.2 (AC-03, FR77): нижняя граница доменного лимита активных
# заявок. Верхней нет: FR77 задаёт «не больше двух» продуктом, но правило
# «лимит > 0» — инвариант, а не вкус, поэтому проверяется только он.
JOIN_REQUEST_ACTIVE_LIMIT_MIN = 1


class Settings(BaseSettings):
    """Настройки из окружения. Значения по умолчанию — только для локальной разработки."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",  # окружение может содержать ключи других сервисов
        case_sensitive=False,
    )

    env: Literal["development", "staging", "production", "test"] = "development"
    database_url: str = "postgresql+asyncpg://karamel:karamel_pass@127.0.0.1:5432/karamel_db"
    redis_url: str = "redis://127.0.0.1:6379/0"
    web_origin: str = "http://localhost:3000"
    api_url: str = "http://127.0.0.1:8000"

    # Task 1.1 (AC-01, AC-02): реквизиты OAuth-приложений. Пустые значения в dev —
    # норма: клиенты проверяются моками, живой вход делает владелец (Task 11.3).
    yandex_client_id: str = ""
    yandex_client_secret: str = ""
    vk_client_id: str = ""
    vk_client_secret: str = ""
    # Task 1.1 (AC-06): ключ подписи access-токена. Дефолт — маркер «не настроено».
    # CR 1.5, патч 26: длина ≥32 символов. `_validate_secrets` работает во всех
    # окружениях, поэтому короткий дефолт ронял импорт `config` без `.env`
    # даже в dev/test — то есть любой прогон, кроме `cp .env.example .env`.
    # Значение по-прежнему отклоняется вне dev проверкой `_validate_critical`.
    jwt_secret: str = "dev_only_jwt_secret_change_me_32ch"
    # Task 1.1 (AC-05): TTL записи state в Redis, секунды.
    oauth_state_ttl_seconds: int = 600
    # Имя cookie, привязывающей `state` к браузеру (защита от login CSRF).
    oauth_cookie_name: str = "karamel_oauth"
    # Task 6.1 (AC-01, D-11, Решение 3 CR 1.3): домен session-cookie.
    # Пусто = host-only: cookie не уходит соседним поддоменам. Значение нужно
    # только когда web и API разведены по поддоменам одного домена.
    session_cookie_domain: str | None = None

    # Task 1.1 (AC-01, AC-04, AC-07): вход по телефону. `stub` пишет код в лог и
    # разрешён только в development/test — вне dev валидатор его запрещает.
    sms_provider: Literal["smsru", "stub"] = "smsru"
    smsru_api_id: str = ""
    # Dev-заглушка (действует только при `SMS_PROVIDER=stub`): фиксированный код
    # вместо `secrets`-генерации. Зачем: вход по телефону отлаживается вручную,
    # настоящих SMS в пилоте нет, и сгенерированный код приходится каждый раз
    # искать в логе контейнера — на телефоне это лишний шаг и лишняя ошибка.
    # Пустое по умолчанию = обычная генерация: заглушка включается только явным
    # значением в `.env` разработчика и никогда сама.
    # Вне dev запрещено валидатором `_validate_sms_stub_fixed_code`: фиксированный
    # код в проде — вход в любой аккаунт с этим номером без доступа к SMS, то есть
    # обход второго фактора целиком.
    sms_stub_fixed_code: str = ""
    # Номера через запятую, для которых действует фиксированный код.
    # Пусто = код действует для ЛЮБОГО номера (для ручного теста удобнее: не нужно
    # помнить нормализованную запись `+7…`). Заданный список = только эти номера.
    sms_stub_fixed_phones: str = ""
    # Task 1.3: перец для HMAC-хеша кода. Дефолт — маркер «не настроено»,
    # вне dev он не проходит валидацию секретов.
    # CR 1.5, патч 26: длина ≥32 символов по той же причине, что у `jwt_secret`.
    sms_code_secret: str = "dev_only_sms_code_secret_change_me32"
    # Стори 6.1 (AC-04, NFR31): ключ шифрования переписки чата. В БД НЕ
    # хранится: тело сообщения — шифртекст AES-256-GCM, ключ живёт только
    # в окружении. Дефолт — маркер «не настроено», как у `jwt_secret`: любая
    # его форма отклоняется вне development/test, а длина проверяется
    # в `_validate_secrets` (sha256 от короткой строки — перебор за минуты).
    chat_message_key: str = "dev_only_chat_message_key_change_me32"

    #: NFR52, NFR63 (стори 8.3): порог «просроченной» жалобы в часах.
    #: Продуктовое значение, поэтому настройка, а не литерал в сервисе.
    reports_overdue_hours: int = 20
    sms_code_ttl_seconds: int = 300
    sms_code_length: int = 6
    sms_code_max_attempts: int = 5
    sms_rate_window_seconds: int = 600
    sms_rate_ip_limit: int = 3
    sms_rate_phone_limit: int = 3
    sms_resend_cooldown_seconds: int = 60
    # Лимит на `/verify` шире, чем на `/request`: проверять код законно можно
    # чаще, чем запрашивать SMS. Без него 5 запросов с мусорным кодом блокируют
    # вход жертвы, а атакующий повторяет цикл каждые 60 с.
    sms_verify_window_seconds: int = 600
    sms_verify_ip_limit: int = 10
    # CR 1.4, Решение 1: лимит `/verify` по телефону. Без него атакующий
    # с нескольких адресов перебирает код одного номера, а per-IP лимит
    # обходится сменой адреса.
    sms_verify_phone_limit: int = 10

    # Task 1.1 (AC-04, D-6 стори 2.3): город пилота — эталон нормализации.
    # Нормализация без эталона невозможна: `'томск'` и `'Томск'` нужно свести
    # к одному написанию, и держать его хардкодом в сервисе нельзя
    # (FR16: «меняется конфигурацией без изменения кода»).
    pilot_city: str = "Томск"
    # Task 1.2 (AC-06, D-9): прямоугольник пилота.
    # Порядок кортежа — (lat_min, lat_max, lng_min, lng_max): сначала обе
    # широты, потом обе долготы. Обратный порядок («min/max по паре»)
    # читался бы короче, но перемешивал оси, и `PILOT_CITY_BBOX` из .env
    # с перепутанными местами давал бы ответ «внутри» в Атлантике.
    # Геокодер не вызывается: координаты сравниваются с константой в памяти
    # (трансграничная передача, NFR34/152-ФЗ).
    # `NoDecode` обязателен: pydantic-settings разбирает complex-типы
    # (tuple/list/dict) из окружения как JSON ещё до валидаторов, и строка
    # `56.30,56.60,84.80,85.20` роняла старт с SettingsError. JSON в .env
    # нечитаем для владельца, поэтому строку разбирает свой валидатор ниже.
    pilot_city_bbox: Annotated[tuple[float, float, float, float], NoDecode] = (
        56.30,
        56.60,
        84.80,
        85.20,
    )

    # Стори 3.1 (AC-03, ADR-01, D-3): сообщества пилота — срез данных, не таблица.
    # `places.community_slug` — колонка-строка с regex-CHECK, FK на `communities`
    # нет: в пилоте одно сообщество (NFR46), а таблица-справочник из одной строки
    # дала бы `join` в каждом запросе без выигрыша.
    #
    # Список нужен сервису: без сверки с ним опечатка в слаге создаёт место,
    # которого не видно ни в одной ленте (`WHERE community_slug = ...` не
    # совпадёт), — «невидимое сообщество» без единой ошибки в логах.
    # `NoDecode` обязателен по той же причине, что у `pilot_city_bbox`:
    # pydantic-settings разбирает tuple из окружения как JSON ещё до
    # валидаторов, и `COMMUNITY_SLUGS=tomsk,kemerovo` роняло бы старт
    # `SettingsError`. JSON в `.env` нечитаем для владельца.
    community_slugs: Annotated[tuple[str, ...], NoDecode] = ("tomsk",)
    # AC-03: значение по умолчанию для создания места без явного слага.
    # Обязано входить в `community_slugs`, иначе сервис отказывает на
    # собственном дефолте — проверка на старте, а не на первом сохранении.
    default_community_slug: str = "tomsk"

    # Стори 4.1, Task 5.1 (AC-03, OQ-1): окно решения по умолчанию для режима
    # «открытый набор» (вместимость 2–6). FR70 прямо говорит, что в этом
    # режиме окно у автора НЕ запрашивается — значит, значение обязано
    # приходить из конфигурации, а не из кода 4.2: иначе смена продуктового
    # дефолта была бы правкой кода, а не настройки.
    #
    # OQ-1 (решение пользователя 2026-10-23): 2880 минут = 48 часов.
    # 24 ч вынуждали автора сидеть у экрана, 72 ч выкидывали карточку из
    # ленты на три дня; 48 ч — середина. Диапазон CHECK `15..10080` не
    # меняется: значение остаётся конфигурируемым внутри него.
    card_default_decision_window_minutes: int = 2880

    # Стори 5б.4, Task 5.1 (AC-02, FR103, ADR-15): за сколько минут до
    # `expires_at` автору уходит напоминание «успей выбрать сам».
    #
    # Дефолт 15 — нижняя граница окна решения (`CARD_MIN_DECISION_WINDOW_MINUTES`
    # в модели). Порог больше минимального окна означал бы «напоминание после
    # закрытия», то есть недостижимый FR103 на коротких окнах. Верхняя граница
    # 1440 (сутки) отсекает конфигурацию, при которой напоминание приходит
    # сразу после публикации и автор привыкает его игнорировать.
    #
    # Значение — настройка, а не константа кода: FR91/§18.6 требуют менять
    # поведение без пересборки образа (`.env` на стенде).
    card_reminder_threshold_minutes: int = 15

    # Стори 4.4, Task 1 (AC-01, AC-02, FR71): предел ОДНОВРЕМЕННО активных
    # карточек автора (`published`/`full`/`closed`). Значение — настройка,
    # а не константа кода: FR91 требует менять поведение без правок кода,
    # а `.env` на стенде меняется без пересборки образа (§18.6).
    #
    # Дефолт 2 — верхняя граница MVP (OQ-3 стори): мягкий старт не забивает
    # ленту, но оставляет автору вторую попытку. Вне `1..2` старт падает:
    # ошибка конфигурации не должна выглядеть ошибкой пользователя.
    card_active_limit: int = 2

    # Стори 5а.2, Task 3.2 (AC-03, FR77, NFR29): доменный предел ОДНОВРЕМЕННО
    # активных заявок участника (`pending`). Значение — настройка по тому же
    # доводу, что `card_active_limit`: FR77 задаёт правило, но точное число
    # должно меняться без правок кода.
    #
    # Не путать с rate limit подач (AC-10): доменный лимит отвечает на вопрос
    # «сколько заявок висит у автора прямо сейчас», технический — «сколько раз
    # в сутки аккаунт нажимает кнопку». Совпадение чисел сделало бы FR82
    # невыполнимым: отменив заявку, участник не смог бы подать вторую.
    # Дефолт 2 = FR77 («не больше двух активных»); вне `>= 1` старт падает.
    join_request_active_limit: int = 2

    # Стори 5а.2, Task 5.1 (AC-10, FR61, AR32): технический потолок подач за
    # окно — анти-абьюз, а не продукт. `OQ-5a.2-1`: 10 подач / 24 ч.
    #
    # ВЫШЕ доменного лимита намеренно: два лимита с одинаковым числом
    # запретили бы вторую подачу после отмены (FR82), и участник оказался бы
    # заблокирован на сутки собственным легальным действием.
    join_request_rate_limit: int = 10
    # AC-10: окно технического лимита. Сутки — единица NFR17 («в сутки»).
    # Отдельная настройка, а не `sms_rate_window_seconds`: у SMS окно короткое
    # (защита номера от спама), у подач — длинное (защита автора от шума).
    join_request_rate_window_seconds: int = 86400

    # Стори 9.5, Task 4.1 (AC-01, D-9.5-9, FR55): технический лимит обращений
    # в поддержку. Доменного лимита НЕТ: FR55 не задаёт числа активных
    # обращений (в отличие от FR77 у заявок), а состояние `open` не мешает
    # отправить следующее. 5/ч отсекает флуд, не мешая уточнить проблему.
    #
    # Настройка, а не литерал (R-1): число пересматривается продуктовым
    # решением — через `.env`, без пересборки образа (§18.6).
    support_ticket_rate_limit: int = 5
    # Стори 9.5, Task 4.1: окно лимита. Час — единица D-9.5-9 («5 обращений
    # в час»). Отдельная настройка, а не общая с заявками: у заявок окно
    # сутки (NFR17), у поддержки час (человек разбирается в проблеме, а не
    # ждёт следующего дня).
    support_ticket_rate_window_seconds: int = 3600

    # Стори 4.3, Task 2 (AC-02, FR91, D-4.3-1): стоп-слова автопроверки
    # карточки. Значения — ДАННЫЕ, а не код: FR91 требует менять поведение без
    # правок кода, а деплой по §18.6 — только полная пересборка образа; `.env`
    # меняется без пересборки.
    #
    # `NoDecode` обязателен по той же причине, что у `community_slugs`:
    # pydantic-settings разбирает complex-типы (tuple) как JSON, и обычная
    # строка `казино,займ` падала бы на JSONDecodeError при старте.
    #
    # Дефолт — минимальный набор под MVP (ADR-08 называет «стоп-слова» классом
    # правил, но списка не даёт; §1118 отдаёт его стори модерации). Приведение
    # к нижнему регистру и нормализация — валидатором ниже, поэтому сравнение
    # в правилах идёт по `casefold()` с обеих сторон.
    card_content_stop_words: Annotated[tuple[str, ...], NoDecode] = (
        "интим",
        "эскорт",
        "казино",
        "займ",
        "крипт",
        "ставки",
        "свидание с продолжением",
        "18+",
    )

    # Фикс OOM (2026-10-22, дефект стенд-проверки 2.3): предел размера тела
    # запроса. Тело на ~10 МБ роняло контейнер `api` по OOM: оно целиком
    # читалось в память, а нормализация шла синхронно в async-обработчике.
    # Гард `core.body_limit` отбивает такое `413 request_too_large` до чтения.
    # 1 МиБ — с большим запасом для анкеты: `bio` 500 символов UTF-8 ≈ 1 КБ,
    # самое тяжёлое легальное тело — единицы килобайт.
    # Стори 2.2 (Task 8.4, AC-08): это ОБЩИЙ лимит, а не единственный.
    # Загрузка фото получает свой через карту `overrides` в `main.py`
    # (`max_photo_bytes + MULTIPART_OVERHEAD`). Поднимать общий лимит ради
    # одного маршрута нельзя: это возвращает исходный OOM на анкете.
    max_request_body_bytes: int = 1_048_576

    # Task 2.1 стори 2.2 (AC-02, AC-04, D-8): приватное S3-хранилище фото.
    # Имена ровно как переменные в `.env` (`S3_ENDPOINT`, `S3_REGION`, ...):
    # pydantic-settings сопоставляет их без учёта регистра, и вводить
    # вторые имена — значит получить два источника правды для одной вещи.
    # Пустые значения в dev — норма (живая проверка делает владелец),
    # вне dev пустые реквизиты роняют старт, как OAuth и SMS.
    s3_endpoint: str = ""
    s3_region: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    # Task 2.2a: секрет попадает в `Settings`, а `Settings` целиком печатается
    # при диагностике конфигурации — поэтому он в редакции логов (`logging.py`).
    s3_secret_key: str = ""
    # D-11: TTL подписанной ссылки, секунды. 900 — компромисс: клиент успевает
    # показать фото, а ссылка не живёт в истории браузера сутками.
    s3_signed_url_ttl: int = 900
    # AC-05, AC-08: предел размера самого файла (не тела multipart).
    # 5 МиБ — типичный кадр телефона после сжатия; больше — OOM контейнера.
    max_photo_bytes: int = 5_242_880

    @field_validator("session_cookie_domain", mode="before")
    @classmethod
    def _normalize_session_cookie_domain(cls, v: object) -> object:
        """Task 6.2/6.4 (AC-01, D-11): нормализация домена cookie и fail-fast.

        Нормализация нужна потому, что в `.env` копируют адрес целиком
        (`https://karamel.ru/`). Без неё `delete_cookie` и `set_cookie`
        получили бы разные строки — это две разные cookie, и старая осталась бы
        в браузере после выхода.

        Валидация на старте, а не в момент выдачи: кривой домен всплывает
        не на первом запросе, а на первом выходе из аккаунта — то есть в проде.
        """
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        raw = v.strip()
        if not raw:
            # Task 6.5: пусто = host-only, это валидное состояние по умолчанию.
            return None
        host = raw.split("://", 1)[1] if "://" in raw else raw
        host = host.split("/", 1)[0].strip().lower()
        # Порт и userinfo в домене cookie недопустимы: `example.com:3000`
        # браузер отбросит целиком, и cookie просто не установится.
        if ":" in host or "@" in host or not host:
            raise ValueError(
                "SESSION_COOKIE_DOMAIN должен быть доменом без схемы и порта, "
                f"например `karamel.ru`; получено: {raw!r}"
            )
        host = host.lstrip(".")
        if not host or "." not in host and host != "localhost":
            raise ValueError(
                "SESSION_COOKIE_DOMAIN должен содержать точку (или быть localhost); "
                f"получено: {raw!r}"
            )
        return host

    @field_validator("pilot_city", mode="before")
    @classmethod
    def _normalize_pilot_city(cls, v: object) -> object:
        """Task 1.1 (AC-04), §18.1: эталон без краёв и NBSP.

        Значение приходит из `.env`, где легко получить `PILOT_CITY=" Томск "`.
        Такой эталон не совпал бы ни с одним нормализованным городом,
        и канонизация молча перестала бы работать — ровно тот класс дефекта,
        который видно только на живом стенде. Поэтому trim и замена NBSP здесь.
        """
        if not isinstance(v, str):
            return v
        # CR 2.6, патч 1 (AC-03): эталон канонизируется тем же `normalize_text`,
        # каким `normalize_city` канонизирует ввод при сохранении анкеты.
        # `strip()` + NBSP мало: внутренний таб/двойной пробел/ZWSP в `PILOT_CITY`
        # давал эталон `'Томск\tцентр'`, тогда как в БД лежит `'Томск центр'`
        # (его записала нормализация 2.3). Точное `=` в предикате видимости
        # возвращало бы 0 строк молча, без ошибки и без лога.
        #
        # Импорт локальный: `app.core` не должен зависеть от `app.services`
        # на уровне модуля (направление слоёв), а цикла здесь нет —
        # `profile_normalize` импортирует только `re` и `unicodedata`.
        from app.services.profile_normalize import normalize_text

        raw = normalize_text(v) if v.strip() else ""
        if not raw:
            raise ValueError("PILOT_CITY не может быть пустым: он эталон нормализации города")
        if len(raw) > 100:
            raise ValueError("PILOT_CITY длиннее 100 символов: не влезет в profiles.city")
        return raw

    @field_validator("community_slugs", mode="before")
    @classmethod
    def _parse_community_slugs(cls, v: object) -> object:
        """AC-03 (Task 2.3): разбор `tomsk,kemerovo` и нормализация каждого слага.

        Порядок шагов повторяет `pilot_city_bbox` (разбор строки) и `pilot_city`
        (нормализация эталона): `NoDecode` отдаёт строку как есть, а `strip()`
        недостаточно — NBSP из копипаста в `.env` не режется ни `strip()`,
        ни `btrim` в PostgreSQL, и слаг с NBSP не совпал бы ни с одним
        сохранённым значением. Поэтому эталон нормализуется тем же
        `normalize_text`, каким сервис нормализует вход (CR 2.6, патч 1).

        Регистр НЕ приводится (D-5): шаблон `^[a-z0-9_-]{3,64}$` требует
        нижний регистр, и `COMMUNITY_SLUGS=Tomsk` должен падать, а не тихо
        превращаться в `tomsk` — иначе значение в `.env` расходится
        с тем, что реально используется.
        """
        if isinstance(v, str):
            v = tuple(chunk for chunk in v.split(","))
        if not isinstance(v, (tuple, list)):
            return v
        from app.services.profile_normalize import normalize_text

        normalized = tuple(normalize_text(str(chunk)) for chunk in v)
        # Хвостовая запятая (`tomsk,`) — обычная опечатка в `.env`; пустая
        # строка здесь означала бы «сообщество без имени», поэтому она
        # отбрасывается, а не превращается в элемент кортежа.
        normalized = tuple(chunk for chunk in normalized if chunk)
        if not normalized:
            raise ValueError(
                "COMMUNITY_SLUGS не может быть пустым: без сообщества создание "
                "места отказывает всегда (AC-03)"
            )
        for chunk in normalized:
            if not _COMMUNITY_SLUG_RE.fullmatch(chunk):
                raise ValueError(
                    "COMMUNITY_SLUGS: каждый слаг должен соответствовать "
                    f"^[a-z0-9_-]{{3,64}}$ (как ck_places_community_slug); получено: {chunk!r}"
                )
        return normalized

    @field_validator("default_community_slug", mode="before")
    @classmethod
    def _normalize_default_community_slug(cls, v: object) -> object:
        """AC-03: дефолт нормализуется тем же кодом, что и список сообществ.

        Без этого `DEFAULT_COMMUNITY_SLUG=" tomsk"` не нашёлся бы в
        `community_slugs` и ронял старт на ровном месте — при том что
        значение валидное, просто с пробелом из `.env`.
        """
        if not isinstance(v, str):
            return v
        from app.services.profile_normalize import normalize_text

        return normalize_text(v)

    @field_validator("card_default_decision_window_minutes")
    @classmethod
    def _validate_card_decision_window(cls, v: int) -> int:
        """Task 5.1 (AC-03): дефолт окна внутри диапазона CHECK `15..10080`.

        Проверка на старте, а не при создании карточки: значение вне
        диапазона отверг бы `ck_event_cards_decision_window`, и автор
        получил бы `500` на валидной форме — ошибка конфигурации,
        выданная за ошибку пользователя. Тот же диапазон, что в модели
        (`CARD_MIN_DECISION_WINDOW_MINUTES` / `CARD_MAX_...`), но здесь
        числа записаны явно: `config.py` не импортирует модели (иначе
        конфиг тянул бы SQLAlchemy на старте до подключения к БД).
        """
        if not 15 <= v <= 10_080:
            raise ValueError(f"CARD_DEFAULT_DECISION_WINDOW_MINUTES вне диапазона 15..10080: {v}")
        return v

    @field_validator("card_reminder_threshold_minutes")
    @classmethod
    def _validate_card_reminder_threshold(cls, v: int) -> int:
        """Task 5.1 (AC-02, FR103): порог напоминания в диапазоне `1..1440`.

        Проверка на старте, а не в воркере: значение вне диапазона дало бы
        либо напоминание сразу после публикации (`0`/отрицательное), либо
        порог больше суток, при котором напоминание приходит за сутки до
        закрытия окна и теряет смысл. Ошибка конфигурации обязана валить
        старт, а не выглядеть «напоминания не работают».
        """
        if not 1 <= v <= 1440:
            raise ValueError(f"CARD_REMINDER_THRESHOLD_MINUTES вне диапазона 1..1440: {v}")
        return v

    @field_validator("card_active_limit")
    @classmethod
    def _validate_card_active_limit(cls, v: int) -> int:
        """Task 1 (AC-02, FR71): лимит строго в диапазоне `1..2`.

        Проверка на старте, а не при публикации: `0` сделал бы публикацию
        невозможной для всех (и автор видел бы `429` на валидной форме),
        `3` — нарушил бы FR71. Ошибка конфигурационная и должна ронять
        старт тем же приёмом, что диапазон окна решения.
        """
        if not CARD_ACTIVE_LIMIT_MIN <= v <= CARD_ACTIVE_LIMIT_MAX:
            raise ValueError(
                f"CARD_ACTIVE_LIMIT вне диапазона "
                f"{CARD_ACTIVE_LIMIT_MIN}..{CARD_ACTIVE_LIMIT_MAX}: {v}"
            )
        return v

    @field_validator("reports_overdue_hours")
    @classmethod
    def _validate_reports_overdue_hours(cls, v: int) -> int:
        """CR 8.3, H-4 (NFR52, NFR63): порог просрочки строго в `1..168`.

        Стори 8.3 заявляла этот валидатор в Dev Notes («Валидатор: `1..168`»),
        но в коде его не было — заявленное расходилось с фактическим.

        Почему это дефект, а не придирка. Порог участвует в сравнении
        `(now - reported_at) > threshold`:

        - `0` и отрицательные делают просроченной ЛЮБУЮ жалобу, включая
          поданную секунду назад, — метка `overdue` перестаёт что-либо значить;
        - значение больше года (`500` даёт ~21 день, `100000` — годы) делает
          её недостижимой, и NFR52 не выполняется молча.

        Верхняя граница `168` (неделя) — продуктовый предел: жалоба, не
        разобранная за неделю, просрочена при любом разумном SLA. Ошибка
        конфигурационная и должна ронять старт, как соседние лимиты.
        """
        if not 1 <= v <= 168:
            raise ValueError(f"REPORTS_OVERDUE_HOURS вне диапазона 1..168: {v}")
        return v

    @field_validator("join_request_active_limit")
    @classmethod
    def _validate_join_request_active_limit(cls, v: int) -> int:
        """Task 3.2 (AC-03): доменный лимит не может быть нулевым.

        `0` запретил бы подачу всем и выглядел бы как ошибка пользователя
        (`429` на валидной форме). Проверка на старте роняет конфигурацию,
        а не рабочий запрос — тот же приём, что `card_active_limit`.
        """
        if v < JOIN_REQUEST_ACTIVE_LIMIT_MIN:
            raise ValueError(
                f"JOIN_REQUEST_ACTIVE_LIMIT меньше {JOIN_REQUEST_ACTIVE_LIMIT_MIN}: {v}"
            )
        return v

    @model_validator(mode="after")
    def _validate_join_request_rate_limit(self) -> "Settings":
        """Task 5.1 (AC-10): технический потолок строго выше доменного — иначе
        вторая подача после отмены стала бы невозможной (FR82, OQ-5a.2-1).

        CR 5а.2 (патч 1): сравнение идёт с ФАКТИЧЕСКИМ `join_request_active_limit`,
        а не с его нижней границей `JOIN_REQUEST_ACTIVE_LIMIT_MIN` (=1). Прежняя
        проверка `v <= JOIN_REQUEST_ACTIVE_LIMIT_MIN` пропускала конфигурацию
        `ACTIVE_LIMIT=5, RATE_LIMIT=3`: технический потолок оказывался НИЖЕ
        доменного, и участник с двумя активными заявками получал бы `rate_limited`
        раньше, чем доменный лимит вообще начинал считаться (FR82 невыполним,
        ровно тот дефект, ради которого заведено OQ-5a.2-1). Проверено на живом
        `Settings(join_request_active_limit=5, join_request_rate_limit=3)`.
        """
        if self.join_request_rate_limit <= self.join_request_active_limit:
            raise ValueError(
                f"JOIN_REQUEST_RATE_LIMIT ({self.join_request_rate_limit}) должен быть "
                f"выше доменного JOIN_REQUEST_ACTIVE_LIMIT "
                f"({self.join_request_active_limit})"
            )
        return self

    @field_validator("support_ticket_rate_limit")
    @classmethod
    def _validate_support_ticket_rate_limit(cls, v: int) -> int:
        """Task 4.1 (AC-01, D-9.5-9): технический лимит положительный.

        `0` или отрицательное значение выключили бы анти-абьюз молча: первый
        же `INCR` дал бы `count=1 > limit`, и пользователь получал бы `429`
        НА ПЕРВОЕ обращение. Ноль не «без лимита», а «запрещено всё» —
        настройка, которая не имеет осмысленного прочтения, должна падать
        на старте, а не в бою.
        """
        if v <= 0:
            raise ValueError(f"SUPPORT_TICKET_RATE_LIMIT должен быть > 0, получено {v}")
        return v

    @field_validator("support_ticket_rate_window_seconds")
    @classmethod
    def _validate_support_ticket_rate_window(cls, v: int) -> int:
        """Task 4.1: окно положительное (иначе ключ без TTL — вечный лимит).

        `SET ... NX EX 0` не создаёт срок: ключ жил бы вечно, и пользователь
        после пяти обращений не смог бы написать в поддержку НИКОГДА.
        """
        if v <= 0:
            raise ValueError(f"SUPPORT_TICKET_RATE_WINDOW_SECONDS должен быть > 0, получено {v}")
        return v

    @field_validator("join_request_rate_window_seconds")
    @classmethod
    def _validate_join_request_rate_window(cls, v: int) -> int:
        """Task 5.1 (AC-10): окно положительное и не больше суток.

        Окно `<= 0` не создало бы ключ с TTL (`SET ... NX` без срока), и
        лимит стал бы вечным; окно больше суток нарушило бы NFR17.
        """
        if not 1 <= v <= 86400:
            raise ValueError(f"JOIN_REQUEST_RATE_WINDOW_SECONDS вне 1..86400: {v}")
        return v

    @field_validator("card_content_stop_words", mode="before")
    @classmethod
    def _parse_content_stop_words(cls, v: object) -> object:
        """AC-02, FR91: разбор `казино,займ` и нормализация каждого слова.

        `NoDecode` отдаёт строку как есть (тот же приём, что у `community_slugs`):
        pydantic-settings иначе попытался бы разобрать её как JSON.

        Нормализация тем же `normalize_text`, каким сервис нормализует текст
        карточки: стоп-слово с NBSP из копипаста в `.env` не срезалось бы
        ни `strip()`, ни `btrim`, и правило не сработало бы никогда.

        Регистр приводится к нижнему: в отличие от слага сообщества, стоп-слово —
        не идентификатор, а человекочитаемое правило; `.env` с `Казино`
        обязан работать. Пустые элементы (хвостовая запятая) отбрасываются,
        пустой итог — ошибка: без стоп-слов правило `stop_word` мертво, и это
        должно выясняться на старте, а не на первой спам-карточке.
        """
        if isinstance(v, str):
            v = tuple(v.split(","))
        if not isinstance(v, (tuple, list)):
            return v
        from app.services.profile_normalize import normalize_text

        normalized = tuple(normalize_text(str(chunk)).casefold() for chunk in v)
        normalized = tuple(chunk for chunk in normalized if chunk)
        if not normalized:
            raise ValueError(
                "CARD_CONTENT_STOP_WORDS не может быть пустым: правило stop_word "
                "без словарного запаса не срабатывает никогда (AC-02, FR101)"
            )
        return normalized

    @model_validator(mode="after")
    def _validate_community(self) -> "Settings":
        """AC-03: дефолтное сообщество обязано входить в список сообществ.

        Проверка на старте, а не в момент создания места: `DEFAULT_COMMUNITY_SLUG`
        вне `community_slugs` означает, что `POST /places` без явного слага
        отказывает всегда — админка 3.2 не работает вовсе. Такое состояние
        должно ронять старт, а не выясняться на первом живом сохранении.
        """
        if self.default_community_slug not in self.community_slugs:
            raise ValueError(
                "DEFAULT_COMMUNITY_SLUG должен входить в COMMUNITY_SLUGS: "
                f"{self.default_community_slug!r} отсутствует в {self.community_slugs!r}"
            )
        return self

    @field_validator("pilot_city_bbox", mode="before")
    @classmethod
    def _validate_pilot_city_bbox(cls, v: object) -> object:
        """Task 1.2 (AC-06): границы диапазонов и порядок — fail-fast на старте.

        Перевёрнутый прямоугольник (`lat_min > lat_max`) не «пустой», а
        невалидный: точка не попадёт внутрь никогда, и `locate-city`
        будет всегда отвечать `outside_pilot_city`. Такое значение должно
        ронять старт, а не превращать определение города в тихий отказ.
        """
        # Строка из `.env` (`56.30,56.60,84.80,85.20`) и кортеж из Python —
        # оба входа допустимы: `NoDecode` отдаёт строку как есть.
        if isinstance(v, str):
            parts = [chunk.strip() for chunk in v.split(",")]
            if len(parts) != 4 or not all(parts):
                raise ValueError(
                    "PILOT_CITY_BBOX должен содержать четыре числа через запятую: "
                    "lat_min,lat_max,lng_min,lng_max"
                )
            try:
                v = tuple(float(chunk) for chunk in parts)
            except ValueError:
                raise ValueError(
                    "PILOT_CITY_BBOX содержит нечисловое значение: "
                    "ожидаются четыре числа через запятую"
                ) from None
        if not isinstance(v, (tuple, list)):
            return v
        if len(v) != 4:
            raise ValueError(
                "PILOT_CITY_BBOX требует ровно четыре числа: lat_min, lat_max, lng_min, lng_max"
            )
        lat_min, lat_max, lng_min, lng_max = (float(x) for x in v)
        if not -90 <= lat_min < lat_max <= 90:
            raise ValueError(
                "PILOT_CITY_BBOX: широты должны идти по возрастанию в -90..90, "
                f"получено {lat_min}..{lat_max}"
            )
        if not -180 <= lng_min < lng_max <= 180:
            raise ValueError(
                "PILOT_CITY_BBOX: долготы должны идти по возрастанию в -180..180, "
                f"получено {lng_min}..{lng_max}"
            )
        return (lat_min, lat_max, lng_min, lng_max)

    @field_validator("env", mode="before")
    @classmethod
    def _normalize_env(cls, v: object) -> object:
        """`ENV=Production` и `ENV=prod` приводятся к каноническому значению."""
        if not isinstance(v, str):
            return v
        raw = v.strip().lower()
        aliases = {
            "prod": "production",
            "production": "production",
            "dev": "development",
            "development": "development",
            "local": "development",
            "stage": "staging",
            "staging": "staging",
            "test": "test",
        }
        return aliases.get(raw, raw)

    @model_validator(mode="after")
    def _validate_critical(self) -> "Settings":
        """Fail-fast вместо молчаливого дефолта вне dev."""
        if self.env in {"production", "staging"}:
            if "karamel_pass" in self.database_url or "127.0.0.1" in self.database_url:
                raise ValueError("DATABASE_URL содержит dev-значение при ENV=" + self.env)
            if "127.0.0.1" in self.redis_url:
                raise ValueError("REDIS_URL указывает на localhost при ENV=" + self.env)
            # Task 1.2 (AC-01, AC-02): без реквизитов провайдера вход нерабочий,
            # а пустой client_id проявляется только на первом живом входе — то есть в проде.
            missing = [
                name
                for name, value in (
                    ("YANDEX_CLIENT_ID", self.yandex_client_id),
                    ("YANDEX_CLIENT_SECRET", self.yandex_client_secret),
                    ("VK_CLIENT_ID", self.vk_client_id),
                    ("VK_CLIENT_SECRET", self.vk_client_secret),
                )
                if not value.strip()
            ]
            if missing:
                raise ValueError(
                    "Не заданы OAuth-реквизиты при ENV=" + self.env + ": " + ", ".join(missing)
                )
            # Task 1.2 (AC-06): дефолтный ключ подписи = любой может выпустить токен.
            # CR 1.5, патч 26: сверяется префикс, а не точное значение: дефолт
            # удлинён до 32 символов, чтобы импорт работал без `.env`, и любая
            # его форма обязана отклоняться вне dev.
            if self.jwt_secret.startswith("dev_only_"):
                raise ValueError("JWT_SECRET оставлен dev-значением при ENV=" + self.env)
            if self.sms_code_secret.startswith("dev_only_"):
                raise ValueError("SMS_CODE_SECRET оставлен dev-значением при ENV=" + self.env)
            # Стори 6.1 (AC-04, NFR31): дефолтный ключ шифрования переписки =
            # любой читает чужую переписку из дампа БД. Вне dev он запрещён.
            # CR 6.1 (находка 4): нормализация та же, что в `_validate_secrets`
            # и в `crypto._derive_key`, — `strip()` + `lower()`. Иначе ключ
            # `"  DEV_ONLY_..."` (пробел и заглавные из копипасты) проходил
            # проверку длины и НЕ ловился здесь, то есть прод стартовал бы
            # с предсказуемым ключом, а дамп `messages` читался бы офлайн.
            if (self.chat_message_key or "").strip().lower().startswith("dev_only_"):
                raise ValueError("CHAT_MESSAGE_KEY оставлен dev-значением при ENV=" + self.env)
            # Task 1.2 (AC-01): без api_id боевой провайдер отвечает ошибкой на
            # первом же входе — то есть в проде. Пустой ключ при `stub` допустим.
            if self.sms_provider == "smsru" and not self.smsru_api_id.strip():
                raise ValueError("Не задан SMSRU_API_ID при ENV=" + self.env)
        return self

    @model_validator(mode="after")
    def _validate_secrets(self) -> "Settings":
        """Fail-fast на слабые секреты: HS256 держится ровно на их стойкости.

        Пустой или короткий `jwt_secret` позволяет подделать `sub` любого
        пользователя — это не «настройка по умолчанию», а дыра, которую
        нельзя оставлять на усмотрение деплоя.
        """
        weak = {"", "change-me", "secret", "changeme", "test"}
        secret = (self.jwt_secret or "").strip()
        if secret.lower() in weak or len(secret) < 32:
            raise ValueError("JWT_SECRET должен быть непустым и не короче 32 символов")
        # Task 1.3 (AC-01, AC-02): перец HMAC защищает коды только пока он
        # секретный. Короткий или дефолтный перец делает офлайн-перебор дампа
        # Redis тривиальным: 10^6 вариантов кода против известного ключа.
        sms_secret = (self.sms_code_secret or "").strip()
        if sms_secret.lower() in weak or len(sms_secret) < 32:
            raise ValueError("SMS_CODE_SECRET должен быть непустым и не короче 32 символов")
        # Стори 6.1 (AC-04): ключ переписки идёт в AES-256 через sha256 от
        # строки, поэтому короткий секрет = короткий перебор. Тот же порог,
        # что у `jwt_secret`/`sms_code_secret`.
        chat_secret = (self.chat_message_key or "").strip()
        if chat_secret.lower() in weak or len(chat_secret) < 32:
            raise ValueError("CHAT_MESSAGE_KEY должен быть непустым и не короче 32 символов")
        # Task 1.3 (AC-01): заглушка печатает код в лог, то есть в боевом
        # окружении это утечка фактора входа в журнал контейнера.
        if self.sms_provider == "stub" and self.env not in {"development", "test"}:
            raise ValueError("SMS_PROVIDER=stub допустим только при ENV=development или test")
        return self

    @model_validator(mode="after")
    def _validate_sms_numbers(self) -> "Settings":
        """Fail-fast на вырожденные лимиты.

        `SMS_CODE_MAX_ATTEMPTS=0` делает любой код мгновенно исчерпанным,
        отрицательный лимит отправки блокирует всех, `ttl=0` не сохраняет код,
        но отдаёт `200`. Такие значения должны падать на старте, а не в проде.
        """
        checks = (
            ("SMS_CODE_LENGTH", self.sms_code_length, 4, 8),
            ("SMS_CODE_TTL_SECONDS", self.sms_code_ttl_seconds, 30, 3600),
            ("SMS_CODE_MAX_ATTEMPTS", self.sms_code_max_attempts, 1, 20),
            ("SMS_RATE_WINDOW_SECONDS", self.sms_rate_window_seconds, 60, 86400),
            ("SMS_RATE_IP_LIMIT", self.sms_rate_ip_limit, 1, 1000),
            ("SMS_RATE_PHONE_LIMIT", self.sms_rate_phone_limit, 1, 1000),
            ("SMS_RESEND_COOLDOWN_SECONDS", self.sms_resend_cooldown_seconds, 1, 3600),
            ("SMS_VERIFY_WINDOW_SECONDS", self.sms_verify_window_seconds, 60, 86400),
            ("SMS_VERIFY_IP_LIMIT", self.sms_verify_ip_limit, 1, 10000),
            ("SMS_VERIFY_PHONE_LIMIT", self.sms_verify_phone_limit, 1, 10000),
        )
        for name, value, low, high in checks:
            if not low <= value <= high:
                raise ValueError(f"{name}={value} вне диапазона {low}..{high}")
        return self

    @model_validator(mode="after")
    def _validate_sms_stub_fixed_code(self) -> "Settings":
        """Fail-fast на фиксированный код вне заглушки.

        Три независимые причины падать на старте, а не в проде:

        1. Код не той длины. `sms_code_length` — тоже настройка, и код из 5
           цифр при длине 6 не совпадёт ни с одним введённым: разработчик
           видел бы «код не подходит» и искал причину в Redis/хеше.
        2. `sms_stub_fixed_code` при `SMS_PROVIDER=smsru` бессмыслен: боевой
           сервис его не читает, но значение в `.env` создаёт ложное
           впечатление включённой заглушки.
        3. **Главное:** фиксированный код вне dev — это вход в любой аккаунт
           с этим номером без доступа к SMS. Второй фактор перестаёт быть
           фактором, а `SMS_STUB_FIXED_PHONES` ограничивает ущерб только
           до тех пор, пока список не забыли расширить. Поэтому проверка
           не «предупреждение в лог», а падение старта.
        4. Список номеров без кода ничего не включает и почти наверняка
           означает опечатку в `.env` (забыли строку выше) — пусть падает.
        """
        fixed_code = (self.sms_stub_fixed_code or "").strip()
        fixed_phones = (self.sms_stub_fixed_phones or "").strip()
        if not fixed_code:
            if fixed_phones:
                raise ValueError(
                    "SMS_STUB_FIXED_PHONES задан, а SMS_STUB_FIXED_CODE пуст: "
                    "список номеров без кода не включает заглушку"
                )
            return self
        expected = self.sms_code_length
        # `isdigit()` пропускает юникодные цифры (арабо-индийские, полноширинные),
        # а код сравнивается как ASCII-строка: такой «код» не совпадёт никогда,
        # зато пройдёт проверку длины. `isascii()` закрывает эту щель.
        if not (fixed_code.isascii() and fixed_code.isdigit()) or len(fixed_code) != expected:
            raise ValueError(
                "SMS_STUB_FIXED_CODE должен состоять ровно из "
                f"{expected} ASCII-цифр, получено {fixed_code!r}"
            )
        if self.sms_provider != "stub":
            raise ValueError(
                "SMS_STUB_FIXED_CODE задан при SMS_PROVIDER="
                f"{self.sms_provider}: фиксированный код действует только в заглушке"
            )
        if self.env not in {"development", "test"}:
            # Прямая формулировка того, что произошло бы: любой вход на этот
            # номер пускает без SMS.
            raise ValueError(
                "SMS_STUB_FIXED_CODE допустим только при ENV=development или test "
                f"(сейчас ENV={self.env}): иначе вход на номер без второго фактора"
            )
        return self

    @model_validator(mode="after")
    def _validate_body_limit(self) -> "Settings":
        """Fail-fast на вырожденный предел тела.

        `MAX_REQUEST_BODY_BYTES=0` отклонил бы любой запрос с телом, включая
        вход и согласие: всякое POST падало бы с `413`. Слишком большой
        предел возвращает исходный OOM, поэтому верхняя граница тоже нужна —
        значение выше памяти контейнера (`mem_limit: 384m`) бессмысленно.
        """
        if not 1024 <= self.max_request_body_bytes <= 32 * 1024 * 1024:
            raise ValueError(
                "MAX_REQUEST_BODY_BYTES вне диапазона 1024..33554432: "
                f"{self.max_request_body_bytes}"
            )
        return self

    @model_validator(mode="after")
    def _validate_s3(self) -> "Settings":
        """Task 2.2 (AC-02, AC-04, D-8): либо все реквизиты, либо ни одного.

        Промежуточное состояние («endpoint есть, ключа нет») не работает
        никогда: boto3 подпишет запрос пустым ключом и получит `403` от
        провайдера. Приватность фото держится на рабочем хранилище, поэтому
        вне dev такое состояние роняет старт — как пустой OAuth или SMS.

        В dev и test пустые значения нормальны: клиенты подменяются моками
        (`fake_media`), живой вход делает владелец.
        """
        values = (
            self.s3_endpoint,
            self.s3_region,
            self.s3_bucket,
            self.s3_access_key,
            self.s3_secret_key,
        )
        filled = [value.strip() for value in values if value.strip()]
        if filled and len(filled) != len(values):
            missing = [
                name
                for name, value in (
                    ("S3_ENDPOINT", self.s3_endpoint),
                    ("S3_REGION", self.s3_region),
                    ("S3_BUCKET", self.s3_bucket),
                    ("S3_ACCESS_KEY", self.s3_access_key),
                    ("S3_SECRET_KEY", self.s3_secret_key),
                )
                if not value.strip()
            ]
            raise ValueError(
                "Реквизиты S3 заданы частично — либо все, либо ни одного: " + ", ".join(missing)
            )
        if not filled and self.env in {"production", "staging"}:
            raise ValueError("Не заданы реквизиты S3 при ENV=" + self.env)
        return self

    @model_validator(mode="after")
    def _validate_s3_ranges(self) -> "Settings":
        """Task 2.3 (AC-05, AC-08): fail-fast на вырожденные пределы фото.

        `S3_SIGNED_URL_TTL=0` отдал бы ссылку, истёкшую до отдачи: клиент
        показывал бы битую картинку и просил новую в цикле. TTL больше суток
        держит подпись в истории браузера дольше, чем живёт само фото.

        `MAX_PHOTO_BYTES=0` отбил бы любое фото; больше 10 МиБ возвращает
        исходный OOM контейнера (`mem_limit: 384m`) — тело целиком читается
        в память перед валидацией сигнатуры.
        """
        if not 60 <= self.s3_signed_url_ttl <= 86_400:
            raise ValueError(f"S3_SIGNED_URL_TTL вне диапазона 60..86400: {self.s3_signed_url_ttl}")
        if not 1_048_576 <= self.max_photo_bytes <= 10_485_760:
            raise ValueError(
                f"MAX_PHOTO_BYTES вне диапазона 1048576..10485760: {self.max_photo_bytes}"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def cookie_secure(self) -> bool:
        """Secure включается везде, кроме локальной разработки и тестов.

        Привязка к `is_production` оставляла staging на http: cookie уезжала
        по открытому каналу.
        """
        return self.env not in {"development", "test"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
