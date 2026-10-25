/**
 * Серверная проверка сессии через API (CR 1.5: Решения 1–3, патч 5; CR 1.6: 13, 14, 15).
 *
 * Гарда проверяет не наличие cookie, а её валидность: `document.cookie='karamel_access=x'`
 * открывал весь кабинет, потому что layout смотрел только на имя cookie.
 * Секрет JWT на фронтенде не нужен — проверку делает `/api/v1/auth/me`,
 * единственный владелец ключа остаётся в контейнере API.
 *
 * CR 1.6 изменил форму ответа: `null` больше не означает «любой отказ».
 *
 * * патч 13: `403` (бан) — не то же самое, что `401`. Раньше забаненный
 *   попадал на обычный `/login` без объяснения, и ветка `?error=user_blocked`
 *   на странице входа была недостижима;
 * * патч 14: `5xx` и таймаут — не «нет сессии». Живая сессия терялась при
 *   каждом подвисании API, а обратной дороги без повторного входа не было;
 * * патч 15: истёкший access при SSR-навигации выбрасывал на `/login` каждый
 *   час, хотя refresh жив и автообновление есть только в BFF.
 *
 * Поэтому наружу отдаётся `SessionResult` — вызывающая гарда решает, что
 * делать с каждым случаем, а не угадывает по `null`.
 */

import { REFRESH_COOKIE_NAME } from "@/lib/auth-cookies";

export type SessionUser = {
  uuid: string;
  role: string;
  status: string;
  /**
   * Стори 1.6 (Task 7.1): подтверждение 18+ и согласия на ПД.
   *
   * Безопасное значение по умолчанию — `false`: ответ API без поля
   * (**старая версия API**) не должен открывать кабинет. Обратный выбор
   * (`true`) снял бы требование согласия на всё время выката.
   */
  age_confirmed: boolean;
  /** Время первого согласия (ISO 8601 UTC) или `null`, если не подтверждено. */
  consent_given_at: string | null;
  /** Пол: `female`/`male`, или `null` до подтверждения (стори 1.6). */
  gender: string | null;
  /**
   * Решение 3 код-ревью 1.6: серверный признак «мужчина ждёт открытия заявок».
   *
   * До этого «мужчина вне выборки» жил только в UI, и прямой вызов API давал
   * тот же ответ, что и UI. Безопасное значение по умолчанию — `false`
   * (считаем, что доступ есть): экран ожидания не должен появляться из-за
   * отсутствующего поля у старой версии API.
   */
  awaiting_applications: boolean;
};

/**
 * Результат проверки сессии. Четыре исхода — четыре разных реакции гарды.
 */
export type SessionResult =
  | { kind: "authenticated"; user: SessionUser }
  | { kind: "unauthenticated" }
  | { kind: "blocked" }
  | { kind: "unavailable" };

/**
 * Решение 1 код-ревью 1.6: единый предикат согласия.
 *
 * До этого три гарды использовали три разных условия (`age_confirmed`
 * в layout кабинета, `gender !== null` в странице, `age_confirmed` в layout
 * `/consent`), и аккаунт с `age_confirmed=true` и `gender=null` попадал
 * в цикл `/app` ↔ `/consent` (`ERR_TOO_MANY_REDIRECTS`). Разные условия
 * в разных местах — это не «чуть разошлись», это недостижимый аккаунт.
 *
 * Условие обязано совпадать с CHECK-ограничением БД (миграция 0003):
 * `age_confirmed = (consent_given_at IS NOT NULL)` плюс заполненный `gender`.
 * Три источника правды (БД, API, UI) сведены к двум: БД и эта функция.
 */
export function hasConsent(user: SessionUser): boolean {
  return user.age_confirmed && user.consent_given_at !== null && user.gender !== null;
}

const API_TIMEOUT_MS = 5_000;

