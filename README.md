# Карамель 2.0

Дейтинг-сервис для отношений с экономикой внимания: выборка из 20 анкет
и 5 лайков в день вместо бесконечной прокрутки. Пилот — Томск.

## Стек

| Слой | Технология |
|---|---|
| Web | Next.js 16 (App Router, route groups), React 19.2, TypeScript strict |
| Стили | Tailwind CSS v4 (PostCSS), палитра «Тёплая бумага» |
| API | FastAPI, SQLAlchemy 2 async + asyncpg |
| Данные | PostgreSQL 16, Redis 7 |
| Инфраструктура | Docker Compose, GitHub Actions |

## Локальная разработка (Docker)

```bash
cp .env.example .env
docker compose up --build      # первый прогон
docker compose ps              # 5 сервисов
curl localhost:8000/health     # {"status":"ok"}
```

**Порты привязаны к `127.0.0.1`** — снаружи БД и Redis недоступны.
На сервере наружу открывают только `80`/`443` через reverse proxy.

## Локальная разработка (без Docker)

Docker не всегда доступен (вложенный Docker требует `CAP_SYS_ADMIN`).
Тогда сервисы запускаются нативно:

```bash
# API
cd services/api
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --reload

# Web
cd apps/web
npm ci && npm run dev
```

Требуются локальные PostgreSQL и Redis с параметрами из `.env.example`.

### Проверки качества (без Docker)

Зависимости проверок объявлены в `[project.optional-dependencies].dev`
и ставятся той же командой `pip install -e ".[dev]"`. Отдельной установки
не требуется.

```bash
cd services/api
.venv/bin/ruff check .            # линт, включая alembic/
.venv/bin/ruff format --check .   # форматирование
.venv/bin/python -m mypy --strict app
```

`mypy` запускается **через `python -m`**, а не как `.venv/bin/mypy`: пакет
собирается с компилированным ядром (`mypy/__init__.cpython-313-*.so`), и
консольный скрипт в этом случае не создаётся. Отсутствие `.venv/bin/mypy`
не означает, что зависимости не установлены — проверять надо
`.venv/bin/python -m mypy --version`.

## Деплой

Этот `docker-compose.yml` — окружение развёртывания на одном хосте
с жёсткими лимитами памяти (рядом работают другие сервисы).
Прод-вариант с reverse proxy — стори 8.6.

```bash
git clone <repo> /opt/karamel && cd /opt/karamel
cp .env.example .env   # заменить пароли
docker compose up -d --build
```

## Миграции

Схема БД меняется **только** миграциями Alembic (`services/api/alembic/versions/`).
`create_all()` не используется нигде: он строит таблицы из моделей и не проверяет
то, что реально применяется к базе.

Alembic лежит в `services/api/alembic.ini`, миграции — в `services/api/alembic/versions/`.
Файл миграции называется `NNNN_описание.py` (`0001_create_users_and_refresh_tokens.py`).

### Обязательная переменная окружения

Alembic берёт URL из `app.core.config.settings.database_url`, то есть из `DATABASE_URL`.
Статического URL в `alembic.ini` нет — иначе пароль попал бы в git.

```bash
cd services/api
export DATABASE_URL='postgresql+asyncpg://karamel:karamel_pass@127.0.0.1:5432/karamel_db'
```

Без `DATABASE_URL` берётся dev-значение по умолчанию из `app/core/config.py`
(`127.0.0.1:5432/karamel_db`). Внутри Docker host — `postgres`, а не `127.0.0.1`:
в контейнере `api` переменная уже задана через `docker-compose.yml`.

### Команды

```bash
cd services/api

python -m alembic upgrade head      # применить все миграции
python -m alembic current           # текущая ревизия, ожидается 0001 (head)
python -m alembic downgrade -1      # откатить последнюю
python -m alembic downgrade base    # откатить всё
python -m alembic check             # расхождения моделей и схемы (пусто = ок)

# новая миграция: ревизия — четыре цифры, описание в snake_case латиницей
python -m alembic revision --autogenerate --rev-id 0002 -m "add profiles"
```

После автогенерации файл **читается и правится руками**: `downgrade()` должен реально
откатывать изменения, порядок удаления таблиц — обратный порядку создания.

