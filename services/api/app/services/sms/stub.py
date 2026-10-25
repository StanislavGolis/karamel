"""Dev-заглушка SMS-провайдера (Task 5.6).

Разрешена только при `ENV=development|test`: код печатается в лог, то есть
в боевом окружении это была бы утечка фактора входа в журнал контейнера.
Валидатор `_validate_secrets` не даст включить `SMS_PROVIDER=stub` вне dev.

Зачем нужна: без неё локальная разработка и интеграционные тесты требуют
реального `SMSRU_API_ID` и тратят деньги на каждое сообщение.
"""

from __future__ import annotations

import structlog

from app.core.config import settings


class StubSmsProvider:
    """Пишет текст сообщения в лог вместо отправки.

    Логгер отдельный (`sms_stub`, не `oauth`): фильтр `RedactingOAuthLogger`
    вырезал бы `msg` как неразрешённое поле, и код было бы негде прочитать.
    В dev/test уровень WARNING, поэтому заглушка видна в консоли без
    переключения всего логгера на DEBUG.
    """

    async def send_code(self, phone: str, text: str) -> None:
        """Task 5.6: вместо SMS — строка в логе с префиксом `SMS_STUB`.

        `phone` печатается маскированно: заглушка не должна приучать разработчика
        к тому, что полные номера в логах — норма (AC-07).
        """
        masked = f"{phone[:2]}***{phone[-2:]}" if len(phone) > 4 else "***"
        # CR 1.4, Решение 4: `test` разрешён наравне с `development`.
        # Валидатор `config.py` пропускал stub для `ENV=test`, а эта проверка
        # падала — CI с `ENV=test` + `SMS_PROVIDER=stub` получал 500 на любом
        # `/request`, и покрытие телефонного входа в CI было нулевым.
        # В `test` лог-уровень WARNING и код попадает в лог теста, не в прод-журнал.
        if settings.env not in {"development", "test"}:
            # В test/staging заглушка писала бы полный код в лог отдельным
            # логгером, в обход `RedactingOAuthLogger` (он привязан к префиксу
            # `oauth`). AC-07 требует, чтобы кода не было в логах вообще.
            raise RuntimeError("SMS_PROVIDER=stub допустим только при ENV=development или test")
        structlog.get_logger("sms_stub").warning("SMS_STUB", phone=masked, msg=text)
