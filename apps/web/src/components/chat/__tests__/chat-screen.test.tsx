/**
 * UI-тесты `ChatScreen` (Chore P-5, решение эпика 6 п.6; закрывает X-18 в части «нет раннера»).
 *
 * До этой задачи фронт проверялся только `tsc` и `eslint`: ни один
 * UI-инвариант не мог покраснеть. Здесь проверяются именно ПОВЕДЕНИЯ,
 * обещанные в стори 6.4:
 *
 * - список сообщений рендерится (AC-01, AC-02);
 * - загрузка — СКЕЛЕТОНЫ с `aria-busy`, а не спиннер (UX-правило);
 * - `404` даёт «Переписка недоступна», а не общий текст ошибки (ADR-13);
 * - отправка шлёт `client_message_id` и перечитывает историю (AC-04, NFR18);
 * - чужой текст с `<script>` выводится как ТЕКСТ, не исполняется (XSS);
 * - запрос отменяется при размонтировании (X-6.4-1).
 *
 * `fetch` подменяется вручную, без `msw`: проект не имеет MSW-инфраструктуры,
 * а два маршрута (`GET`/`POST`) проще описать прямым моком, чем поднимать
 * обработчик сервера.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatScreen } from "@/components/chat/chat-screen";
import { FakeWebSocket, installFakeWebSocket } from "@/test/fake-websocket";

const CHAT_UUID = "11111111-1111-4111-8111-111111111111";
const WS_URL = "ws://test/api/v1/ws";
const VIEWER_UUID = "22222222-2222-4222-8222-222222222222";
const OTHER_UUID = "33333333-3333-4333-8333-333333333333";

function message(uuid: string, sender: string, body: string, createdAt: string) {
  return { uuid, sender_uuid: sender, body, created_at: createdAt };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Ждёт, пока компонент запросит историю, и отвечает заданной страницей. */
