/**
 * BFF-прокси `/api/proxy/*` → `{API_URL}/api/v1/*` (Task 7; AC-02, AC-03, AC-08).
 *
 * Зачем прокси, а не прямые запросы к API: одна origin для браузера (AC-03),
 * значит `SameSite=Lax` cookie уезжает, CORS не нужен, а `API_URL` остаётся
 * серверным секретом. Плюс единственное место, где живёт автообновление
 * сессии (AC-02) — клиент о нём не знает.
 *
 * Логика обновления вынесена в `lib/bff-proxy.ts` (D-15): тест-раннера
 * в `apps/web` нет, поэтому всё, что можно проверить без браузера,
 * живёт чистыми функциями.
 */

import { randomUUID } from "node:crypto";

import {
  ACCESS_COOKIE_NAME,
  REFRESH_COOKIE_NAME,
} from "@/lib/auth-cookies";
import {
  appendSetCookies,
  bearerFromCookie,
  buildTargetUrl,
  copyResponseHeaders,
  forwardRequestHeaders,
  isRefreshable,
  mergeSetCookies,
} from "@/lib/bff-proxy";

const REQUEST_TIMEOUT_MS = 15_000;

/**
 * AC-03: `API_URL` читается только на сервере и в момент запроса.
 *
 * Deep import из модуля запрещён: значение, прочитанное на этапе сборки,
 * зашилось бы в образ при `next build` и не менялось без пересборки.
 * Отдельной константы с `process.env` на верхнем уровне нет по той же причине.
 */
function apiUrl(): string {
  return process.env.API_URL ?? "http://127.0.0.1:8000";
}

type RouteContext = { params: Promise<{ path: string[] }> };

/**
 * Single-flight обновления (Task 8.3; D-12).
 *
 * Пять параллельных запросов иначе дадут пять refresh: один успешный и четыре
 * `401` — пользователя выкинет из-за собственной активности. Ключ — значение
 * refresh-cookie, поэтому разные пользователи не блокируют друг друга.
 * `Map` в памяти процесса: при нескольких репликах нужен Redis (пост-MVP),
 * и это осознанно отложено, а не забыто.
 */
type RefreshResult = {
  status: number;
  cookies: string[];
  body: string;
};

/** Запись в полёте: промис результата и число ожидающих его запросов. */
type InFlightRefresh = { promise: Promise<RefreshResult>; waiters: number };

const refreshInFlight = new Map<string, InFlightRefresh>();

/**
 * Обновление сессии с single-flight (Task 8.3; D-12; CR 1.5, патчи 7, 8, 14).
 *
 * Патч 14: наружу отдаётся **результат refresh**, а не только `Set-Cookie`:
 * при отказе клиент должен получить `session_expired` или `user_blocked`,
 * а не тело исходного `401 unauthenticated`. Тело читается один раз здесь —
 * `Response` одноразовый, а делят его несколько ожидающих.
 *
 * Патч 7: запись живёт, пока её ждёт хоть один запрос. Раньше `Map`
 * освобождался в `finally` самой загрузки, и запрос, пришедший в этом окне,
 * брал уже погашенный refresh → 401 и очистка cookie.
 *
 * Патч 8: отклонённый промис удаляется из `Map`. Если `fetch` бросил
 * синхронно (пустой `API_URL`), `finally` срабатывал до `set`, и запись
 * оставалась отклонённой до перезапуска процесса.
 */
