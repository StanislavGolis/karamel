/**
 * Тесты фейка `WebSocket` (Chore P-6, O6).
 *
 * Фейк — часть обвязки, и он тоже может врать. Если `serverOpen()` не
 * выставляет `readyState = OPEN`, клиентский код стори 6.5 либо не начнёт
 * отправку, либо тест «после реконнекта отправилось» будет зелёным по
 * неверной причине. Проверяются оба значения каждого признака (§18.11).
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  CLOSED,
  OPEN,
  FakeWebSocket,
  installFakeWebSocket,
} from "@/test/fake-websocket";

let uninstall: () => void;

beforeEach(() => {
  uninstall = installFakeWebSocket();
});

afterEach(() => {
  uninstall();
});

/** Создаёт соединение и возвращает его же: почти каждый тест начинает с этого. */
function connect(): FakeWebSocket {
  return new FakeWebSocket("ws://test/api/v1/ws");
}

describe("FakeWebSocket", () => {
  it("подменяет глобальный WebSocket", () => {
    expect(globalThis.WebSocket).toBe(FakeWebSocket);
  });

  it("начинает в состоянии CONNECTING и открывается только по serverOpen", () => {
    const socket = connect();
    expect(socket.readyState).toBe(FakeWebSocket.CONNECTING);

    let opened = 0;
    socket.addEventListener("open", () => {
      opened += 1;
    });
    socket.serverOpen();

    expect(socket.readyState).toBe(OPEN);
    expect(opened).toBe(1);
  });

  it("вызывает и onopen, и addEventListener-подписчика", () => {
    const socket = connect();
    let viaProperty = 0;
    let viaListener = 0;
    socket.onopen = () => {
      viaProperty += 1;
    };
    socket.addEventListener("open", () => {
      viaListener += 1;
    });

    socket.serverOpen();

    expect(viaProperty).toBe(1);
    expect(viaListener).toBe(1);
  });

  it("доставляет конверт как JSON-строку в message", () => {
    const socket = connect();
    socket.serverOpen();
    const received: unknown[] = [];
    socket.addEventListener("message", (event) => {
      received.push(JSON.parse((event as MessageEvent).data as string));
    });

    socket.serverMessage({ type: "message.created", payload: { n: 1 }, ts: "2026-10-24T00:00:00Z" });

    expect(received).toEqual([
      { type: "message.created", payload: { n: 1 }, ts: "2026-10-24T00:00:00Z" },
    ]);
  });

  it("доставляет НЕвалидный JSON как есть — клиент обязан не падать", () => {
    const socket = connect();
    socket.serverOpen();
    let raw: string | null = null;
    socket.onmessage = (event) => {
      raw = event.data as string;
    };

    socket.serverRawMessage("не json");

    expect(raw).toBe("не json");
  });

  it("close() клиента закрывает соединение и сообщает код 1000", () => {
    const socket = connect();
    socket.serverOpen();
    const codes: number[] = [];
    socket.addEventListener("close", (event) => {
      codes.push((event as CloseEvent).code);
    });

    socket.close();

    expect(socket.readyState).toBe(CLOSED);
    expect(codes).toEqual([1000]);
  });

  it("serverClose() отдаёт код обрыва и не считается чистым", () => {
    const socket = connect();
    socket.serverOpen();
    let wasClean: boolean | null = null;
    let code = 0;
    socket.onclose = (event) => {
      wasClean = event.wasClean;
      code = event.code;
    };

    socket.serverClose(1006);

    expect(code).toBe(1006);
    expect(wasClean).toBe(false);
  });

  it("send() записывает конверт и падает вне OPEN", () => {
    const socket = connect();
    expect(() => socket.send('{"type":"chat.subscribe"}')).toThrowError(/send в состоянии/);

    socket.serverOpen();
    socket.send('{"type":"chat.subscribe","payload":{"chat_uuid":"c-1"}}');

    expect(socket.sent).toEqual([{ type: "chat.subscribe", payload: { chat_uuid: "c-1" } }]);
  });

  it("removeEventListener снимает подписчика", () => {
    const socket = connect();
    socket.serverOpen();
    let calls = 0;
    const listener = () => {
      calls += 1;
    };
    socket.addEventListener("message", listener);
    socket.serverMessage({ type: "probe.ping", payload: {}, ts: "2026-10-24T00:00:00Z" });
    socket.removeEventListener("message", listener);
    socket.serverMessage({ type: "probe.ping", payload: {}, ts: "2026-10-24T00:00:00Z" });

    expect(calls).toBe(1);
  });

  it("latest() падает понятной ошибкой, если соединений нет", () => {
    expect(() => FakeWebSocket.latest()).toThrowError(/ни одного соединения/);

    connect();

    expect(FakeWebSocket.latest().url).toBe("ws://test/api/v1/ws");
  });

  it("uninstall возвращает исходный WebSocket", () => {
    uninstall();

    expect(globalThis.WebSocket).not.toBe(FakeWebSocket);
    expect(FakeWebSocket.instances).toEqual([]);
  });
});
