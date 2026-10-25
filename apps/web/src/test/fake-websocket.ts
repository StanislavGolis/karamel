/**
 * Фейк `WebSocket` для jsdom (Chore P-6, O6; TD 6.5, «Требования к обвязке»).
 *
 * **Зачем.** jsdom не реализует `WebSocket` вообще: `new WebSocket(url)` бросает
 * `ReferenceError`. Без фейка ни один UI-инвариант стори 6.5 (реконнект,
 * дедуп, игнор неизвестного типа, индикатор соединения) не может покраснеть —
 * тесты проверяли бы отсутствие API, а не поведение клиента.
 *
 * **Почему не `mock-socket`/`vitest-websocket-mock`.** Новая зависимость ради
 * одного класса, который занимает 150 строк: правило проекта — зависимость
 * только с согласия владельца (D-15). Фейк к тому же должен уметь ровно то,
 * что нужно нам: ручную доставку события, ручной обрыв, ручной отказ —
 * библиотека даёт больше, но её поведение пришлось бы изучать.
 *
 * **Почему класс, а не объект.** Клиент (Task 7.1) будет читать
 * `readyState`, вешать `on*`-свойства и звать `send`/`close`; подмена
 * объекта-литерала не поймала бы опечатку в имени метода.
 */

/** Коды готовности по спецификации WebSocket. */
export const CONNECTING = 0;
export const OPEN = 1;
export const CLOSING = 2;
export const CLOSED = 3;

export type FakeCloseEvent = { code: number; reason: string; wasClean: boolean };

/**
 * Одно соединение, управляемое тестом.
 *
 * Тест зовёт `serverOpen()`, `serverMessage(json)` и `serverClose(code)` —
 * это «сервер». Клиент (код приложения) видит обычный `WebSocket`.
 */
export class FakeWebSocket {
  static readonly CONNECTING = CONNECTING;
  static readonly OPEN = OPEN;
  static readonly CLOSING = CLOSING;
  static readonly CLOSED = CLOSED;

  /** Все созданные соединения — в порядке создания. */
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readyState: number = CONNECTING;

  /** Что клиент отправил на «сервер». */
  readonly sent: unknown[] = [];

  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  private readonly listeners: Map<string, Set<(event: Event) => void>> = new Map();

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  /** Сбрасывает статическое состояние между тестами. */
  static reset(): void {
    FakeWebSocket.instances = [];
  }

  /** Последнее созданное соединение; падает понятно, если его нет. */
  static latest(): FakeWebSocket {
    const socket = FakeWebSocket.instances.at(-1);
    if (socket === undefined) {
      throw new Error("FakeWebSocket: ни одного соединения не создано");
    }
    return socket;
  }

  addEventListener(type: string, listener: (event: Event) => void): void {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: (event: Event) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  send(data: string): void {
    if (this.readyState !== OPEN) {
      throw new Error(`FakeWebSocket: send в состоянии ${this.readyState}`);
    }
    this.sent.push(JSON.parse(data) as unknown);
  }

  close(code = 1000, reason = ""): void {
    if (this.readyState === CLOSED) return;
    this.readyState = CLOSED;
    this.emitClose({ code, reason, wasClean: true });
  }

  // ——— сторона «сервера» ——————————————————————————————————————————————

  /** Handshake завершён: клиент получает `open`. */
  serverOpen(): void {
    this.readyState = OPEN;
    const event = new Event("open");
    this.onopen?.(event);
    this.dispatch("open", event);
  }

  /** Сервер прислал конверт: клиент получает `message`. */
  serverMessage(payload: unknown): void {
    const event = new MessageEvent("message", { data: JSON.stringify(payload) });
    this.onmessage?.(event);
    this.dispatch("message", event);
  }

  /** Сервер прислал НЕвалидный JSON: клиент не должен падать. */
  serverRawMessage(raw: string): void {
    const event = new MessageEvent("message", { data: raw });
    this.onmessage?.(event);
    this.dispatch("message", event);
  }

  /** Обрыв: клиент получает `close` и обязан начать реконнект. */
  serverClose(code = 1006, reason = ""): void {
    this.readyState = CLOSED;
    this.emitClose({ code, reason, wasClean: code === 1000 });
  }

  /** Сетевая ошибка без закрытия (браузер так умеет). */
  serverError(): void {
    const event = new Event("error");
    this.onerror?.(event);
    this.dispatch("error", event);
  }

  private emitClose(init: FakeCloseEvent): void {
    const event = new CloseEvent("close", init);
    this.onclose?.(event);
    this.dispatch("close", event);
  }

  private dispatch(type: string, event: Event): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

/**
 * Подменяет глобальный `WebSocket` на фейк и возвращает функцию снятия.
 *
 * Возвращается именно функция, а не делается `afterEach` внутри: тест
 * обязан сам решить, когда вернуть окружение — иначе при вложенных
 * подменах восстанавливался бы не тот класс.
 */
export function installFakeWebSocket(): () => void {
  const original = (globalThis as { WebSocket?: unknown }).WebSocket;
  FakeWebSocket.reset();
  (globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket;
  return () => {
    (globalThis as { WebSocket?: unknown }).WebSocket = original;
    FakeWebSocket.reset();
  };
}