function mockHistory(page: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page, status));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ChatScreen", () => {
  it("рендерит сообщения партнёров и свои", async () => {
    mockHistory({
      items: [
        message("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", OTHER_UUID, "Привет!", "2026-10-24T10:00:00Z"),
        message("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", VIEWER_UUID, "Здравствуй", "2026-10-24T10:01:00Z"),
      ],
      next_cursor: null,
    });

    render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} />);

    expect(await screen.findByText("Привет!")).toBeInTheDocument();
    expect(screen.getByText("Здравствуй")).toBeInTheDocument();
    // Скелетоны исчезли после загрузки — иначе «загружено» не отличить от «грузится».
    expect(document.querySelector("[aria-busy='true']")).toBeNull();
  });

  it("показывает скелетоны с aria-busy вместо спиннера", async () => {
    // Промис не разрешается: компонент остаётся в состоянии загрузки.
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));

    render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} />);

    const busy = document.querySelector("[aria-busy='true']");
    expect(busy).not.toBeNull();
    // Скелетоны — три блока: габариты держатся, layout shift исключён.
    expect(busy?.children.length).toBe(3);
    // Спиннера нет: слова «Загрузка» в интерфейсе не появляется.
    expect(screen.queryByText(/загрузка/i)).not.toBeInTheDocument();
  });

  it("на 404 показывает «Переписка недоступна», а не ошибку сети", async () => {
    mockHistory({ code: "chat_not_found" }, 404);

    render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} />);

    expect(await screen.findByText(/Переписка недоступна/i)).toBeInTheDocument();
    // ADR-13: причина не раскрывается — подсказок «нет доступа» быть не должно.
    expect(screen.queryByText(/нет доступа/i)).not.toBeInTheDocument();
  });

  it("отправляет сообщение с client_message_id и перечитывает историю", async () => {
    const sent = message(
      "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      VIEWER_UUID,
      "Новое сообщение",
      "2026-10-24T10:02:00Z",
    );
    let historyCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      // Стори 6.6 (Task 6.6, AC-02): открытие чата отмечает прочтение — это
      // отдельный POST, и он НЕ должен сдвигать очередь ответов истории
      // (иначе второй GET получил бы ответ «сохранённое сообщение»).
      if (url.endsWith("/read")) return jsonResponse({ chat_uuid: CHAT_UUID, unread_count: 0 });
      if ((init?.method ?? "GET") === "POST") return jsonResponse(sent, 201);
      historyCalls += 1;
      // Первый GET — пустая история, второй — с отправленным сообщением.
      if (historyCalls === 1) return jsonResponse({ items: [], next_cursor: null });
      return jsonResponse({ items: [sent], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} />);

    const textarea = await screen.findByLabelText("Сообщение");
    await userEvent.type(textarea, "Новое сообщение");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(await screen.findByText("Новое сообщение")).toBeInTheDocument();

    // Ищем именно отправку СООБЩЕНИЯ: `/read` (стори 6.6) — тоже POST, но
    // тела у него нет, и первый найденный POST дал бы `body === undefined`.
    const postCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        (init as RequestInit | undefined)?.method === "POST" &&
        String(input).includes("/messages"),
    );
    expect(postCall).toBeDefined();
    const payload = JSON.parse(String((postCall?.[1] as RequestInit).body)) as {
      body: string;
      client_message_id: string;
    };
    expect(payload.body).toBe("Новое сообщение");
    // NFR18: идентификатор есть ВСЕГДА — без него ретрай создал бы дубль.
    expect(payload.client_message_id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("не исполняет HTML из чужого сообщения (XSS)", async () => {
    mockHistory({
      items: [
        message(
          "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
          OTHER_UUID,
          "<script>alert('xss')</script>опасный текст",
          "2026-10-24T10:03:00Z",
        ),
      ],
      next_cursor: null,
    });

    render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} />);

    // Текст виден как текст…
    expect(await screen.findByText(/опасный текст/)).toBeInTheDocument();
    // …но `<script>` не попал в DOM как элемент.
    expect(document.querySelector("script")).toBeNull();
  });

  it("догружает пропущенное после реконнекта ровно один раз (X-6.5-5)", async () => {
    // Сценарий целиком: история загружена → WS рвётся → сервер пишет новое
    // сообщение → соединение восстанавливается → сообщение ОБЯЗАНО появиться,
    // и ровно один раз. Именно это ломается без chore: Pub/Sub не хранит
    // историю, и событие за окно обрыва не придёт никогда.
    const uninstall = installFakeWebSocket();
    const missed = message(
      "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
      OTHER_UUID,
      "Явка подтверждена",
      "2026-10-24T10:05:00Z",
    );
    const first = message(
      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      OTHER_UUID,
      "Привет!",
      "2026-10-24T10:00:00Z",
    );
    const TAIL = "tail-cursor-1";
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/read")) return jsonResponse({ chat_uuid: CHAT_UUID, unread_count: 0 });
        calls.push(url);
        // Первый GET — история из одного сообщения с курсором хвоста.
        if (!url.includes("cursor=")) {
          return jsonResponse({ items: [first], next_cursor: null, last_cursor: TAIL });
        }
        // Догрузка после реконнекта: новое сообщение, пришедшее за обрыв.
        return jsonResponse({ items: [missed], next_cursor: null, last_cursor: "tail-cursor-2" });
      }),
    );

    render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} wsUrl={WS_URL} />);
    expect(await screen.findByText("Привет!")).toBeInTheDocument();

    const socket = FakeWebSocket.latest();
    socket.serverOpen();
    socket.serverClose(1006);
    // Задержка первой попытки реконнекта — 0 мс (D-6.5-2), ждём нового сокета.
    const reconnected = await waitFor(() => {
      const latest = FakeWebSocket.latest();
      expect(latest).not.toBe(socket);
      return latest;
    });
    reconnected.serverOpen();

    expect(await screen.findByText("Явка подтверждена")).toBeInTheDocument();
    // Ровно один раз: дедуп по `uuid` — склейка истории с событиями не должна
    // давать дубль, даже если событие успело прийти до `GET`.
    expect(screen.getAllByText("Явка подтверждена")).toHaveLength(1);
    // И запрос действительно был ИНКРЕМЕНТОМ от курсора хвоста, а не «с нуля»:
    // перечитывание первой страницы вернуло бы начало переписки.
    expect(calls.some((url) => url.includes(`cursor=${TAIL}`))).toBe(true);

    uninstall();
  });

  it("не дублирует сообщение, пришедшее и событием, и догрузкой (X-6.5-5)", async () => {
    // Источник дубля — НЕ сервер, а склейка двух путей: событие WS за окно
    // реконнекта успело дойти, а догрузка по курсору вернула то же сообщение.
    // Без дедупа в `loadTail` пользователь увидел бы две одинаковые строки.
    const uninstall = installFakeWebSocket();
    const same = message(
      "ffffffff-ffff-4fff-8fff-ffffffffffff",
      OTHER_UUID,
      "Явка подтверждена",
      "2026-10-24T10:06:00Z",
    );
    const first = message(
      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      OTHER_UUID,
      "Привет!",
      "2026-10-24T10:00:00Z",
    );
    // Новое сообщение, которого на экране ещё нет: его появление — признак,
    // что ответ догрузки реально дошёл до списка (см. ниже).
    const missed = message(
      "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      OTHER_UUID,
      "Без тебя не соберёмся",
      "2026-10-24T10:07:00Z",
    );
    let resolveTail: ((value: Response) => void) | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/read")) return jsonResponse({ chat_uuid: CHAT_UUID, unread_count: 0 });
        if (!url.includes("cursor=")) {
          return jsonResponse({ items: [first], next_cursor: null, last_cursor: "tail-1" });
        }
        // Догрузка отвечает ПОЗЖЕ — ровно чтобы событие успело прийти первым.
        return new Promise<Response>((resolve) => {
          resolveTail = resolve;
        });
      }),
    );

    render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} wsUrl={WS_URL} />);
    expect(await screen.findByText("Привет!")).toBeInTheDocument();

    const socket = FakeWebSocket.latest();
    socket.serverOpen();
    socket.serverClose(1006);
    const reconnected = await waitFor(() => {
      const latest = FakeWebSocket.latest();
      expect(latest).not.toBe(socket);
      return latest;
    });
    reconnected.serverOpen();

    // Событие приходит ПЕРВЫМ (пока догрузка ещё в полёте)…
    reconnected.serverMessage({
      type: "message.created",
      payload: {
        message_uuid: same.uuid,
        chat_uuid: CHAT_UUID,
        sender_uuid: same.sender_uuid,
        body: same.body,
        created_at: same.created_at,
      },
      ts: "t",
    });
    expect(await screen.findByText("Явка подтверждена")).toBeInTheDocument();

    // …а затем догрузка возвращает ТО ЖЕ сообщение ПЛЮС одно новое.
    //
    // Новое сообщение здесь — не украшение: это сигнал, что ответ догрузки
    // РЕАЛЬНО обработан. Без него `waitFor` на «ровно одна строка» проходил
    // бы мгновенно (сообщение уже на экране от события) и не проверял бы
    // дедуп вовсе — мутация «убрать дедуп» оставалась бы зелёной.
    await waitFor(() => expect(resolveTail).not.toBeNull());
    // Приведение типа обязательно: `resolveTail` присваивается ВНУТРИ
    // колбэка `fetch`, и анализ потока TypeScript сужает переменную до `null`
    // по инициализатору — без него `next build` падает `TS2349`.
    (resolveTail as ((value: Response) => void) | null)?.(
      jsonResponse({
        items: [same, missed],
        next_cursor: null,
        last_cursor: "tail-2",
      }),
    );

    // Догрузка дошла до списка…
    expect(await screen.findByText("Без тебя не соберёмся")).toBeInTheDocument();
    // …и дубль при этом ровно один: дедуп по `uuid` между событием и историей.
    expect(screen.getAllByText("Явка подтверждена")).toHaveLength(1);
    uninstall();
  });

  it("отменяет запрос истории при размонтировании (X-6.4-1)", async () => {
    let capturedSignal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        capturedSignal = init?.signal ?? undefined;
        return new Promise<Response>(() => {});
      }),
    );

    const { unmount } = render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} />);

    await waitFor(() => expect(capturedSignal).toBeDefined());
    expect(capturedSignal?.aborted).toBe(false);

    unmount();

    // X-6.4-1: запрос обязан быть отменён, а не доживать до таймаута.
    expect(capturedSignal?.aborted).toBe(true);
  });
});
