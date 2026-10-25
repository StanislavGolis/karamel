/**
 * Прокси WebSocket `/ws` для Next (chore P-7, X-6.5-4).
 *
 * **Зачем прокси вообще.** Cookie `karamel_access` имеет `SameSite=Lax`, и
 * браузер прикладывает её к handshake только на СВОЁМ origin. Прямое
 * подключение к API (`ws://api:8000/api/v1/ws`) с другой origin уехало бы
 * без cookie, и сервер закрыл бы сокет кодом 1008 «нет сессии». Значит
 * браузер обязан идти на origin фронта, а фронт — туннелировать.
 *
 * **Почему custom server, а не `rewrites`/route handler.** `rewrites` в Next
 * не обрабатывают `Upgrade` вовсе (документация Next: «rewrites do not apply
 * to WebSocket»), а route handler работает на уровне HTTP-запроса и
 * `Upgrade` не видит. В Next 16 единственная поддерживаемая точка перехвата —
 * `nextServer.getUpgradeHandler()`, а её можно получить только из сервера,
 * который сам создал Next.
 *
 * **Почему raw-TCP туннель, а не `ws`-клиент.** Handshake и кадры уже
 * сформированы браузером; серверу не нужно их понимать, только переслать
 * байты. Туннель на `net.connect` не добавляет зависимости (`ws` есть в
 * дереве лишь транзитивно и объявленным не является — установка по AGENTS.md
 * требует согласования), не парсит кадры и потому не может их испортить.
 * Цена — прокси не видит содержимого: ни логов, ни фильтрации. Для этой
 * задачи это плюс: приложение уже проверяет участие по БД, а второй
 * источник истины в прокси разошёлся бы с API.
 */

import { connect } from "node:net";

/**
 * Путь на origin фронта, который туннелируется в API.
 *
 * Совпадает с путём API НАМЕРЕННО: `ChatScreen` уже строит адрес
 * `ws(s)://<origin>/api/v1/ws` от текущего origin (same-origin обязателен из-за
 * `SameSite=Lax`), и менять публичный контракт клиента ради внутренней
 * детали прокси было бы лишней правкой. Снаружи фронта путь принадлежит
 * фронту, внутри docker-сети — API; менять его не нужно.
 */
export const WS_PROXY_PATH = "/api/v1/ws";

/** Путь эндпоинта API: `api_router` смонтирован под `/api/v1`. */
export const WS_UPSTREAM_PATH = "/api/v1/ws";

/**
 * Заголовки, которые НЕЛЬЗЯ переносить в upstream как есть.
 *
 * `host` заменяется на адрес API: FastAPI строит по нему абсолютные ссылки, и
 * чужой Host дал бы ссылки на фронт. `content-length` у handshake всегда
 * отсутствует, но если появится — тело не пересылается потоково и длина
 * разошлась бы с фактом.
 */
const REPLACED_HEADERS = new Set(["host", "content-length"]);

/**
 * Нужно ли туннелировать запрос (chore P-7).
 *
 * Сравнение по ПУТИ без query: `new URL` нормализует `%2F` и убирает
 * `.`/`..`, поэтому `%2e%2e/ws` не пройдёт как свой путь. Возвращается
 * булево, а не «куда»: адрес upstream знает вызывающий.
 */
export function isProxyPath(url) {
  if (typeof url !== "string" || url.length === 0) return false;
  let pathname;
  try {
    // `base` обязателен: в `Upgrade`-запросе `req.url` — путь, не абсолютный URL.
    pathname = new URL(url, "http://localhost").pathname;
  } catch {
    return false;
  }
  return pathname === WS_PROXY_PATH;
}

/**
 * Разбирает `API_URL` в хост и порт для `net.connect`.
 *
 * `ws`/`wss` принимаются наравне с `http`/`https`: в `.env` стенда адрес API
 * часто записан схемой WS, и отказ на этой форме был бы ловушкой
 * конфигурации. TLS не поддерживается — внутри docker-сети его нет, а
 * публичный TLS терминируется перед фронтом (это задача деплоя, не прокси).
 */
export function parseUpstream(apiUrl) {
  const parsed = new URL(apiUrl);
  const port = parsed.port
    ? Number(parsed.port)
    : parsed.protocol === "https:" || parsed.protocol === "wss:"
      ? 443
      : 80;
  return { host: parsed.hostname, port, protocol: parsed.protocol };
}

