"""Константы 7-дневного окна удаления аккаунта (стори 10.2; NFR37).

**Почему отдельный модуль.** Окно нужно и сервису
(`services/account_deletion_service.py`), и репозиторию
(`repositories/account_deletion.py`), а сервис импортирует репозиторий.
Объявление константы в сервисе дало бы циклический импорт
`service → repository → service`. Модуль-лист без зависимостей рвёт круг.

**Почему не `Settings`** (D-10.2-4). NFR37 фиксирует 7 суток как продуктовое
требование, а не настройку среды: `Settings`-переменная позволила бы на
стенде поставить 0 дней и «доказать» удаление, сломав требование.
"""

from __future__ import annotations

from datetime import timedelta

__all__ = ["DELETION_WINDOW", "DELETION_WINDOW_DAYS"]

#: NFR37: ровно 7 суток. Менять только правкой PRD.
DELETION_WINDOW_DAYS = 7

#: Окно в виде `timedelta` — единственное место, где оно превращается в срок.
DELETION_WINDOW = timedelta(days=DELETION_WINDOW_DAYS)
