/**
 * Тесты WS-клиента (стори 6.5; TD 6.5: V1, V2, V4, V5, V6).
 *
 * Фейк `WebSocket` берётся из Chore P-6 (`@/test/fake-websocket`): jsdom не
 * реализует `WebSocket` вовсе, и без фейка эти тесты проверяли бы отсутствие
 * API, а не поведение клиента.
 *
 * **Почему таймеры подменяются.** Реконнект по D-6.5-2 идёт с задержками до
 * 4 секунд: реальное ожидание сделало бы набор медленным и флейки. Но
 * проверяются НАСТОЯЩИЕ значения задержек, а не факт их вызова.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FakeWebSocket, installFakeWebSocket } from "@/test/fake-websocket";
import {
  BASE_DELAY_MS,
  MAX_DELAY_MS,
  createWsClient,
  jitteredDelay,
  nextDelay,
  type ConnectionState,
} from "@/lib/ws-client";

const CHAT_UUID = "11111111-1111-4111-8111-111111111111";
const WS_URL = "ws://test/api/v1/ws";

let uninstall: () => void;

beforeEach(() => {
  uninstall = installFakeWebSocket();
});

afterEach(() => {
  uninstall();
  vi.useRealTimers();
});

/** Собирает клиент с подменёнными таймерами и записью состояний. */
function makeClient(overrides: { random?: () => number } = {}) {
  const states: ConnectionState[] = [];
  const messages: string[] = [];
  const timeouts: { delay: number; fire: () => void }[] = [];
  const reconnects: number[] = [];

  const stop = createWsClient({
    url: WS_URL,
    chatUuid: CHAT_UUID,
    onMessage: (payload) => messages.push(payload.body),
    onStateChange: (state) => states.push(state),
    // X-6.5-5 (chore P-7): отдельный колбэк восстановления проверяется
    // отдельным счётчиком — он не должен срабатывать на первом открытии.
    onReconnect: () => reconnects.push(Date.now()),
    setTimeoutFn: (handler, timeout) => {
      timeouts.push({ delay: timeout, fire: handler });
      return timeouts.length;
    },
    clearTimeoutFn: () => undefined,
    random: overrides.random ?? (() => 0.5),
  });

  return { stop, states, messages, timeouts, reconnects };
}

function messagePayload(uuid: string, body: string) {
  return {
    message_uuid: uuid,
    chat_uuid: CHAT_UUID,
    sender_uuid: "22222222-2222-4222-8222-222222222222",
    body,
    created_at: "2026-10-24T00:00:00Z",
  };
}

describe("nextDelay (D-6.5-2)", () => {
  it("первая попытка немедленная, далее 500×2ⁿ с потолком 4000", () => {
    expect([1, 2, 3, 4, 5, 6].map(nextDelay)).toEqual([0, 500, 1000, 2000, 4000, 4000]);
  });

  it("потолок не превышается даже на большом номере попытки", () => {
    expect(nextDelay(20)).toBe(MAX_DELAY_MS);
    expect(nextDelay(2)).toBe(BASE_DELAY_MS);
  });

  it("кумулятив до 4-й попытки укладывается в NFR17 (≤5 с)", () => {
    const cumulative = [1, 2, 3, 4].reduce((sum, attempt) => sum + nextDelay(attempt), 0);

    expect(cumulative).toBe(3500);
    expect(cumulative).toBeLessThan(5000);
  });
});

describe("jitteredDelay (V5)", () => {
  it("границы ±20%: random=0 даёт 0.8×, random≈1 даёт 1.2×", () => {
    const low = jitteredDelay(3, () => 0);
    const high = jitteredDelay(3, () => 0.999999);

    expect(low).toBe(Math.round(1000 * 0.8));
    expect(high).toBe(Math.round(1000 * 1.2));
  });

  it("первая попытка остаётся нулевой при любом jitter", () => {
    expect(jitteredDelay(1, () => 0)).toBe(0);
    expect(jitteredDelay(1, () => 0.999999)).toBe(0);
  });
});