async function refreshSession(refreshToken: string, requestId: string): Promise<RefreshResult> {
  const existing = refreshInFlight.get(refreshToken);
  if (existing) {
    existing.waiters += 1;
    try {
      return await existing.promise;
    } finally {
      existing.waiters -= 1;
      if (existing.waiters === 0) refreshInFlight.delete(refreshToken);
    }
  }

  const entry: InFlightRefresh = {
    promise: undefined as unknown as Promise<RefreshResult>,
    waiters: 1,
  };

  entry.promise = (async (): Promise<RefreshResult> => {
    const response = await fetch(`${apiUrl().replace(/\/+$/, "")}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { cookie: `${REFRESH_COOKIE_NAME}=${refreshToken}`, "x-request-id": requestId },
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    return {
      status: response.status,
      cookies: response.headers.getSetCookie(),
      body: await response.text(),
    };
  })();

  refreshInFlight.set(refreshToken, entry);
  // Патч 8: отказ не должен осесть в `Map`.
  entry.promise.catch(() => {
    if (refreshInFlight.get(refreshToken) === entry) refreshInFlight.delete(refreshToken);
  });

  try {
    return await entry.promise;
  } finally {
    entry.waiters -= 1;
    if (entry.waiters === 0) refreshInFlight.delete(refreshToken);
  }
}

async function handler(request: Request, context: RouteContext): Promise<Response> {
  // Task 7.2: в Next 16 `params` — Promise, синхронное чтение дало бы ошибку типов.
  const { path } = await context.params;
  const requestId = randomUUID();
  const started = Date.now();

  const { url } = buildTargetUrl(apiUrl(), path, new URL(request.url).search);
  // CR 1.5, патч 11: тело читается как байты, а не текст. `request.text()`
  // прогоняет его через UTF-8 и портит любые байты ≥ 0x80 — загрузка фото
  // профиля (эпик 2) приходила битой.
  const body =
    request.method === "GET" || request.method === "HEAD"
      ? undefined
      : await request.arrayBuffer();

  const headers = forwardRequestHeaders(request.headers);
  // Task 7.6/7.10: свой `x-request-id` для сквозной трассировки и Bearer
  // из cookie, если заголовка ещё нет (cookie в API приоритетнее — D-2).
  headers.set("x-request-id", requestId);
  if (!headers.has("authorization")) {
    const accessToken = bearerFromCookie(request.headers.get("cookie"), ACCESS_COOKIE_NAME);
    if (accessToken) headers.set("authorization", `Bearer ${accessToken}`);
  }

  const send = async (target: string, targetHeaders: Headers): Promise<Response> => {
    try {
      return await fetch(target, {
        method: request.method,
        headers: targetHeaders,
        body,
        cache: "no-store",
        redirect: "manual",
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
    } catch (error) {
      // CR 1.5, патч 12: сетевой отказ и таймаут — это `502` с `x-request-id`,
      // а не `500` из недр Next. Иначе клиент не отличает «сессия истекла»
      // от «сервер сломан», а в логе нет ни одного следа запроса.
      logRequest(request, path, 502, requestId, started);
      console.error(
        JSON.stringify({
          event: "bff_upstream_failed",
          method: request.method,
          path: `/api/v1/${path.join("/")}`,
          request_id: requestId,
          reason: error instanceof Error ? error.name : "unknown",
        }),
      );
      return new Response(JSON.stringify({ code: "upstream_unavailable" }), {
        status: 502,
        headers: { "content-type": "application/json", "x-request-id": requestId },
      });
    }
  };

  let upstream = await send(url, headers);
  let refreshedCookies: string[] = [];

  if (isRefreshable(upstream.status, path)) {
    const refreshToken = bearerFromCookie(
      request.headers.get("cookie"),
      REFRESH_COOKIE_NAME,
    );

    if (refreshToken) {
      const refreshResponse = await refreshSession(refreshToken, requestId);
      refreshedCookies = refreshResponse.cookies;

      if (refreshResponse.status === 200) {
        // Task 8.4: ровно один повтор с обновлённой cookie. Значения берутся
        // из свежих `Set-Cookie` самого refresh-ответа: пересобирать их из
        // тела нельзя — тела с токенами не существует (AC-08).
        const retryHeaders = forwardRequestHeaders(request.headers);
        retryHeaders.set("x-request-id", requestId);
        // CR 1.5, патч 15: переносятся ВСЕ cookie, а не только пара сессии.
        // Иначе `karamel_oauth` и любые будущие cookie терялись в повторе.
        const merged = mergeSetCookies(request.headers.get("cookie"), refreshedCookies);
        if (merged) retryHeaders.set("cookie", merged);
        // CR 1.5, патч 10: `bearerFromCookie` вместо `split("=")[1]`.
        // JWT в base64url содержит `=` в паддинге, и разрез по первому `=`
        // обрезал токен → 401 на валидной паре.
        const newAccess = bearerFromCookie(merged, ACCESS_COOKIE_NAME);
        if (newAccess) retryHeaders.set("authorization", `Bearer ${newAccess}`);

        // Task 8.7: один повтор, без рекурсии — повторный 401 уходит клиенту.
        upstream = await send(url, retryHeaders);
      } else {
        // CR 1.5, патч 14: клиент получает статус и тело **refresh-ответа**
        // (`session_expired` или `user_blocked`), а не `unauthenticated`
        // исходного запроса. При 403 (бан) сохраняется 403: обновление
        // не поможет, и клиент не должен идти на экран входа.
        const headers = copyResponseHeaders(upstream.headers);
        for (const cookie of refreshedCookies) headers.append("set-cookie", cookie);
        headers.set("x-request-id", requestId);
        logRequest(request, path, refreshResponse.status, requestId, started);
        return new Response(refreshResponse.body, {
          status: refreshResponse.status,
          headers,
        });
      }
    }
  }

  const responseHeaders = copyResponseHeaders(upstream.headers);
  // Сначала cookie исходного ответа, затем cookie обновления: браузер
  // применяет `Set-Cookie` по порядку, и новая пара должна победить.
  appendSetCookies(responseHeaders, upstream.headers);
  for (const cookie of refreshedCookies) responseHeaders.append("set-cookie", cookie);
  responseHeaders.set("x-request-id", requestId);

  logRequest(request, path, upstream.status, requestId, started);

  // Task 7.8: `204`/`304` без тела — иначе браузер получает тело там,
  // где его быть не должно.
  if (upstream.status === 204 || upstream.status === 304) {
    return new Response(null, { status: upstream.status, headers: responseHeaders });
  }
  return new Response(await upstream.arrayBuffer(), {
    status: upstream.status,
    headers: responseHeaders,
  });
}

/**
 * AC-08: в лог только метод, путь, статус и `x-request-id`.
 *
 * Тело, значения cookie и `Authorization` не логируются ни при каких
 * условиях: `console.log(headers)` в отладке — это утечка всех живых
 * сессий в журнал контейнера.
 */
function logRequest(
  request: Request,
  path: string[],
  status: number,
  requestId: string,
  started: number,
): void {
  console.log(
    JSON.stringify({
      event: "bff_proxy",
      method: request.method,
      path: `/api/v1/${path.join("/")}`,
      status,
      request_id: requestId,
      duration_ms: Date.now() - started,
    }),
  );
}

export const GET = handler;
export const POST = handler;
export const PATCH = handler;
export const PUT = handler;
export const DELETE = handler;
