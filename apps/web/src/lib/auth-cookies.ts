/**
 * Имена session-cookie (Task 9.1; AC-01).
 *
 * Дублирование имён между TS и Python неизбежно без кодогенерации.
 * Оба места помечены ссылкой на AC-01: сервер задаёт cookie в
 * `services/api/app/services/session_service.py`, клиент читает их здесь.
 * Расхождение имён — тихая поломка: гарда «всегда редиректит» или
 * BFF «никогда не обновляет сессию».
 */
export const ACCESS_COOKIE_NAME = "karamel_access";
export const REFRESH_COOKIE_NAME = "karamel_refresh";