### Проверка в CI

Job `migrations` выполняет цикл `upgrade head → downgrade base → upgrade head`
и `alembic check` на чистом `postgres:16`. Любая ошибка валит job.

## Вход через Яндекс ID и VK ID

### Где взять реквизиты

| Провайдер | Где создать приложение | Что нужно |
|---|---|---|
| Яндекс ID | <https://oauth.yandex.ru/client/new> | «ID приложения» → `YANDEX_CLIENT_ID`, «Пароль приложения» → `YANDEX_CLIENT_SECRET` |
| VK ID | <https://id.vk.ru/about/business/go> | «ID приложения» → `VK_CLIENT_ID`, «Защищённый ключ» → `VK_CLIENT_SECRET` |

`redirect_uri` регистрируется у провайдера **дословно**, иначе провайдер вернёт
`redirect_uri mismatch` ещё до выдачи кода:

```
{API_URL}/api/v1/auth/yandex/callback
{API_URL}/api/v1/auth/vk/callback
```

`API_URL` берётся из окружения: локально `http://127.0.0.1:8000`, на стенде — публичный
адрес. Значение подставляется в `redirect_uri` из настроек, а не из заголовка `Host`:
заголовок подделывается клиентом, и тогда код уехал бы на чужой адрес.

### Переменные окружения

```bash
YANDEX_CLIENT_ID=
YANDEX_CLIENT_SECRET=
VK_CLIENT_ID=
VK_CLIENT_SECRET=
# Ключ подписи access-токена (HS256). Сгенерировать: openssl rand -hex 32
JWT_SECRET=dev_only_secret
OAUTH_STATE_TTL_SECONDS=600
```

Пустые реквизиты и `JWT_SECRET=dev_only_secret` допустимы **только** при
`ENV=development`. В `staging`/`production` приложение падает на старте с `ValueError`:
иначе пустой `client_id` проявился бы на первом живом входе, то есть уже в проде,
а дефолтный ключ подписи позволил бы выпустить токен кому угодно.

### Эндпоинты

| Метод | Путь | Поведение |
|---|---|---|
| `GET` | `/api/v1/auth/yandex/start` | `302` на `oauth.yandex.ru/authorize` |
| `GET` | `/api/v1/auth/vk/start` | `302` на `id.vk.ru/authorize` с PKCE (`S256`) |
| `GET` | `/api/v1/auth/{yandex,vk}/callback` | `302 {WEB_ORIGIN}/app` + cookie сессии |

Ошибки наружу отдаются только нашими кодами: `oauth_denied` (отказ у провайдера),
`oauth_failed` (ошибка обмена или профиля), `invalid_state` (`400`, защита CSRF),
`invalid_callback` (`400`, у VK нет `device_id`). Текст провайдера (`error_description`)
не показывается: он может содержать `client_id` и `redirect_uri`.

### Ограничения текущей реализации

- Склейки учётных записей между провайдерами нет: вошедший через Яндекс, а потом
  через VK получит две учётки. Это продуктовое решение пост-MVP, см. `deferred-work.md`.
- Полный цикл сессии (BFF-прокси, автообновление access, ротация refresh, выход, бан) —
  стори 1.5. Здесь выдаётся **одна** сессия на момент входа.
- Rate limiting на `/auth/*/start` не реализован (NFR16), лимиты — стори 1.4/8.x.
- Имена полей профиля провайдеров подтверждаются при первом живом входе.

### Живая проверка

```bash
curl -sSI 'http://127.0.0.1:8000/api/v1/auth/yandex/start' | grep -i location
curl -sSI 'http://127.0.0.1:8000/api/v1/auth/vk/start' | grep -i location
```

Ожидается `302` на страницу провайдера. Признак успешного входа: редирект на `/app`,
в логах `oauth_callback_success` с `user_uuid` и без персональных данных,
`count(*) from users where provider='yandex'` вырос на 1, повторный вход счётчик не меняет.

## Вход по номеру телефона (SMS)

Пользователь без аккаунта в Яндексе и VK вводит номер, получает код в SMS и
входит тем же способом, что и через OAuth: одна сессия, те же cookie.