function apiUrl(): string {
  return (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
}

/**
 * Патч 15: серверное обновление сессии.
 *
 * Возвращает новый заголовок `Cookie` для повторного запроса или `null`, если
 * обновить не удалось. Установить обновлённую cookie в браузер из RSC нельзя —
 * это сделает BFF при первом же клиентском запросе. Задача этого шага скромнее:
 * не выбрасывать пользователя на `/login`, пока refresh жив.
 */
async function refreshCookieHeader(cookieHeader: string): Promise<string | null> {
  const match = new RegExp(`(?:^|;\\s*)${REFRESH_COOKIE_NAME}=([^;]+)`).exec(cookieHeader);
  if (!match) return null;

  try {
    const response = await fetch(`${apiUrl()}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { cookie: `${REFRESH_COOKIE_NAME}=${match[1]}`, "cache-control": "no-store" },
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
    if (!response.ok) return null;

    // D-13 из 1.5: `getSetCookie()` обязателен, `get('set-cookie')` склеивает
    // пару через запятую, а `Expires` тоже содержит запятую.
    const fromRefresh = response.headers
      .getSetCookie()
      .map((line) => line.split(";", 1)[0])
      .filter((pair) => pair.length > 0);
    if (fromRefresh.length === 0) return null;

    // Старые значения заменяются новыми, остальные cookie сохраняются.
    const replaced = new Set(fromRefresh.map((pair) => pair.split("=", 1)[0]));
    const rest = cookieHeader
      .split(";")
      .map((part) => part.trim())
      .filter((part) => part.length > 0 && !replaced.has(part.split("=", 1)[0]));
    return [...rest, ...fromRefresh].join("; ");
  } catch {
    return null;
  }
}

/** Один запрос `/auth/me` с таймаутом. Возвращает `null` при сетевом отказе. */
async function fetchMe(cookieHeader: string): Promise<Response | null> {
  try {
    return await fetch(`${apiUrl()}/api/v1/auth/me`, {
      headers: cookieHeader ? { cookie: cookieHeader } : {},
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
  } catch {
    // Сеть или таймаут — «API недоступен», а не «сессии нет» (патч 14).
    return null;
  }
}

function toUser(payload: Partial<SessionUser>): SessionUser | null {
  if (typeof payload.uuid !== "string" || typeof payload.role !== "string") {
    return null;
  }
  return {
    uuid: payload.uuid,
    role: payload.role,
    status: typeof payload.status === "string" ? payload.status : "active",
    // Task 7.1: только строгий `true` считается подтверждением (D-8).
    age_confirmed: payload.age_confirmed === true,
    consent_given_at: typeof payload.consent_given_at === "string" ? payload.consent_given_at : null,
    gender: typeof payload.gender === "string" ? payload.gender : null,
    // Решение 3: только строгий `true` включает экран ожидания.
    awaiting_applications: payload.awaiting_applications === true,
  };
}

/**
 * Проверяет сессию и различает четыре исхода (патчи 13, 14, 15).
 *
 * `cache: "no-store"` обязателен: иначе Next закэширует результат RSC-запроса
 * и один пользователь увидит кабинет другого.
 */
export async function loadSession(cookieHeader: string | null): Promise<SessionResult> {
  const header = cookieHeader ?? "";
  const first = await fetchMe(header);

  // Патч 14: нет ответа вовсе — API недоступен, сессия не опровергнута.
  if (first === null) return { kind: "unavailable" };

  if (first.ok) {
    const user = toUser((await first.json()) as Partial<SessionUser>);
    // Ответ `200` без обязательных полей — контракт нарушен, доверять нельзя.
    return user === null ? { kind: "unavailable" } : { kind: "authenticated", user };
  }

  // Патч 13: бан отличается от истечения сессии: пользователю нужен текст,
  // а не форма входа, которая заведомо не сработает.
  if (first.status === 403) return { kind: "blocked" };

  // Патч 14: `5xx` — недоступность API, а не отказ в доступе.
  if (first.status >= 500) return { kind: "unavailable" };

  if (first.status !== 401) return { kind: "unavailable" };

  // Патч 15: access истёк, но refresh жив — одна попытка серверного обновления.
  const renewed = await refreshCookieHeader(header);
  if (renewed === null) return { kind: "unauthenticated" };

  const second = await fetchMe(renewed);
  if (second === null || second.status >= 500) return { kind: "unavailable" };
  if (second.status === 403) return { kind: "blocked" };
  if (!second.ok) return { kind: "unauthenticated" };

  const user = toUser((await second.json()) as Partial<SessionUser>);
  return user === null ? { kind: "unavailable" } : { kind: "authenticated", user };
}

/**
 * Совместимая обёртка: `SessionUser | null` для мест, где различие исходов
 * не нужно (например `/mod`, где любой отказ ведёт на главную).
 */
export async function getSessionUser(cookieHeader: string | null): Promise<SessionUser | null> {
  const result = await loadSession(cookieHeader);
  return result.kind === "authenticated" ? result.user : null;
}