describe("createWsClient", () => {
  it("V4: подписывается на комнату и сообщает online после открытия", () => {
    const { states, stop } = makeClient();
    const socket = FakeWebSocket.latest();
    socket.serverOpen();

    expect(socket.sent).toEqual([
      { type: "chat.subscribe", payload: { chat_uuid: CHAT_UUID } },
    ]);
    expect(states).toContain("online");
    stop();
  });

  it("V2: дубль по message_uuid доставляется ОДИН раз", () => {
    const { messages, stop } = makeClient();
    const socket = FakeWebSocket.latest();
    socket.serverOpen();

    socket.serverMessage({ type: "message.created", payload: messagePayload("m-1", "раз"), ts: "t" });
    socket.serverMessage({ type: "message.created", payload: messagePayload("m-1", "раз"), ts: "t" });

    expect(messages).toEqual(["раз"]);
    stop();
  });

  it("V2 (второе значение): разные message_uuid доставляются оба", () => {
    const { messages, stop } = makeClient();
    const socket = FakeWebSocket.latest();
    socket.serverOpen();

    socket.serverMessage({ type: "message.created", payload: messagePayload("m-1", "раз"), ts: "t" });
    socket.serverMessage({ type: "message.created", payload: messagePayload("m-2", "два"), ts: "t" });

    expect(messages).toEqual(["раз", "два"]);
    stop();
  });

  it("V1: неизвестный тип игнорируется и не роняет клиент", () => {
    const { messages, stop } = makeClient();
    const socket = FakeWebSocket.latest();
    socket.serverOpen();

    expect(() => {
      socket.serverMessage({ type: "card.unknown_future", payload: { a: 1 }, ts: "t" });
      socket.serverRawMessage("не json");
    }).not.toThrow();

    socket.serverMessage({ type: "message.created", payload: messagePayload("m-9", "после мусора"), ts: "t" });
    expect(messages).toEqual(["после мусора"]);
    stop();
  });

  it("V6: обрыв запускает переподключение с задержкой по D-6.5-2", () => {
    const { states, timeouts, stop } = makeClient();
    const socket = FakeWebSocket.latest();
    socket.serverOpen();

    socket.serverClose(1006);

    expect(states).toContain("reconnecting");
    expect(timeouts.map((t) => t.delay)).toEqual([0]);
    stop();
  });

  it("V6: последующие попытки растут по экспоненте", () => {
    const { timeouts, stop } = makeClient({ random: () => 0.5 });

    // Первая попытка 0 мс → новое соединение → сразу рвём снова.
    FakeWebSocket.latest().serverOpen();
    FakeWebSocket.latest().serverClose(1006);

    timeouts[0]?.fire();
    FakeWebSocket.latest().serverClose(1006);
    timeouts[1]?.fire();
    FakeWebSocket.latest().serverClose(1006);

    // random=0.5 даёт множитель 1.0 → задержки равны базовым.
    expect(timeouts.map((t) => t.delay)).toEqual([0, 500, 1000]);
    stop();
  });

  it("X-6.5-5 (P-3, false): первое открытие НЕ считается восстановлением", () => {
    // Второе значение признака обязательно: тест только на «сработал после
    // реконнекта» прошёл бы и при `onReconnect`, вызываемом ВСЕГДА, — тогда
    // экран делал бы лишний запрос истории на каждое монтирование.
    const { reconnects, stop } = makeClient();

    FakeWebSocket.latest().serverOpen();

    expect(reconnects).toEqual([]);
    stop();
  });

  it("X-6.5-5 (P-3, true): открытие ПОСЛЕ обрыва сообщает о восстановлении", () => {
    const { timeouts, reconnects, stop } = makeClient();

    FakeWebSocket.latest().serverOpen();
    FakeWebSocket.latest().serverClose(1006);
    timeouts[0]?.fire();
    FakeWebSocket.latest().serverOpen();

    expect(reconnects).toHaveLength(1);
    stop();
  });

  it("X-6.5-5: восстановление приходит ПОСЛЕ подписки на комнату", () => {
    // Порядок важен: если клиент начнёт читать историю ДО `chat.subscribe`,
    // сообщение, пришедшее между `GET` и подпиской, не придёт ни историей
    // (его ещё не было в ответе), ни событием (подписки не было).
    // После реконнекта создаётся НОВЫЙ сокет, поэтому подменять `send`
    // у первого бессмысленно. Проверяется наблюдаемое: в момент вызова
    // `onReconnect` актуальный сокет УЖЕ отправил `chat.subscribe`.
    const sentAtReconnect: unknown[][] = [];
    const timeouts: (() => void)[] = [];
    const stop = createWsClient({
      url: WS_URL,
      chatUuid: CHAT_UUID,
      onMessage: () => undefined,
      onReconnect: () => sentAtReconnect.push([...FakeWebSocket.latest().sent]),
      setTimeoutFn: (handler) => {
        timeouts.push(handler);
        return timeouts.length;
      },
      clearTimeoutFn: () => undefined,
      random: () => 0.5,
    });

    FakeWebSocket.latest().serverOpen();
    FakeWebSocket.latest().serverClose(1006);
    timeouts[0]?.();
    FakeWebSocket.latest().serverOpen();

    expect(sentAtReconnect).toEqual([
      [{ type: "chat.subscribe", payload: { chat_uuid: CHAT_UUID } }],
    ]);
    stop();
  });

  it("stop() закрывает сокет и не планирует переподключение", () => {
    const { timeouts, stop } = makeClient();
    const socket = FakeWebSocket.latest();
    socket.serverOpen();

    stop();
    socket.serverClose(1006);

    expect(socket.readyState).toBe(3);
    expect(timeouts).toEqual([]);
  });
});