/**
 * Собирает HTTP-запрос handshake для upstream.
 *
 * `rawHeaders` — плоский список `[имя, значение, имя, значение, …]` из
 * `IncomingMessage.rawHeaders`. Именно он, а не `req.headers`: последний
 * склеивает повторяющиеся заголовки через запятую, и `Sec-WebSocket-Extensions`
 * с двумя значениями превратился бы в невалидный список.
 *
 * Строка запроса — ВСЕГДА `WS_UPSTREAM_PATH`: путь браузера (`/ws`) наружу
 * фронта — публичный контракт, путь API — внутренний. Пересылать `/ws` в
 * upstream нельзя: там такого маршрута нет, и FastAPI ответил бы `403`.
 */
export function buildUpstreamHandshake(rawHeaders, upstream) {
  const lines = [`GET ${WS_UPSTREAM_PATH} HTTP/1.1`];
  for (let index = 0; index < rawHeaders.length; index += 2) {
    const name = rawHeaders[index];
    const value = rawHeaders[index + 1];
    if (REPLACED_HEADERS.has(name.toLowerCase())) continue;
    lines.push(`${name}: ${value}`);
  }
  lines.push(`Host: ${upstream.host}:${upstream.port}`);
  return `${lines.join("\r\n")}\r\n\r\n`;
}

/**
 * Туннелирует один `Upgrade`-запрос к API.
 *
 * Кадры WebSocket не разбираются: что пришло от браузера, уходит в API, и
 * наоборот. Поэтому прокси не может испортить кадр, но и не видит
 * содержимого — приложение уже проверяет участие по БД, и второй источник
 * истины здесь разошёлся бы с API.
 */
function tunnel(req, socket, head, upstream) {
  const target = connect({ host: upstream.host, port: upstream.port });

  // Ошибка соединения с API: браузер обязан получить ЧЕСТНЫЙ отказ, а не
  // висящий сокет. `502` без тела достаточно: клиент переподключится по
  // своему backoff (D-6.5-2), а причина останется в логе фронта.
  target.on("error", (error) => {
    console.error(
      JSON.stringify({
        event: "ws_proxy_upstream_failed",
        host: upstream.host,
        port: upstream.port,
        reason: error instanceof Error ? error.name : "unknown",
      }),
    );
    try {
      socket.end("HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
    } catch {
      // сокет уже закрыт — нечего делать
    }
  });

  target.on("connect", () => {
    target.write(buildUpstreamHandshake(req.rawHeaders, upstream));
    // `head` — байты, уже прочитанные парсером HTTP после заголовков.
    // Терять их нельзя: в них может лежать первый кадр WebSocket.
    if (head && head.length > 0) target.write(head);
    socket.pipe(target);
    target.pipe(socket);
  });

  // Закрытие любой из сторон завершает обе: иначе второй сокет висит до
  // таймаута ОС и держит файловый дескриптор на каждого отключившегося.
  const closeBoth = () => {
    socket.destroy();
    target.destroy();
  };
  socket.on("error", closeBoth);
  socket.on("close", closeBoth);
  target.on("close", closeBoth);
}

/**
 * Вешает прокси `/ws` на HTTP-сервер (chore P-7).
 *
 * Вынесено из `server.mjs` ради проверяемости: тест поднимает обычный
 * `node:http` сервер, вешает ЭТОТ обработчик и подключается настоящим
 * клиентом WebSocket. Без выноса пришлось бы поднимать весь Next, и тест
 * проверял бы сборку, а не туннель.
 *
 * `fallback` вызывается для всех остальных `Upgrade`-запросов. В проде это
 * обработчик Next (HMR в dev идёт по WS и обязан остаться рабочим); в тесте
 * его можно не передавать.
 */
export function attachWsProxy(server, { apiUrl, fallback } = {}) {
  const resolveApiUrl = typeof apiUrl === "function" ? apiUrl : () => apiUrl;
  server.on("upgrade", (req, socket, head) => {
    if (!isProxyPath(req.url)) {
      if (fallback) fallback(req, socket, head);
      else socket.destroy();
      return;
    }
    tunnel(req, socket, head, parseUpstream(resolveApiUrl()));
  });
  return server;
}
