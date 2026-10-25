// @vitest-environment node
/**
 * Тест пробрасывания заголовков ответа через BFF (CR 9.5 MEDIUM-2).
 *
 * **Почему именно этот тест.** CR нашёл разрыв ровно на стыке слоёв: API
 * отдаёт `429` с `Retry-After`, форма читает заголовок, но BFF его не
 * копировал — и человек видел «попробуйте позже» вместо «через N минут».
 * Ни один существующий тест этот стык не покрывал: бэкенд-тесты смотрят на
 * прямой ответ API, фронтовые — на компоненты без прокси.
 *
 * **Почему `node`-окружение.** `Headers` — стандартный глобал Node 18+;
 * в jsdom он тоже есть, но тест проверяет Transport-функцию, которой jsdom
 * не нужен, и `node` ближе к тому, где она реально исполняется.
 */

import { describe, expect, it } from "vitest";

import { copyResponseHeaders } from "../bff-proxy";

describe("copyResponseHeaders", () => {
  it("пробрасывает retry-after: без него 429 теряет число", () => {
    // MEDIUM-2: заголовок — единственный носитель числа на клиенте.
    const source = new Headers({ "retry-after": "3600" });

    const copied = copyResponseHeaders(source);

    expect(copied.get("retry-after")).toBe("3600");
  });

  it("сохраняет прежние пробрасываемые заголовки", () => {
    const source = new Headers({
      "content-type": "application/json",
      location: "/app/profile",
      "www-authenticate": 'Bearer realm="karamel"',
    });

    const copied = copyResponseHeaders(source);

    expect(copied.get("content-type")).toBe("application/json");
    expect(copied.get("location")).toBe("/app/profile");
    expect(copied.get("www-authenticate")).toBe('Bearer realm="karamel"');
  });

  it("не пробрасывает чужие заголовки и всегда ставит no-store", () => {
    // Белый список, а не копирование всего: `x-internal-debug` из upstream
    // не должен утечь клиенту.
    const source = new Headers({ "x-internal-debug": "trace-id", server: "uvicorn" });

    const copied = copyResponseHeaders(source);

    expect(copied.get("x-internal-debug")).toBeNull();
    expect(copied.get("server")).toBeNull();
    expect(copied.get("cache-control")).toBe("no-store");
  });
});
