"use client";

/**
 * Экран чата компании (стори 6.4, Task 6.1; AC-01, AC-02, AC-04).
 *
 * **Почему `useEffect` + `fetch`, а не TanStack Query** (D-6.4-7): пакет
 * `@tanstack/react-query` в проекте НЕ установлен (долг X-18), а установка
 * зависимостей по AGENTS.md требует согласования с пользователем. Тот же
 * выбор зафиксирован в `lib/queries/cards.ts`: кэш и инвалидация остаются
 * заботой экрана, а не библиотеки. Курсорная пагинация и «перечитать после
 * отправки» реализованы руками, поэтому смена библиотеки позже не изменит
 * контракт: компонент работает с теми же `{items, next_cursor}`.
 *
 * **Идемпотентность отправки (NFR18, AC-04).** `client_message_id`
 * генерируется `crypto.randomUUID()` ДО запроса и НЕ пересоздаётся при
 * ретрае: повтор с тем же идентификатором сервер вернёт `201` с тем же
 * `uuid` вместо второго сообщения. Это контракт для 6.5 (WS) — тот же
 * приём переживёт переключение транспорта.
 *
 * **Скелетоны, а не спиннеры** (UX-правило проекта): спиннер по центру
 * сдвигает контент и читается как «зависло»; скелетон держит габариты.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ConnectionIndicator } from "@/components/chat/connection-indicator";
import { SystemMessage } from "@/components/chat/system-message";
import { markChatRead } from "@/lib/queries/chats";
import { createWsClient, type ConnectionState } from "@/lib/ws-client";

/** Сообщение в ответе `GET /api/v1/chats/{uuid}/messages`. */
export type ChatMessage = {
  uuid: string;
  /**
   * `null` у системного сообщения (стори 6.7, AC-04): автора-пользователя нет.
   * Выравнивание «своё/чужое» считается только для несистемных — иначе
   * `null === viewerUuid` было бы ложью, а `null !== viewerUuid` рисовало бы
   * системную строку как чужую реплику с аватаром.
   */
  sender_uuid: string | null;
  body: string;
  created_at: string;
  /** Стори 6.7 (AC-04): признак системного сообщения. */
  is_system?: boolean;
};

type MessagesPage = {
  items: ChatMessage[];
  next_cursor: string | null;
  /**
   * X-6.5-5 (chore P-7): курсор ПОСЛЕДНЕГО элемента страницы.
   *
   * `next_cursor` для догрузки после реконнекта не годится: он `null` на
   * последней странице, а именно там и находится «хвост», который надо
   * догрузить. Формат курсора непрозрачен — клиент только возвращает его
   * серверу, второй источник истины не появляется.
   */
  last_cursor?: string | null;
};

const API_TIMEOUT_MS = 10_000;

