/**
 * WS-клиент чата (стори 6.5, Task 7.1; AC-01, AC-02, AC-03).
 *
 * **Почему нативный `WebSocket`, а не socket.io.** Решение CS проверено
 * фактами: `socket.io-client` в `apps/web/package.json` НЕТ, а сервер
 * отдаёт конверт NFR60 `{type, payload, ts}` — socket.io навязал бы свой
 * формат `42[...]`, который пришлось бы разбирать и переупаковывать на обоих
 * концах. Один протокол вместо двух, ноль новых зависимостей.
 *
 * **Почему задержки именно такие (D-6.5-2, решение пользователя).**
 * NFR17 требует восстановления ≤5 с. Наивная экспонента `500 × 2ⁿ` с
 * потолком 5000 даёт кумулятивно `0.5 + 1 + 2 + 4 = 7.5 с` — требование
 * НЕ выполняется. Принятая трактовка: ПЕРВАЯ попытка немедленная (0 мс),
 * далее `500 × 2ⁿ`, потолок **4000 мс**. Кумулятивно: `0 → 0.5 → 1.5 → 3.5 с`,
 * то есть при восстановлении сети клиент переподключается не позднее
 * 4-й попытки — внутри 5 с.
 *
 * **Почему дедуп в клиенте, хотя он есть на сервере.** Серверный дедуп
 * гасит дубли ОДНОГО события из Redis, но после реконнекта клиент сам
 * запрашивает историю и склеивает её с тем, что уже показано. Без сверки по
 * `message.uuid` одно и то же сообщение попадало бы в список дважды —
 * источник дубля другой, и сервер его не видит.
 *
 * **Почему неизвестный тип не ошибка.** NFR60: клиент обязан игнорировать
 * события будущих версий сервера. Исключение здесь означало бы, что
 * обновление бэкенда ломает старый фронт.
 */

/** Конверт NFR60. `payload` — объект, `ts` — ISO-8601 UTC с `Z`. */
export type WsEnvelope = {
  type: string;
  payload: Record<string, unknown>;
  ts: string;
};

/** Состояние соединения для индикатора (AC-03). */
export type ConnectionState = "online" | "reconnecting" | "offline";

/** Задержка перед попыткой `attempt` (1 — первая после обрыва). */
export const BASE_DELAY_MS = 500;
export const MAX_DELAY_MS = 4000;

/**
 * Задержка перед попыткой переподключения.
 *
 * `attempt = 1` → `0` (немедленная попытка, D-6.5-2), далее `500 × 2^(n-2)`
 * с потолком 4000. Немедленная первая попытка — не оптимизация: без неё
 * восстановление после краткого мигания сети занимало бы полсекунды, а
 * пользователь видел бы «переподключаемся» на ровном месте.
 */
export function nextDelay(attempt: number): number {
  if (attempt <= 1) return 0;
  const raw = BASE_DELAY_MS * 2 ** (attempt - 2);
  return Math.min(raw, MAX_DELAY_MS);
}

/**
 * Задержка с джиттером ±20%.
 *
 * Джиттер нужен против «шторма»: после падения сервера все клиенты
 * переподключаются синхронно и добивают его в момент старта. `random`
 * передаётся параметром, а не берётся из `Math.random` внутри, — иначе
 * границы ±20% нельзя было бы проверить тестом (тавтология §18.2).
 */
export function jitteredDelay(attempt: number, random: () => number): number {
  const base = nextDelay(attempt);
  if (base === 0) return 0;
  const factor = 0.8 + random() * 0.4; // 0.8…1.2
  return Math.round(base * factor);
}

export type MessageCreatedPayload = {
  message_uuid: string;
  chat_uuid: string;
  /** `null` у СИСТЕМНОГО сообщения: автора-пользователя нет (6.7, AC-04). */
  sender_uuid: string | null;
  /** Стори 6.7 (AC-04): признак системного сообщения в событии. */
  is_system?: boolean;
  body: string;
  created_at: string;
};

export type WsClientOptions = {
  /** Собирает адрес WS из текущего origin (cookie уезжает автоматически). */
  url: string;
  /** Комната `chat:{uuid}` — подписка сразу после открытия. */
  chatUuid: string;
  /** Вызывается на `message.created` (уже без дублей). */
  onMessage: (payload: MessageCreatedPayload) => void;
  /**
   * Вызывается, когда соединение ВОССТАНОВЛЕНО после обрыва (X-6.5-5).
   *
   * Отличается от `onStateChange("online")` тем, что не срабатывает на
   * ПЕРВОМ открытии: на нём историю уже загрузил экран, и повторный запрос
   * был бы лишним. Второй источник истины не появился — оба колбэка питаются
   * одним `onopen`, различает их счётчик успешных открытий.
   */
  onReconnect?: () => void;
  /** Вызывается на каждое изменение состояния — для индикатора. */
  onStateChange?: (state: ConnectionState) => void;
  /** Подмена конструктора и таймеров в тестах; в проде не передаётся. */
  createSocket?: (url: string) => WebSocket;
  setTimeoutFn?: (handler: () => void, timeout: number) => number;
  clearTimeoutFn?: (id: number) => void;
  random?: () => number;
};

