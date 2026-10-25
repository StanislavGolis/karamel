"""Реэкспорт моделей (Task 2.3).

Alembic autogenerate видит только модели, импортированные здесь: `env.py` берёт
`Base.metadata` из этого модуля. Новая модель без реэкспорта = миграция не создаст её.

Стори 2.2 (Task 1.6): `ProfilePhoto` добавлена в реэкспорт — без этого
`alembic check` показал бы пустой autogenerate, а `profile_photos` не
появилась бы ни в одной миграции.

Стори 4.1 (Task 2.8): `EventCard` добавлена по той же причине — без реэкспорта
`0008` не создала бы `event_cards`, а `alembic check` молчал бы о расхождении.

Стори 5а.1 (Task 1.4, 2.6): `JoinRequest` добавлена по той же причине —
без реэкспорта `0012` не создала бы `join_requests`, а `alembic check`
молчал бы о расхождении (дефект 2.2).

Стори 3.1 (Task 3.7): `Place` добавлена по той же причине — без реэкспорта
`0007` не создала бы `places`, а `alembic check` молчал бы о расхождении.

Стори 7.1 (Task 1.4): `Attendance` добавлена по той же причине — без
реэкспорта `0020` не создала бы `attendance`, а `alembic check` молчал бы
о расхождении (дефект 2.2, AC-08).

Стори 7.3 (Task 1.6): `Notification` добавлена по той же причине — без
реэкспорта `0021` не создала бы `notifications`, а `alembic check` молчал бы
о расхождении.

Стори 9.1 (Task 5.7): `SupportTicket` добавлена по той же причине — без
реэкспорта `alembic check` показал бы «Detected removed table 'support_tickets'»
(или не увидел бы её вовсе), и модель с миграцией разошлись бы молча.

Стори 8.6 (Task 2.1): `AccessLog` добавлена по той же причине — без
реэкспорта `alembic check` показал бы «Detected removed table 'access_logs'»,
то есть модель и миграция разошлись бы молча (проверено прогоном в DS 8.6).
"""

from app.models.access_log import AccessLog
from app.models.attendance import Attendance
from app.models.base import Base, TimestampMixin
from app.models.chat import Chat
from app.models.enums import (
    AttendanceStatus,
    AuthProvider,
    CardVerificationStatus,
    EventCardStatus,
    Gender,
    JoinRequestStatus,
    NotificationType,
    PhotoStatus,
    PlaceCategory,
    ReportResolution,
    ReportTargetType,
    SanctionType,
    TicketStatus,
    UserRole,
    UserStatus,
)
from app.models.event_card import EventCard
from app.models.join_request import JoinRequest
from app.models.message import Message
from app.models.notification import Notification
from app.models.notification_mute import NotificationMute
from app.models.place import Place
from app.models.profile import Profile
from app.models.profile_photo import ProfilePhoto
from app.models.refresh_token import RefreshToken
from app.models.report import Report
from app.models.sanction import Sanction
from app.models.support_ticket import SupportTicket
from app.models.user import User

__all__ = [
    "AccessLog",
    "Attendance",
    "AttendanceStatus",
    "AuthProvider",
    "Base",
    "CardVerificationStatus",
    "Chat",
    "EventCard",
    "EventCardStatus",
    "Gender",
    "JoinRequest",
    "JoinRequestStatus",
    "Message",
    "Notification",
    "NotificationMute",
    "NotificationType",
    "PhotoStatus",
    "Place",
    "PlaceCategory",
    "Profile",
    "ProfilePhoto",
    "RefreshToken",
    "Report",
    "ReportResolution",
    "ReportTargetType",
    "Sanction",
    "SanctionType",
    "SupportTicket",
    "TicketStatus",
    "TimestampMixin",
    "User",
    "UserRole",
    "UserStatus",
]
