/**
 * Чистые функции BFF-прокси (Task 8.1; AC-02, AC-03, AC-08; D-15).
 *
 * Вынесены из route handler намеренно: в `apps/web` нет тест-раннера
 * (D-15 — новая зависимость только с согласия владельца), поэтому вся
 * логика, которую можно проверить без браузера, живёт здесь функциями
 * без побочных эффектов. Сам handler остаётся тонкой обвязкой над `fetch`.
 */

export const API_PREFIX = "/api/v1";

/** Пути, для которых автообновление запрещено (AC-02, D-12). */
const NO_REFRESH_PATHS = ["auth/refresh", "auth/logout"];

export type ProxyTarget = { url: string };

/**
 * Собирает адрес FastAPI (Task 7.4; AC-03).
 *
 * `API_URL` читается только здесь, на сервере: если бы он попал в клиентский
 * бандл, браузер узнал бы внутренний адрес API и мог обращаться к нему
 * напрямую — тогда BFF перестаёт быть единственной origin.
 */
export function buildTargetUrl(
  apiUrl: string,
  path: string[],
  search: string,
): ProxyTarget {
  const base = apiUrl.replace(/\/+$/, "");
  // CR 1.5, патч 13: `..` и `.` отбрасываются до кодирования. `encodeURIComponent`
  // превращает их в `%2E%2E`, но `fetch` нормализует путь после декодирования,
  // и `/api/proxy/..%2f..%2fopenapi.json` выходил за префикс `/api/v1`.
  // Сегментов с точками в API нет, поэтому отказ дешевле проверки результата.
  const safe = path.filter((segment) => segment !== ".." && segment !== ".");
  const joined = safe.map((segment) => encodeURIComponent(segment)).join("/");
  return { url: `${base}${API_PREFIX}/${joined}${search}` };
}

/**
 * Нужно ли обновлять сессию после ответа (Task 8.2; AC-02, AC-05, AC-06).
 *
 * Только `401`. `403` — это бан: обновление не поможет, а попытка создала бы
 * петлю refresh → 403 → refresh. Пути `auth/refresh` и `auth/logout`
 * исключены: обновление самой ротации или выхода зациклило бы запрос.
 */
export function isRefreshable(status: number, path: string[]): boolean {
  if (status !== 401) return false;
  const joined = path.join("/");
  return !NO_REFRESH_PATHS.includes(joined);
}

/**
 * Заголовки, которые прокси не должен пробрасывать (Task 7.5).
 *
 * `host` — иначе FastAPI получит чужой Host и построит неверные абсолютные
 * ссылки; `content-length` — тело пересобирается из `text()`, и старая длина
 * может разойтись с новой; `connection` управляет транспортом между
 * узлами, а не приложением.
 */
const DROPPED_REQUEST_HEADERS = new Set(["host", "content-length", "connection"]);

/**
 * Отбирает заголовки запроса для проброса (Task 7.5).
 *
 * Белый список транспорта вместо чёрного: заголовки вроде `x-forwarded-for`
 * или `origin` меняют поведение FastAPI (лимиты по IP, CORS) и не должны
 * приходить от браузера в обход BFF.
 */
const FORWARDED_REQUEST_HEADERS = ["content-type", "accept", "cookie", "accept-language"];

export function forwardRequestHeaders(source: Headers): Headers {
  const result = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = source.get(name);
    if (value !== null) result.set(name, value);
  }
  return result;
}

/**
 * Заголовки ответа, которые копируются клиенту (Task 7.8).
 *
 * `set-cookie` здесь отсутствует намеренно: его нельзя копировать через
 * `set`, иначе пара склеится в одну строку (D-13). `location` нужен для
 * OAuth-редиректов, `content-type` — чтобы браузер разобрал тело так же,
 * как его отдал FastAPI.
 *
 * **`retry-after` (CR 9.5 MEDIUM-2).** Без него `429 rate_limited` доходил
 * до формы БЕЗ числа: `support-form` читал заголовок и получал `null`, то
 * есть показывал «попробуйте позже» вместо «через N минут». Заголовок —
 * единственный носитель числа на клиенте (тело разбирается по `code`), и
 * терять его на BFF значит терять всю пользу лимита для человека.
 */
