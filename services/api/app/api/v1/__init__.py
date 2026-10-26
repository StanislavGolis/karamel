"""Сборка роутеров версии v1 (Task 6.5).

Префикс `/api/v1` добавляется в `main.py` ровно один раз: дублирование
префикса в роутере и в `include_router` даёт `/api/v1/api/v1/...`,
который выглядит рабочим в тестах на конкретный путь и ломается в бою.
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.cards import router as cards_router
from app.api.v1.chats import router as chats_router
from app.api.v1.join_requests import requests_router as join_requests_own_router
from app.api.v1.join_requests import router as join_requests_router
from app.api.v1.me import router as me_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.moderation import router as moderation_router
from app.api.v1.moderation_reports import router as moderation_reports_router
from app.api.v1.moderation_support import router as moderation_support_router
from app.api.v1.moderation_users import router as moderation_users_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.phone_auth import router as phone_auth_router
from app.api.v1.places import router as places_router
from app.api.v1.profiles import router as profiles_router
from app.api.v1.reports import router as reports_router
from app.api.v1.support import router as support_router
from app.api.v1.ws import router as ws_router

api_router = APIRouter()
api_router.include_router(auth_router)
# Стори 1.4: `/auth/phone/request` и `/auth/phone/verify`. Отдельный роутер,
# а не ветка в `auth.py`: другой провайдер и другой набор кодов ошибок.
api_router.include_router(phone_auth_router)
# Стори 2.1: `GET /profiles/me` — чтение собственной анкеты. Создание и правка
# анкеты появятся в 2.3; маршрута `/profiles/{uuid}` здесь нет намеренно (Task 6.5).
api_router.include_router(profiles_router)
# Стори 8.2: `POST /reports` — жалоба на собеседника в диалоге. Отдельный
# роутер: объект жалобы — ДИАЛОГ, а не карточка, и адресация через `chat_uuid`
# (7.4 адресует пару явки через `card_uuid`, D-23).
api_router.include_router(reports_router)
# Стори 3.2: запись справочника мест (`POST`/`PATCH`/`DELETE`) под гардом
# `ModeratorUser`. Стори 3.3 добавила чтение: `GET /places` с `category`,
# `cursor` и `include_inactive` (R-03, R-06). `include_inactive` объявлен
# только в зависимости `get_places_reader` — второй источник в сигнатуре
# роутера дал бы два ответа на один вопрос «видит ли он скрытые».
api_router.include_router(places_router)
# Стори 4.2: создание, правка черновика и публикация карточки события.
# `GET /cards` (лента, 4.5) здесь намеренно НЕТ: его отсутствие проверяется
# тестом поверхности API (`test_routes_no_numeric_ids`).
api_router.include_router(cards_router)
# Стори 4.3: очередь модерации карточек и решения по ним. Отдельный роутер,
# а не ветки в `cards.py`: другой гард (`ModeratorUser` на всех маршрутах),
# другой ресурс (`/moderation/cards`) и другой набор ошибок.
api_router.include_router(moderation_router)
# Стори 5а.2: подача заявки на карточку режима «заявки» (вместимость 1).
# `POST /cards/{uuid}/requests` — путь ресурса карточки, но другой гард
# (`ConsentUser` + технический rate limit) и другой набор кодов ошибок.
# `DELETE /requests/{uuid}` и `GET /my/requests` — 5а.3, список заявившихся — 5а.4.
api_router.include_router(join_requests_router)
# Стори 5а.3: `DELETE /requests/{uuid}` и `GET /my/requests` — тот же модуль,
# второй префикс. Один модуль на домен заявок: `_error`/`_domain_error` и
# коды ошибок живут в одном месте, иначе появился бы второй источник правды.
api_router.include_router(join_requests_own_router)
# Стори 6.4: `/chats/{uuid}/messages` — отправка и история. Отдельный роутер,
# а не ветки в `cards.py`: другой ресурс (`/chats`), другой гард (участие в
# компании) и другой набор кодов ошибок (`chat_not_found`).
api_router.include_router(chats_router)
# Стори 6.5: `WS /ws` — доставка сообщений в реальном времени. Не `HTTP`-роутер:
# регистрируется тем же `include_router` (FastAPI различает по декоратору),
# префикс `/api/v1` приходит из `main.py` как у остальных.
api_router.include_router(ws_router)
# Стори 7.5: `GET /moderation/users/{uuid}/attendance` — история явок
# пользователя для модератора (FR108, NFR39). Отдельный модуль, а не ветка
# в `moderation.py`: другой ресурс (`/moderation/users`) и другой набор кодов
# ошибок (`user_not_found`). Гард тот же (`ModeratorUser`), второго правила
# доступа не появляется.
api_router.include_router(moderation_users_router)

# Стори 8.3: `GET /moderation/reports` и `.../{uuid}/fragment` — очередь жалоб
# и фрагмент переписки. Отдельный роутер, а не ветка `moderation.py`: другой
# ресурс (`/moderation/reports`) и свой набор кодов (`invalid_cursor`,
# `report_not_found`). Только чтение — `commit` не вызывается.
api_router.include_router(moderation_reports_router)

# Стори 9.3: `GET`/`PATCH /notifications/settings` — настройки канала
# уведомлений (FR46). Отдельный роутер, а не ветка `profiles.py`: другой
# ресурс (`/notifications`) и другой гард (`CurrentUser`, не `ConsentUser` —
# настройка не раскрывает ПД, D-9.3-9). Роута СПИСКА уведомлений здесь НЕТ
# намеренно (D-9.3-10, Y-8.4-2 вне 9.3).
api_router.include_router(notifications_router)

# Стори 9.5: `POST`/`GET /support/tickets` — создание обращения и список
# своих (FR55, FR56). Отдельный роутер, а не ветка `notifications.py`:
# другой ресурс (`/support`) и другой набор кодов (`body_required`,
# `rate_limited`, `invalid_cursor`). Гард — `CurrentUser` (D-9.5-3):
# обращение не раскрывает ПД третьих лиц (прецедент D-9.3-9).
# Маршрута `GET /support/tickets/{uuid}` здесь НЕТ намеренно (D-9.5-2):
# потребителя нет, роут «на будущее» дал бы ложный контракт.
# Ответ модератора и очередь обращений — 9.6, отдельный роутер под
# `ModeratorUser` (`/moderation/support/*`). Уведомления в 9.5 не создаются
# (D-9.5-13): `support.replied` уже в перечне 11 типов, потребитель — 9.6.
api_router.include_router(support_router)

# Стори 9.6: `GET /moderation/support/tickets` и
# `POST /moderation/support/tickets/{uuid}/reply` — очередь обращений и ответ
# модератора (FR57, NFR52). Отдельный модуль, а не ветка `support.py`: другой
# гард (`ModeratorUser` против `CurrentUser`) и другой набор кодов
# (`ticket_not_found`, `ticket_already_answered`). Тот же довод, что у
# `/moderation/reports` (8.3) и `/moderation/users` (7.5).
api_router.include_router(moderation_support_router)

# Стори 10.2: `POST`/`GET`/`DELETE /me/delete` — 7-дневное окно удаления аккаунта
# (FR10, NFR37, NFR40, NFR41). Отдельный модуль, а не ветка в `profiles.py`:
# другой ресурс (`/me/delete` — операция над УЧЁТКОЙ, анкета — про человека) и
# другой набор кодов (`deletion_already_requested`). Расширять `GET /profiles/me`
# полем `deletion_*` отказано (D-10.2-6): один ответ не смешивает две оси (§6).
api_router.include_router(me_router)

# Стори 10.5: `GET /metrics` — метрики пилота для кабинета администратора
# (AC-01…AC-05). Отдельный модуль, а не ветка `moderation.py`: другой ресурс
# (метрики пилота, а не очередь карточек) и свой набор кодов
# (`invalid_period`, `period_too_long`). Гард тот же — `ModeratorUser`
# (D-10.5-1): отдельной роли `admin` в `UserRole` нет и не заводится.
api_router.include_router(metrics_router)
