/**
 * Запросы ленты карточек (стори 4.5, Task 7.7; AC-01, AC-08).
 *
 * Тонкие функции `fetch` через BFF (`/api/proxy/...`), как в
 * `components/cards/card-create-form.tsx` (4.2) и `profile-photos.tsx` (2.5).
 * `@tanstack/react-query` в проекте не установлен (X-18), и установка
 * зависимостей по AGENTS.md требует согласования — поэтому кэш и инвалидация
 * остаются заботой страницы, а не библиотеки.
 *
 * `credentials: "same-origin"` обязателен: cookie сессии ставится на origin
 * страницы (D-10 стори 2.3), и без флага BFF не получил бы её.
 */

/** Место карточки в ответе (§7.2). */
export type CardPlace = { uuid: string; name: string; address_text: string };

/** Автор карточки в ответе (§7.2). */
export type CardAuthor = { uuid: string; name: string | null; age: number | null };

/** Карточка в ответе ленты (§7.2). */
export type CardRead = {
  uuid: string;
  title: string;
  description: string | null;
  category: string;
  starts_at: string;
  expires_at: string;
  /**
   * Серверное «сейчас» — якорь отсчёта окна (AC-04 стори 5б.1, NFR14).
   * Без него `CountdownBadge` считал бы остаток по часам устройства, и
   * участник с убежавшими часами видел бы «окно закрыто» раньше сервера.
   */
  server_time: string;
  capacity: number;
  filled_count: number;
  status: "draft" | "published" | "full" | "closed" | "finished" | "cancelled";
  verification_status: string;
  created_at: string;
  place: CardPlace;
  author: CardAuthor;
  chat_uuid: string | null;
  /**
   * AC-09 стори 5б.2 (D-5b.2-7, FR104): зритель в составе компании.
   *
   * `true` → «Отменить участие» (`leaveCard`), `false` → «Присоединиться»
   * (`joinCard`). Признак ПРО ЗРИТЕЛЯ, не про карточку: `filled_count`
   * говорит «сколько мест занято», а не «занял ли одно из них я».
   *
   * Вычислять на клиенте нельзя: список участников в ленте не отдаётся
   * (FR81 — состав компании виден только её членам), а `capacity=1` вообще
   * не предполагает участия через `join` (FR70: там заявки).
   */
  viewer_participates: boolean;
};

/** Конверт выдачи §4: `{items, total, cursor}`. */
export type CardFeedResponse = {
  items: CardRead[];
  total: number;
  cursor: string | null;
};

/** Параметры запроса ленты: только те, что есть в контракте §7.2. */
export type CardFeedParams = {
  category?: string | null;
  hasSeats?: boolean;
  cursor?: string | null;
};

/**
 * Загружает страницу ленты.
 *
 * Возвращает `{ok: true, data}` или `{ok: false, code}`: клиент ветвится по
 * МАШИННОМУ коду (`invalid_cursor`, `category_invalid`, `unauthenticated`),
 * а не по тексту ответа (project-context §4). Текст для человека выбирает
 * вызывающий экран — сервер не знает, где именно показывается ошибка.
 */
export async function fetchCards(
  params: CardFeedParams = {},
): Promise<{ ok: true; data: CardFeedResponse } | { ok: false; code: string }> {
  const query = new URLSearchParams();
  if (params.category) query.set("category", params.category);
  if (params.hasSeats) query.set("has_seats", "true");
  if (params.cursor) query.set("cursor", params.cursor);
  const suffix = query.toString();

  let response: Response;
  try {
    response = await fetch(`/api/proxy/cards${suffix ? `?${suffix}` : ""}`, {
      method: "GET",
      credentials: "same-origin",
      headers: { accept: "application/json" },
    });
  } catch {
    // Сеть недоступна: `code` остаётся машинным, экран покажет «повторить».
    return { ok: false, code: "network_error" };
  }

  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const code =
      typeof body === "object" && body !== null && "code" in body && typeof body.code === "string"
        ? body.code
        : `http_${response.status}`;
    return { ok: false, code };
  }

  return { ok: true, data: (await response.json()) as CardFeedResponse };
}

/**
 * Ответ на `close`/`cancel` (стори 4.6; `EventCardRef`, §7.2).
 *
 * Отдельный тип, а не `CardRead`: сервер отвечает минимальной формой (X-13),
 * и обещать клиенту полную карточку было бы ложью контракта.
 */
export type CardRef = {
  uuid: string;
  status: CardRead["status"];
  verification_status: string;
  created_at: string;
};

/**
 * Действия над карточкой автора (стори 4.6, AC-03, AC-04).
 *
 * `closeCard` — FR74 (`published → closed`), `cancelCard` — FR100 (мягкая
 * отмена, в том числе через `DELETE`). Обе возвращают `EventCardRef`, а не
 * полную карточку: после успеха экран перечитывает её сам, иначе пришлось бы
 * держать два представления одного ресурса в памяти клиента.
 *
 * `code` в ошибке машинный (`CARD_NOT_PUBLISHED`, `CARD_NOT_CANCELLABLE`,
 * `CARD_ALREADY_JOINED`): текст для человека выбирает экран, сервер не знает,
 * где именно показывается отказ (project-context §4).
 */