/** ISO 8601 UTC → «19:34» в локальной зоне читателя. */
function timeOf(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

type ChatScreenProps = {
  chatUuid: string;
  /** `uuid` текущего пользователя: свои сообщения выравниваются вправо. */
  viewerUuid: string;
  /**
   * Адрес WS-эндпоинта. По умолчанию — ТОТ ЖЕ origin и `/api/v1/ws`.
   *
   * Same-origin обязателен: cookie `karamel_access` имеет `SameSite=Lax` и
   * на cross-origin handshake не уезжает (Task 2.1). Проп существует ради
   * тестов: в jsdom нет `window.location`, пригодного для реального сокета.
   */
  wsUrl?: string;
};

/** Адрес WS по умолчанию: `ws(s)://<origin>/api/v1/ws`. */
function defaultWsUrl(): string {
  if (typeof window === "undefined") return "";
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/api/v1/ws`;
}

export function ChatScreen({ chatUuid, viewerUuid, wsUrl }: ChatScreenProps) {
  const [items, setItems] = useState<ChatMessage[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  /**
   * `client_message_id` ТЕКУЩЕЙ неудачной попытки (NFR18).
   *
   * Живёт в `useRef`, а не в состоянии: смена значения не должна вызывать
   * перерисовку, а ретрай обязан переиспользовать ТОТ ЖЕ идентификатор —
   * иначе сервер увидит новое сообщение и создаст дубль.
   */
  const pendingClientId = useRef<string | null>(null);

  /**
   * Состояние WS-соединения (стори 6.5, AC-03). `offline` — стартовое:
   * до первого `open` индикатор обязан показывать «нет связи», а не
   * «всё хорошо» (иначе при мёртвом сокете экран врёт).
   */
  const [connection, setConnection] = useState<ConnectionState>("offline");

  /**
   * Адрес WS считается ОДИН раз при монтировании: пересчёт на каждый рендер
   * пересоздавал бы строку и (через зависимость эффекта) рвал соединение.
   */
  const [resolvedWsUrl] = useState(() => wsUrl ?? defaultWsUrl());

  /**
   * Контроллер запроса истории (X-6.4-1, закрыто в Chore P-5).
   *
   * До правки `fetch` жил до собственного таймаута: уход со страницы или
   * смена чата оставляли запрос в полёте. Состояние защищал флаг `active`,
   * поэтому утечки памяти не было — но соединение и трафик тратились зря,
   * а ответ на СТАРЫЙ `chatUuid` мог прийти после монтирования нового.
   */
  const historyAbort = useRef<AbortController | null>(null);

  /**
   * Курсор ХВОСТА загруженной истории (X-6.5-5, chore P-7).
   *
   * Хранится в `useRef`, а не в состоянии: значение нужно только обработчику
   * реконнекта и не влияет на разметку — обновление через `setState`
   * перерисовывало бы весь список сообщений на каждую страницу.
   *
   * Источник — `last_cursor` из ответа (а не пересчёт из `items`): формат
   * курсора непрозрачен и принадлежит серверу, второй источник истины на
   * клиенте разошёлся бы при смене формата.
   */
  const tailCursor = useRef<string | null>(null);

  const loadPage = useCallback(
    async (nextCursor: string | null, append: boolean) => {
      const query = nextCursor ? `?cursor=${encodeURIComponent(nextCursor)}` : "";
      // Предыдущий запрос отменяется: две страницы одного чата не должны
      // конкурировать за `setItems`, иначе порядок зависел бы от того, кто
      // ответил первым.
      historyAbort.current?.abort();
      const controller = new AbortController();
      historyAbort.current = controller;
      // Таймаут — ТАЙМЕРОМ на тот же контроллер, а не `AbortSignal.any`
      // (Chore P-5, поймано тестом): `AbortSignal.any` отсутствует в jsdom 25
      // и в Safari < 17.4, и его отсутствие бросало TypeError ДО `fetch` —
      // запрос не уходил вовсе, а тест видел «вечную загрузку». Один
      // контроллер покрывает оба условия: отмену и предел ожидания.
      const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
      try {
        const response = await fetch(
          `/api/proxy/chats/${encodeURIComponent(chatUuid)}/messages${query}`,
          {
            credentials: "same-origin",
            headers: { accept: "application/json" },
            signal: controller.signal,
          },
        );
        if (response.status === 404) {
          setNotFound(true);
          return;
        }
        if (!response.ok) {
          setError("Не удалось загрузить переписку. Обновите страницу.");
          return;
        }
        const page = (await response.json()) as MessagesPage;
        setItems((previous) => (append ? [...previous, ...page.items] : page.items));
        setCursor(page.next_cursor);
        // X-6.5-5 (chore P-7): запоминаем курсор ХВОСТА. Обновляется и при
        // первой загрузке, и при догрузке «Показать ещё»: обе двигают конец
        // известной истории вперёд, и реконнект обязан читать ровно после
        // последнего показанного сообщения, а не с начала.
        if (page.last_cursor) tailCursor.current = page.last_cursor;
        setError(null);
        // Стори 6.6 (Task 6.6, AC-02): открытие чата = прочтение. Отметка
        // ставится ТОЛЬКО на первой странице (`append === false`): догрузка
        // истории «Ещё» не является новым открытием и не должна повторно
        // трогать `read_at` (там и так 0 строк — идемпотентность).
        //
        // Запрос НЕ ожидается (`void`): переписка уже на экране, и падение
        // отметки (сеть, 5xx) не имеет права превратить показанный чат в
        // ошибку. `markChatRead` не бросает — возвращает `false`, который
        // здесь не нужен: счётчик в списке обновится при следующем открытии
        // списка, а источник истины — `read_at` в БД.
        if (!append) {
          void markChatRead(chatUuid);
        }
      } catch (error) {
        // Отмена — не ошибка: сообщение «проверьте связь» на уходе со
        // страницы было бы ложью и мигало бы при каждом переключении.
        if (error instanceof DOMException && error.name === "AbortError") return;
        setError("Не удалось загрузить переписку. Проверьте связь.");
      } finally {
        // Таймер снимается ВСЕГДА: без этого он держал бы контроллер живым
        // до `API_TIMEOUT_MS` и мог прервать следующий запрос уже после
        // успешного ответа предыдущего.
        clearTimeout(timeoutId);
      }
    },
    [chatUuid],
  );

  /**
   * Догрузка ХВОСТА после реконнекта WS (X-6.5-5, chore P-7).
   *
   * **Зачем.** Пока сокет был разорван, сервер продолжал писать сообщения.
   * События за это окно клиент не получил и не получит: Redis Pub/Sub не
   * хранит историю. Без догрузки подтверждение явки (эпик 7) визуально
   * «теряется» — человек видит старый список и не знает, что ответ уже дан.
   *
   * **Почему по курсору, а не перечитыванием первой страницы.** История
   * листается ВПЕРЁД от курсора (AC-02), поэтому первая страница вернула бы
   * начало переписки, а не пропущенный хвост. Курсор берётся из
   * `last_cursor` последнего ответа — формат остаётся серверным.
   *
   * **Дедуп по `uuid`.** Между обрывом и `GET` событие могло всё же дойти
   * (реконнект внутри окна) — без сверки одно сообщение попало бы дважды.
   * Источник дубля снова не сервер, а склейка истории с событиями.
   */
  const loadTail = useCallback(async () => {
    const cursor = tailCursor.current;
    // Курсора нет — история ещё не загружена (или пуста). Первичная загрузка
    // сделает это сама; второй запрос был бы лишним.
    if (!cursor) return;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
    try {
      const response = await fetch(
        `/api/proxy/chats/${encodeURIComponent(chatUuid)}/messages?cursor=${encodeURIComponent(cursor)}`,
        {
          credentials: "same-origin",
          headers: { accept: "application/json" },
          signal: controller.signal,
        },
      );
      // Догрузка хвоста — фоновая операция: ошибка не имеет права заменить
      // уже показанную переписку сообщением об ошибке. Следующий реконнект
      // повторит попытку.
      if (!response.ok) return;
      const page = (await response.json()) as MessagesPage;
      if (page.items.length === 0) return;
      setItems((previous) => {
        const known = new Set(previous.map((item) => item.uuid));
        const fresh = page.items.filter((item) => !known.has(item.uuid));
        return fresh.length === 0 ? previous : [...previous, ...fresh];
      });
      if (page.last_cursor) tailCursor.current = page.last_cursor;
    } catch {
      // Отмена/сеть: тот же довод, что выше — фоновая догрузка молчит.
    } finally {
      clearTimeout(timeoutId);
    }
  }, [chatUuid]);

  useEffect(() => {
    // Одно соединение на экран (Task 7.3). `stop` вызывается при
    // размонтировании и смене чата: без него сокет и таймер реконнекта
    // переживают уход со страницы (класс X-6.4-1).
    if (!resolvedWsUrl) return;
    const stop = createWsClient({
      url: resolvedWsUrl,
      chatUuid,
      onStateChange: setConnection,
      // X-6.5-5 (chore P-7): после восстановления связи догружаем хвост.
      // События за окно обрыва не придут никогда — Pub/Sub не хранит историю.
      onReconnect: () => {
        void loadTail();
      },
      onMessage: (payload) => {
        // Дедуп по `message.uuid` (Task 7.3): событие может прийти и после
        // догрузки истории, которая это сообщение уже содержит. Источник
        // дубля — не сервер, а склейка истории с событиями.
        setItems((previous) =>
          previous.some((item) => item.uuid === payload.message_uuid)
            ? previous
            : [
                ...previous,
                {
                  uuid: payload.message_uuid,
                  sender_uuid: payload.sender_uuid,
                  body: payload.body,
                  created_at: payload.created_at,
                  // Стори 6.7 (AC-04): событие несёт признак системности;
                  // без него системное сообщение отрисовалось бы пузырём
                  // собеседника с аватаром и без объяснения факта.
                  is_system: payload.is_system === true,
                },
              ],
        );
      },
    });
    return stop;
    // `loadTail` в зависимостях обязателен: колбэк захватывает `chatUuid`,
    // и при его смене соединение пересоздаётся вместе с обработчиком
    // реконнекта — иначе догрузка читала бы историю СТАРОГО чата.
  }, [chatUuid, resolvedWsUrl, loadTail]);

  useEffect(() => {
    // CR-замечание линтера `react-hooks/set-state-in-effect`: `setLoading(true)`
    // в теле эффекта запускает каскадный рендер на каждом монтировании.
    // Начальное значение уже `true` (`useState(true)`), а перезагрузка при
    // смене `chatUuid` не нужна: страница чата монтируется заново на каждый
    // `uuid` (роут-параметр), и состояние сбрасывается вместе с компонентом.
    let active = true;
    // Запуск обёрнут в `async`-функцию: правило `react-hooks/set-state-in-effect`
    // считает `setState` синхронным, если он достижим из тела эффекта без
    // `await`. Здесь все обновления состояния происходят ПОСЛЕ ожидания
    // ответа API — синхронного каскадного рендера нет.
    const run = async () => {
      try {
        await loadPage(null, false);
      } finally {
        if (active) setLoading(false);
      }
    };
    void run();
    return () => {
      active = false;
      // X-6.4-1 (Chore P-5): отмена запроса истории при размонтировании и при
      // смене `chatUuid` (эффект перезапускается — `loadPage` в зависимостях).
      // Без этого fetch жил до таймаута, а ответ на старый чат мог прийти
      // после монтирования нового.
      historyAbort.current?.abort();
    };
  }, [loadPage]);

  async function send() {
    const body = draft.trim();
    if (!body || sending) return;
    setSending(true);
    setSendError(null);
    // NFR18: идентификатор создаётся ДО запроса и переиспользуется при ретрае.
    const clientId = pendingClientId.current ?? crypto.randomUUID();
    pendingClientId.current = clientId;
    try {
      const response = await fetch(
        `/api/proxy/chats/${encodeURIComponent(chatUuid)}/messages`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json", accept: "application/json" },
          body: JSON.stringify({ body, client_message_id: clientId }),
          signal: AbortSignal.timeout(API_TIMEOUT_MS),
        },
      );
      if (response.status === 404) {
        setNotFound(true);
        return;
      }
      if (!response.ok) {
        const payload: unknown = await response.json().catch(() => null);
        const code =
          typeof payload === "object" && payload !== null && "code" in payload
            ? String((payload as { code: unknown }).code)
            : `http_${response.status}`;
        // `service_unavailable` (NFR19) — «попробуйте ещё»: сервер честно
        // сказал, что не записал. Текст отличается от «нет доступа».
        setSendError(
          code === "service_unavailable"
            ? "Сервис недоступен. Сообщение не отправлено — попробуйте ещё раз."
            : "Не удалось отправить. Попробуйте ещё раз.",
        );
        return;
      }
      const saved = (await response.json()) as ChatMessage;
      setDraft("");
      pendingClientId.current = null;
      // Инвалидация без библиотеки: сервер — источник истины, поэтому список
      // перечитывается целиком с первой страницы (порядок и `created_at`
      // приходят с сервера, локальная вставка их бы не знала).
      await loadPage(null, false);
      // Оптимистичный минимум: если перечитывание не удалось, сообщение
      // всё равно видно — оно уже сохранено сервером.
      setItems((previous) =>
        previous.some((item) => item.uuid === saved.uuid) ? previous : [...previous, saved],
      );
    } catch {
      setSendError("Сеть недоступна. Сообщение не отправлено — попробуйте ещё раз.");
    } finally {
      setSending(false);
    }
  }

  if (notFound) {
    return (
      <p className="text-base leading-relaxed text-[#5A5045]">
        Переписка недоступна. Возможно, встреча отменена или ссылка неверна.
      </p>
    );
  }

  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold tracking-tight">Чат компании</h1>

      {/* AC-03, NFR57: тонкая полоса, при `online` не рисуется вовсе. */}
      <ConnectionIndicator state={connection} />

      {loading ? (
        // Скелетоны тех же габаритов, что и сообщения: layout shift исключён.
        <div className="flex flex-col gap-3" aria-busy="true" aria-live="polite">
          {[0, 1, 2].map((index) => (
            <div
              key={index}
              className="h-10 w-2/3 animate-pulse rounded-md bg-[#F2EDE6]"
            />
          ))}
        </div>
      ) : items.length === 0 ? (
        <p className="text-base leading-relaxed text-[#5A5045]">
          Сообщений пока нет. Напишите первым — партнёры увидят ваше сообщение.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((message) => {
            // AC-04 (6.7): системное сообщение рендерит ОТДЕЛЬНЫЙ компонент —
            // без аватара, без выравнивания по автору и без кнопки удаления.
            if (message.is_system) {
              return (
                <SystemMessage
                  key={message.uuid}
                  body={message.body}
                  createdAt={message.created_at}
                />
              );
            }
            const own = message.sender_uuid === viewerUuid;
            return (
              <li
                key={message.uuid}
                className={`flex flex-col gap-1 ${own ? "items-end" : "items-start"}`}
              >
                <div
                  className={`max-w-[80%] whitespace-pre-wrap break-words rounded-md px-3 py-2 text-base leading-relaxed ${
                    own
                      ? "bg-[#7A3E12] text-white"
                      : "border border-[#D8CFC4] bg-white text-[#3D3529]"
                  }`}
                >
                  {message.body}
                </div>
                <span className="text-xs text-[#5A5045]">{timeOf(message.created_at)}</span>
              </li>
            );
          })}
        </ul>
      )}

      {cursor !== null && !loading ? (
        <button
          type="button"
          disabled={loadingMore}
          onClick={async () => {
            setLoadingMore(true);
            await loadPage(cursor, true);
            setLoadingMore(false);
          }}
          className="min-h-11 self-start rounded-md border border-[#D8CFC4] px-4 py-2 text-sm font-medium text-[#3D3529] hover:bg-[#F2EDE6] disabled:opacity-60"
        >
          {loadingMore ? "Загружаем…" : "Показать ещё"}
        </button>
      ) : null}

      {error ? (
        <p role="alert" className="text-sm text-[#8A2B12]">
          {error}
        </p>
      ) : null}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
        className="flex flex-col gap-2"
      >
        <label htmlFor="chat-message" className="text-sm font-medium text-[#3D3529]">
          Сообщение
        </label>
        <textarea
          id="chat-message"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          maxLength={4000}
          rows={3}
          className="w-full rounded-md border border-[#D8CFC4] px-3 py-2 text-base leading-relaxed"
          placeholder="Договоритесь о деталях встречи"
        />
        <button
          type="submit"
          disabled={sending || !draft.trim()}
          className="min-h-11 self-start rounded-md bg-[#7A3E12] px-4 py-2 text-sm font-medium text-white hover:bg-[#5E2F0D] disabled:opacity-60"
        >
          {sending ? "Отправляем…" : "Отправить"}
        </button>
        {sendError ? (
          <p role="alert" className="text-sm text-[#8A2B12]">
            {sendError}
          </p>
        ) : null}
      </form>
    </section>
  );
}