/**
 * Одно соединение на приложение с реконнектом и дедупом.
 *
 * Возвращает функцию остановки: без неё компонент оставлял бы живой сокет
 * после размонтирования, а тест — висящий таймер (X-6.4-1 того же класса).
 */
export function createWsClient(options: WsClientOptions): () => void {
  const {
    url,
    chatUuid,
    onMessage,
    onStateChange,
    onReconnect,
    createSocket = (target: string) => new WebSocket(target),
    setTimeoutFn = (handler, timeout) =>
      globalThis.setTimeout(handler, timeout) as unknown as number,
    clearTimeoutFn = (id) => globalThis.clearTimeout(id),
    random = Math.random,
  } = options;

  // Дедуп по `message_uuid` с ОГРАНИЧЕННЫМ окном (CR 6.5).
  //
  // Неограниченный `Set` рос бы всё время жизни вкладки: за сутки активной
  // переписки это десятки тысяч uuid, и память не освобождается никогда —
  // утечка под видом защиты от дубля. Окно 500 записей с вытеснением старых
  // покрывает любой реальный реконнект (порядок событий за минуту) и не растёт.
  const DEDUP_WINDOW = 500;
  const seen = new Set<string>();
  const seenOrder: string[] = [];
  const remember = (uuid: string): void => {
    seen.add(uuid);
    seenOrder.push(uuid);
    while (seenOrder.length > DEDUP_WINDOW) {
      const oldest = seenOrder.shift();
      if (oldest !== undefined) seen.delete(oldest);
    }
  };
  let attempt = 0;
  let stopped = false;
  let socket: WebSocket | null = null;
  let timer: number | null = null;
  let state: ConnectionState = "offline";
  /**
   * Сколько раз соединение УСПЕШНО открылось (X-6.5-5).
   *
   * Один счётчик вместо отдельного флага «был ли обрыв»: `onopen` уже
   * единственная точка, где соединение становится живым, и «первое открытие»
   * — это ровно `attempt === 0`. Счётчик не сбрасывается при обрыве:
   * восстановлением считается любое открытие, кроме первого.
   */
  let opened = 0;

  function setState(next: ConnectionState): void {
    if (state === next) return;
    state = next;
    onStateChange?.(next);
  }

  function scheduleReconnect(): void {
    if (stopped) return;
    attempt += 1;
    const delay = jitteredDelay(attempt, random);
    setState("reconnecting");
    timer = setTimeoutFn(() => {
      timer = null;
      connect();
    }, delay);
  }

  function handleEnvelope(raw: string): void {
    let envelope: WsEnvelope;
    try {
      envelope = JSON.parse(raw) as WsEnvelope;
    } catch {
      // Битый кадр — не повод рвать соединение: сервер мог прислать что-то
      // из будущей версии контракта.
      return;
    }
    if (envelope === null || typeof envelope !== "object") return;
    if (envelope.type !== "message.created") {
      // NFR60: неизвестный тип игнорируется, а не роняет клиент.
      // `chat.subscribed`/`chat.restored` тоже сюда: они не меняют список.
      return;
    }
    const payload = envelope.payload as Partial<MessageCreatedPayload>;
    if (typeof payload?.message_uuid !== "string") return;
    if (seen.has(payload.message_uuid)) return;
    remember(payload.message_uuid);
    onMessage(payload as MessageCreatedPayload);
  }

  function connect(): void {
    if (stopped) return;
    const ws = createSocket(url);
    socket = ws;
    ws.onopen = () => {
      attempt = 0;
      opened += 1;
      setState("online");
      // Подписка на комнату: сервер проверит участие по БД (AC-06).
      ws.send(JSON.stringify({ type: "chat.subscribe", payload: { chat_uuid: chatUuid } }));
      // X-6.5-5: восстановление ПОСЛЕ обрыва. Первое открытие сюда не идёт
      // (`opened === 1`): экран уже загрузил историю сам, и лишний запрос
      // был бы двойной загрузкой. Колбэк вызывается ПОСЛЕ подписки: сервер
      // обязан уже знать о соединении, когда клиент начнёт читать историю,
      // иначе новое сообщение между `GET` и `subscribe` не придёт ни одним
      // путём (ни историей, ни событием) — та же дыра, что закрывает сам
      // chore.
      if (opened > 1) onReconnect?.();
    };
    ws.onmessage = (event: MessageEvent) => {
      if (typeof event.data === "string") handleEnvelope(event.data);
    };
    ws.onclose = () => {
      socket = null;
      if (stopped) return;
      scheduleReconnect();
    };
    ws.onerror = () => {
      // Ошибка без закрытия (браузер так умеет): закрываем сами, чтобы не
      // остаться в состоянии «online» с мёртвым сокетом.
      try {
        ws.close();
      } catch {
        // уже закрыт
      }
    };
  }

  setState("reconnecting");
  connect();

  return () => {
    stopped = true;
    if (timer !== null) {
      clearTimeoutFn(timer);
      timer = null;
    }
    if (socket !== null) {
      const current = socket;
      socket = null;
      try {
        current.close();
      } catch {
        // уже закрыт
      }
    }
    setState("offline");
  };
}