### Где взять реквизиты

| Что | Где | Переменная |
|---|---|---|
| API-ключ SMS.ru | <https://sms.ru/my/api> — личный кабинет | `SMSRU_API_ID` |
| Перец для хеша кодов | генерируется: `openssl rand -hex 32` | `SMS_CODE_SECRET` |

Стоимость одного сообщения — примерно **1–2 ₽** (зависит от оператора и объёма).
Ключ выдаётся физлицу без ОКУП и постоплаты; SMS.ru выбран как российский
провайдер, то есть данные остаются в РФ (152-ФЗ).

### Переменные окружения

```bash
# smsru — боевой провайдер, stub — пишет код в лог (только dev/test)
SMS_PROVIDER=stub
SMSRU_API_ID=
# Перец HMAC-SHA256. Короче 32 символов — падение на старте.
SMS_CODE_SECRET=
```

Для локальной разработки: `SMS_PROVIDER=stub` и пустой `SMSRU_API_ID`.
Код печатается в лог с префиксом `SMS_STUB` — реальное SMS не отправляется,
деньги не тратятся. В `staging`/`production` заглушка запрещена валидатором,
а пустой `SMSRU_API_ID` при `SMS_PROVIDER=smsru` роняет приложение на старте.

### Формат номера

Принимаются `+7XXXXXXXXXX`, `8XXXXXXXXXX`, `7XXXXXXXXXX`, а также записи
с пробелами, дефисами, скобками и точками. В БД и в Redis номер всегда `+7`
и 10 цифр (E.164). Чужие коды стран отклоняются: `400 invalid_phone`.

### Эндпоинты

| Метод | Путь | Поведение |
|---|---|---|
| `POST` | `/api/v1/auth/phone/request` | `200 {expires_in, resend_after}`, SMS отправлено |
| `POST` | `/api/v1/auth/phone/verify` | `200 {user_uuid, is_new}` + cookie сессии |

Коды ошибок (все — `{"code": "..."}`): `invalid_phone` (400), `rate_limited`
(429 с `retry_after` — и для лимита окна, и для cooldown повторной отправки),
`storage_unavailable`
(503), `sms_unavailable` (503), `code_invalid` (401 + `attempts_left`),
`code_expired` (410), `code_attempts_exceeded` (401), `phone_taken` (409),
`user_blocked` (403).

### Ограничения текущей реализации

- Номер, уже привязанный к учётке Яндекс/VK, даёт `409 phone_taken`: автосклейки
  нет, потому что у OAuth-учётки нет подтверждения владения телефоном.
- Лимиты: 3 запроса за 10 минут на IP и на номер, cooldown 60 с, 5 попыток проверки.
  Счётчики живут в Redis и переживают перезапуск процесса.
- 18+ и согласие на обработку ПД — стори 1.6; здесь учётка создаётся с
  `age_confirmed=false` и `consent_given_at=NULL`.
- Полный цикл сессии (BFF, автообновление, ротация, выход) — стори 1.5.

### Живая проверка

```bash
# запрос кода (в логе появится SMS_STUB с кодом)
curl -sS -X POST http://127.0.0.1:8000/api/v1/auth/phone/request \
  -H 'content-type: application/json' -d '{"phone":"+79001234567"}'
# проверка кода
curl -sS -X POST http://127.0.0.1:8000/api/v1/auth/phone/verify \
  -H 'content-type: application/json' -d '{"phone":"+79001234567","code":"123456"}' -i
```

Признак успеха: `200 {"user_uuid":...,"is_new":true}`, две `Set-Cookie`
(`karamel_access`, `karamel_refresh`) с `HttpOnly`, повторный вход тем же
номером даёт `is_new=false` и тот же `user_uuid`.

## Проверки

```bash
cd apps/web && npm run lint && npx tsc --noEmit && npm run build
cd services/api && ruff check . && ruff format --check . && python -m mypy app && pytest
```

Интеграционные тесты требуют `DATABASE_URL`; без него они пропускаются с причиной,
а не зеленеют молча:

```bash
cd services/api
export DATABASE_URL='postgresql+asyncpg://karamel:karamel_pass@127.0.0.1:5432/karamel_db'
pytest -q
```
