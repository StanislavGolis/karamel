/**
 * Запросы списка чатов (стори 6.6, Task 6.1; AC-01, AC-02).
 *
 * Тонкие функции `fetch` через BFF (`/api/proxy/...`) — тот же приём, что в
 * `lib/queries/cards.ts` (4.5) и `components/chat/chat-screen.tsx` (6.4).
 * `@tanstack/react-query` в проекте не установлен (X-18), и установка
 * зависимостей по AGENTS.md требует согласования: кэш и инвалидация остаются
 * заботой экрана, а контракт `{items, total, cursor}` от смены библиотеки не
 * изменится.
 *
 * `credentials: "same-origin"` обязателен: cookie сессии ставится на origin
 * страницы (D-10 стори 2.3), и без флага BFF не получил бы её.
 *
 * Разбор — по МАШИННОМУ коду (`chat_not_found`, `invalid_cursor`),
 * а не по тексту ответа (project-context §4): сервер не знает, где именно
 * показывается ошибка, и текст выбирает вызывающий экран.
 */

/** Последнее сообщение в строке списка (AC-01). */
export type ChatListLastMessage = {
  uuid: string;
  sender_uuid: string;
  body: string;
  created_at: string;
};

/** Строка списка чатов (AC-01, AC-04). */
export type ChatListItem = {
  chat_uuid: string;
  card_uuid: string;
  card_title: string;
  /** `null` — переписки ещё нет; чат ВСЁ РАВНО в списке. */
  last_message: ChatListLastMessage | null;
  unread_count: number;
  last_activity_at: string;
};

/** Конверт §4: `{items, total, cursor}`. */
export type ChatListResponse = {
  items: ChatListItem[];
  total: number;
  cursor: string | null;
};

export type ChatsResult =
  | { ok: true; data: ChatListResponse }
  | { ok: false; code: string };

const API_TIMEOUT_MS = 10_000;

/**
 * Загружает страницу списка чатов.
 *
 * Таймаут — обязательный: без него «вечная загрузка» на скелетонах читается
 * как поломка, и человек уходит. `AbortController` создаётся на каждый
 * вызов: два параллельных запроса не должны отменять друг друга.
 */
export async function fetchChats(cursor: string | null = null): Promise<ChatsResult> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/proxy/chats${query}`, {
      credentials: "same-origin",
      headers: { accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) {
      const payload = (await response.json().catch(() => null)) as { code?: unknown } | null;
      const code =
        typeof payload?.code === "string" ? payload.code : `http_${response.status}`;
      return { ok: false, code };
    }
    const data = (await response.json()) as ChatListResponse;
    return { ok: true, data };
  } catch {
    // Сеть, таймаут, битый JSON — для экрана одно и то же: «данных нет».
    return { ok: false, code: "network_error" };
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Отмечает чат прочитанным (AC-02).
 *
 * Идемпотентно на сервере: повторный вызов вернёт тот же `{unread_count: 0}`.
 * Ошибка НЕ пробрасывается: отметка прочтения — побочный эффект открытия
 * чата, и падение сети на ней не должно ломать переписку. Возвращается
 * признак успеха — для теста и для будущей инвалидации кэша.
 */
export async function markChatRead(chatUuid: string): Promise<boolean> {
  try {
    const response = await fetch(
      `/api/proxy/chats/${encodeURIComponent(chatUuid)}/read`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { accept: "application/json" },
      },
    );
    return response.ok;
  } catch {
    return false;
  }
}