const COPIED_RESPONSE_HEADERS = ["content-type", "location", "www-authenticate", "retry-after"];

export function copyResponseHeaders(source: Headers): Headers {
  const result = new Headers();
  for (const name of COPIED_RESPONSE_HEADERS) {
    const value = source.get(name);
    if (value !== null) result.set(name, value);
  }
  // AC-08: ответы не кэшируются — иначе прокси отдаст токен из кэша
  // следующему запросу того же браузера.
  result.set("cache-control", "no-store");
  return result;
}

/**
 * Переносит `Set-Cookie` парами (Task 7.9; D-13).
 *
 * `headers.get("set-cookie")` склеивает пару через запятую, а `Expires`
 * тоже содержит запятую — браузер получает битую cookie. `getSetCookie()`
 * возвращает массив. Fallback нужен для сред без этого метода (Node < 20,
 * старые полифилы `fetch`).
 */
export function appendSetCookies(target: Headers, source: Headers): void {
  const cookies = readSetCookies(source);
  for (const cookie of cookies) target.append("set-cookie", cookie);
}

/**
 * Читает `Set-Cookie` массивом, с ручным разбором как fallback (CR 1.5, патч 9).
 *
 * Раньше при отсутствии `getSetCookie` возвращался пустой массив: ротированная
 * пара молча терялась, клиент повторял сгоревший токен и выкидывался из сессии
 * на каждом запросе. Fallback режет по запятой, за которой следует `имя=`,
 * а не по каждой запятой: `Expires=Wed, 21 Oct 2026 …` тоже содержит запятую,
 * и наивный `split(",")` ломал дату истечения.
 */
function readSetCookies(source: Headers): string[] {
  if (typeof source.getSetCookie === "function") {
    return source.getSetCookie();
  }
  const raw = source.get("set-cookie");
  if (raw === null) return [];
  // Граница печеньки — запятая, за которой идёт `token=` без пробела внутри
  // самого значения: `Expires` после запятой начинается с дня недели.
  return raw.split(/,(?=\s*[^;,=\s]+=)/).map((cookie) => cookie.trim());
}

/**
 * Извлекает access-токен из cookie для `Authorization` (Task 7.10).
 *
 * Нужен, когда FastAPI получает запрос без cookie: часть браузеров и
 * клиентов шлёт только заголовок. Порядок в зависимостях API — cookie
 * первым (D-2), поэтому подстановка заголовка не перекрывает cookie.
 */
export function bearerFromCookie(cookieHeader: string | null, name: string): string | null {
  if (!cookieHeader) return null;
  for (const part of cookieHeader.split(";")) {
    const [rawKey, ...rest] = part.split("=");
    if (rawKey?.trim() === name) {
      const value = rest.join("=").trim();
      if (value.length > 0) return value;
    }
  }
  return null;
}

/**
 * Накладывает `Set-Cookie` refresh-ответа на исходный заголовок `Cookie` (CR 1.5, патч 15).
 *
 * Раньше заголовок повтора собирался только из `Set-Cookie` обновления, и все
 * прочие cookie (`karamel_oauth`, любые будущие) в повтор не попадали.
 * Удаление (`Max-Age=0`) вычищает пару из результата, остальные имена
 * переносятся из исходного запроса без изменений.
 */
export function mergeSetCookies(
  cookieHeader: string | null,
  setCookies: string[],
): string {
  const jar = new Map<string, string>();
  if (cookieHeader) {
    for (const part of cookieHeader.split(";")) {
      const index = part.indexOf("=");
      if (index <= 0) continue;
      jar.set(part.slice(0, index).trim(), part.slice(index + 1).trim());
    }
  }
  for (const raw of setCookies) {
    const [pair] = raw.split(";");
    const index = pair.indexOf("=");
    if (index <= 0) continue;
    const name = pair.slice(0, index).trim();
    const value = pair.slice(index + 1).trim();
    if (/max-age=0/i.test(raw) || value === "") {
      jar.delete(name);
    } else {
      jar.set(name, value);
    }
  }
  return [...jar].map(([name, value]) => `${name}=${value}`).join("; ");
}

export { DROPPED_REQUEST_HEADERS };