async function cardAction(
  uuid: string,
  action: "close" | "cancel",
): Promise<{ ok: true; data: CardRef } | { ok: false; code: string }> {
  let response: Response;
  try {
    response = await fetch(`/api/proxy/cards/${encodeURIComponent(uuid)}/${action}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { accept: "application/json" },
    });
  } catch {
    return { ok: false, code: "network_error" };
  }

  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const code =
      typeof body === "object" && body !== null && "code" in body && typeof body.code === "string"
        ? body.code
        : `http_${response.status}`;
    return { ok: false, code };
  }

  return { ok: true, data: (await response.json()) as CardRef };
}

/** AC-03 (FR74): автор закрывает набор вручную. */
export function closeCard(uuid: string) {
  return cardAction(uuid, "close");
}

/** AC-04 (FR100, R-04): мягкая отмена — тот же переход, что `DELETE`. */
export function cancelCard(uuid: string) {
  return cardAction(uuid, "cancel");
}

/**
 * Ответ `POST /requests/{uuid}/select` (стори 5б.2, AC-01).
 *
 * `chat_uuid` — идентификатор чата компании. Таблицы `chats` пока нет
 * (эпик 6), но контракт значения уже действует: `chats.uuid` будет равен
 * именно этому значению, а не сгенерирован заново.
 */
export type SelectRequestResult = { card_uuid: string; chat_uuid: string };

/**
 * Приём `cardAction` для тел `204` и `200` с разной формой (5б.2).
 *
 * `cardAction` выше жёстко типизирован `CardRef`, а здесь два маршрута
 * возвращают разное: `DELETE /join` — `204` без тела, `POST /select` —
 * `{card_uuid, chat_uuid}`. Копия разбора ошибки разошлась бы с оригиналом
 * при первой правке формата конверта, поэтому общая часть вынесена в
 * `readErrorCode`, а `fetch`-обёртка осталась на месте вызова.
 */
async function readErrorCode(response: Response): Promise<{ ok: false; code: string }> {
  const body: unknown = await response.json().catch(() => null);
  const code =
    typeof body === "object" && body !== null && "code" in body && typeof body.code === "string"
      ? body.code
      : `http_${response.status}`;
  return { ok: false, code };
}

/**
 * AC-09 стори 5б.2 (FR104): присоединение к карточке открытого набора.
 *
 * `204` не имеет тела, поэтому `data` — `null`: экран перечитывает карточку
 * через `router.refresh()` и не держит второго представления ресурса.
 *
 * Коды отказа доходят до экрана как есть (`CARD_IS_FULL`, `CARD_NOT_ACCEPTING`,
 * `ALREADY_JOINED`, `CAPACITY_ONE_USES_REQUESTS`, `network_error`): текст
 * выбирает компонент, сервер не знает, где показывается отказ.
 */
export async function joinCard(
  uuid: string,
): Promise<{ ok: true; data: null } | { ok: false; code: string }> {
  let response: Response;
  try {
    response = await fetch(`/api/proxy/cards/${encodeURIComponent(uuid)}/join`, {
      method: "POST",
      credentials: "same-origin",
      headers: { accept: "application/json" },
    });
  } catch {
    return { ok: false, code: "network_error" };
  }
  if (!response.ok) return readErrorCode(response);
  return { ok: true, data: null };
}

/**
 * AC-09 стори 5б.2 (FR104): отмена участия в открытом наборе.
 *
 * `DELETE`, а не `POST .../leave`: маршрут 5а.6 — `DELETE /cards/{uuid}/join`.
 * `404 PARTICIPATION_NOT_FOUND` доходит до экрана кодом — он же покрывает
 * случай «участия не было».
 */
export async function leaveCard(
  uuid: string,
): Promise<{ ok: true; data: null } | { ok: false; code: string }> {
  let response: Response;
  try {
    response = await fetch(`/api/proxy/cards/${encodeURIComponent(uuid)}/join`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { accept: "application/json" },
    });
  } catch {
    return { ok: false, code: "network_error" };
  }
  if (!response.ok) return readErrorCode(response);
  return { ok: true, data: null };
}

/**
 * AC-10 стори 5б.2: автор выбирает заявившегося.
 *
 * Путь — `/requests/{uuid}/select`, а не `/cards/{uuid}/select`: решение
 * принимается НАД ЗАЯВКОЙ, и автор видит её `uuid` в списке заявившихся.
 * Карточка выводится из заявки — выбрать заявку чужой карточки нельзя.
 *
 * Идемпотентности нет (D-5b.2-9): повтор даёт `409 REQUEST_NOT_PENDING`,
 * и кнопка обязана заблокироваться после первого клика.
 */
export async function selectRequest(
  requestUuid: string,
): Promise<{ ok: true; data: SelectRequestResult } | { ok: false; code: string }> {
  let response: Response;
  try {
    response = await fetch(
      `/api/proxy/requests/${encodeURIComponent(requestUuid)}/select`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { accept: "application/json" },
      },
    );
  } catch {
    return { ok: false, code: "network_error" };
  }
  if (!response.ok) return readErrorCode(response);
  return { ok: true, data: (await response.json()) as SelectRequestResult };
}
