"""Текст пакетного `INSERT` запросов подтверждения явки (стори 9.4; AC-02).

§18.2: ассерт обязан падать от реальной правки. Форма запроса НЕ наблюдаема
через публичный интерфейс: `RETURNING` возвращает пары, но проверка через БД
показала бы только «строки есть» — снятие `RETURNING` дало бы пустой список
и упало бы в интеграционном тесте, а вот `join users` отличим только по
тексту (без него `user_uuid` неоткуда взять, но интеграционный тест упадёт
на этапе сборки кортежа — здесь дешевле и точнее).

§18.3: поведение и адресность — в `test_attendance_prompts.py` и
`test_attendance_user_channel.py`.
"""

from __future__ import annotations

from app.repositories.notifications import _CREATE_ATTENDANCE_REQUESTS_SQL


class TestAttendanceRequestsSql:
    """AC-02 9.4 (D-9.4-7): один запрос отдаёт оба идентификатора."""

    def test_returns_notification_and_user_uuid(self) -> None:
        """Краснеет при снятии `RETURNING` (M5): без него воркер не адресует канал.

        Проверяются ОБА столбца: `notifications.uuid` — для `delivered_at`
        именно этой строки, `users.uuid` — для `ws:user:{uuid}`. Одного мало:
        с `notifications.uuid` без `users.uuid` публикация стала бы невозможна,
        а без `notifications.uuid` — невозможна отметка доставки.
        """
        sql = _CREATE_ATTENDANCE_REQUESTS_SQL
        assert "RETURNING" in sql.upper(), "без RETURNING пар нет (M5)"
        returning = sql.upper().split("RETURNING", 1)[1]
        assert "UUID" in returning
        # Два разных источника uuid: строка уведомления и пользователь.
        assert "NOTIFICATIONS.UUID" in returning, returning
        assert "U.UUID" in returning, returning

    def test_joins_users_for_public_uuid(self) -> None:
        """Краснеет при снятии `join users` (M6): `user_uuid` брать неоткуда.

        Второй `SELECT` по `(type, card_uuid)` отвергнут (D-9.2-5): он
        неоднозначен при нескольких уведомлениях одного типа по одной карточке.
        """
        sql = _CREATE_ATTENDANCE_REQUESTS_SQL
        assert "JOIN USERS" in sql.upper(), sql
        # CTE `inserted` возвращает УЖЕ вставленные строки, поэтому связь идёт
        # с `i.user_id`, а не с `target` (`t`).
        assert "U.ID = I.USER_ID" in sql.upper(), sql

    def test_gate_stays_inside_insert(self) -> None:
        """AC-01 9.3: gate отключения остаётся ВНУТРИ вставки (R6, M2 9.3).

        Снятие `NOT EXISTS` из `WHERE` отправило бы уведомление отключившему.
        Проверяются оба условия предиката: без `user_id` глушится тип всем,
        без `type` — все типы одному (M13).
        """
        sql = _CREATE_ATTENDANCE_REQUESTS_SQL
        assert "notification_mutes" in sql, sql
        assert "m.user_id = t.user_id" in sql, sql
        assert "m.type =" in sql, sql
