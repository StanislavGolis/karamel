// @vitest-environment node
/**
 * Публичный маршрут мониторинга `/api/health` (стори 11.2, AC-3.2, AC-4.1).
 *
 * **Почему эти тесты существуют.** До стори 11.2 у web не было ни одного
 * публичного маршрута о доступности; единственный `/api/proxy/*` тянет cookie
 * и ходит в `/api/v1/*`, поэтому внешний монитор получал бы на нём 401 и
 * cookie в запросе — прямое нарушение AC-3. Маршрут создаётся ради AC-1 и
 * дешевле всего проверяется здесь.
 *
 * **Почему `node`-окружение.** Обработчик живёт на сервере Next: `Response`
 * и `fetch` — стандартные глобалы Node 22. В jsdom тест проверял бы заглушки.
 *
 * **Почему часть тестов идёт на реальном `http.Server`, а не на моке.**
 * CR 2026-10-27 показал: мок `fetch` не различает `redirect: "manual"` и
 * снятие таймаута — мутации M15/M16/M20 выживали (8/8 passed). `fetch` — это
 * стандартный глобал Node, и поведение «следует редиректу» и «висит без
 * таймаута» воспроизводится только живым сервером. Правило 18.2: ассерт,
 * который не может упасть по делу, хуже отсутствующего.
 *
 * **Чего тест не проверяет.** Реальную доступность домена — это делает Task 1
 * замером на стенде (правило 18.5): маршрут, зелёный локально и 404 на
 * домене, — ровно тот дефект, ради которого стори существует.
 */

import { readFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET } from "../route";

/**
 * Минимальный ответ upstream. `Headers` без `set-cookie` — эталон для T4:
 * маршрут мониторинга не имеет права раздавать cookie даже если upstream их
 * прислал.
 */
function upstream(status: number, body: unknown = { status: "ok" }): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("GET /api/health", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => upstream(200));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("API_URL", "http://api:8000");
  });

  afterEach(() => {
    // N-6: снимаем и env, и подменённый глобал. Прежняя версия снимала только
    // env — подмена `fetch` протекала в соседние файлы и давала плавающие
    // падения, не связанные с проверяемым кодом.
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("W-11.2-T1: при 200 от api отдаёт 200 и ровно {status:'ok'}", async () => {
    // T1: тело — контракт для монитора и для человека, читающего curl.
    // Лишний ключ (`version`, `uptime`, `host`, имя сервиса) превращает
    // health в источник сведений о внутренностях стенда.
    const response = await GET();

    expect(response.status).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["status"]);
    expect(body.status).toBe("ok");
  });

  it("W-11.2-T2: при 500 от api отдаёт 503, а не 200", async () => {
    // T2: монитор судит по HTTP-коду (D-11.2-2). «200 с плохим телом» =
    // вечный зелёный. Убрать проверку `response.ok` — тест обязан покраснеть.
    fetchMock.mockResolvedValueOnce(upstream(500, { status: "error" }));

    const response = await GET();

    expect(response.status).toBe(503);
    const body = (await response.json()) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["status"]);
    expect(body.status).toBe("unavailable");
  });

  it("W-11.2-T2: при отказе соединения отдаёт 503", async () => {
    // T2 (вторая ветка): api лежит — `fetch` бросает, а не возвращает ответ.
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));

    const response = await GET();

    expect(response.status).toBe(503);
    expect(((await response.json()) as Record<string, unknown>).status).toBe(
      "unavailable",
    );
  });

  it("W-11.2-T2: при таймауте отдаёт 503", async () => {
    // T2 (третья ветка): таймаут приходит как `TimeoutError` из
    // `AbortSignal.timeout`. Обработчик обязан отличить его от 200 и
    // не пробросить исключение наружу (иначе Next отдаст 500).
    // Поведенческий случай «сервер молчит» — отдельный тест T2b ниже.
    fetchMock.mockRejectedValueOnce(
      new DOMException("The operation was aborted due to timeout", "TimeoutError"),
    );

    const response = await GET();

    expect(response.status).toBe(503);
  });

  it("W-11.2-T3: запрос к api — GET на /health без заголовков авторизации", async () => {
    // T3: AC-3 — в запросе нет ни токенов, ни cookie, ни `X-*`.
    // Монитор получает только URL и метод; маршрут не должен добавлять
    // к этому ничего от себя. Добавить `Authorization` — тест краснеет.
    await GET();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toMatch(/\/health$/);
    expect(init.method).toBe("GET");

    const headers = new Headers(init.headers as HeadersInit | undefined);
    const names = [...headers.keys()].map((name) => name.toLowerCase());
    expect(names).not.toContain("authorization");
    expect(names).not.toContain("cookie");
    expect(names.filter((name) => name.startsWith("x-"))).toEqual([]);
  });

  it("W-11.2-T4: ответ не раздаёт cookie и не раскрывает внутренности", async () => {
    // T4: AC-3.2 — тело ровно из одного ключа, и заголовки без `Set-Cookie`.
    // Если upstream пришлёт cookie, маршрут обязан их не пробрасывать:
    // иначе монитор (внешний сервис) получал бы сессионные данные.
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: {
          "content-type": "application/json",
          "set-cookie": "karamel_access=secret; Path=/",
        },
      }),
    );

    const response = await GET();

    expect(response.headers.get("set-cookie")).toBeNull();
    const text = await response.text();
    expect(text).not.toMatch(/host|version|uptime|postgres|redis|api:8000/i);
  });

  it("W-11.2-T5: два вызова дают два запроса к api (маршрут не кэшируется)", async () => {
    // T5: D-11.2-3. Кэшированный health не замечает падения — это тавтология
    // (правило 18.2). Мутация: модульный кэш ответа или `cache: "force-cache"`
    // вместо `no-store` → счётчик вызовов станет 1 и тест покраснеет.
    await GET();
    await GET();

    expect(fetchMock).toHaveBeenCalledTimes(2);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.cache).toBe("no-store");
  });

  it("W-11.2-T5: в файле маршрута нет кэширующих директив", async () => {
    // T5 (вторая ветка): сегментные опции (`dynamic`, `runtime`) в проекте
    // запрещены — `next.config.ts` включает `cacheComponents`, и `next build`
    // падал на обеих. Поэтому кэш проверяется по факту: при `cacheComponents`
    // GET-route handler динамический по умолчанию, а кэш включается только
    // явной директивой. Появится `"use cache"` или `force-cache` — тест краснеет.
    const raw = readFileSync(
      // N-5: `fileURLToPath`, а не `.pathname`. `pathname` оставляет
      // percent-encoding (`%20`), и на пути с пробелом тест падал бы как
      // «файл не найден» — то есть врал бы о причине.
      fileURLToPath(new URL("../route.ts", import.meta.url)),
      "utf8",
    );
    // Комментарии срезаются до проверки: объяснение в шапке само упоминает
    // `"use cache"` и `force-cache`, и гард по сырому тексту красился бы на
    // верном коде. Проверяется код, а не проза вокруг него.
    const source = raw
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .split("\n")
      .filter((line) => !line.trimStart().startsWith("//"))
      .join("\n");

    expect(source).not.toMatch(/"use cache"/);
    expect(source).not.toMatch(/cache:\s*"force-cache"/);
    expect(source).toMatch(/cache: "no-store"/);
  });

  it("W-11.2-T7: ответ 200 и 503 несут cache-control: no-store", async () => {
    // N-1 (слепая зона M17): заголовок ответа не проверялся ни одним ассертом,
    // его удаление проходило зелёным. Промежуточный кэш (Caddy, корпоративный
    // прокси) запомнил бы 200 и отдавал его при мёртвом api.
    const ok = await GET();
    expect(ok.headers.get("cache-control")).toBe("no-store");

    fetchMock.mockResolvedValueOnce(upstream(500));
    const failed = await GET();
    expect(failed.headers.get("cache-control")).toBe("no-store");
  });

  it("W-11.2-T8: 3xx от api — недоступность, а не зелёный", async () => {
    // N-2: `fetch` по умолчанию следует редиректам, и 302 в captive-portal
    // или `http→https` редирект зеленел монитор при мёртвом api. Здесь
    // проверяется трактовка кода; сам `redirect: "manual"` доказывается
    // поведенческим тестом T9 на живом сервере.
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 302 }));
    expect((await GET()).status).toBe(503);

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 308 }));
    expect((await GET()).status).toBe(503);
  });

  it("W-11.2-T10: пустой API_URL — недоступность с записью причины", async () => {
    // N-4: `??` не ловил пустую строку и пробел — маршрут строил ОТНОСИТЕЛЬНЫЙ
    // `/health`, `fetch` бросал, и монитор краснел навсегда без следа причины.
    // Мутация: вернуть `?? "http://127.0.0.1:8000"` — тест краснеет на обоих
    // значениях (пустая строка и пробел).
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    vi.stubEnv("API_URL", "");
    const empty = await GET();
    expect(empty.status).toBe(503);
    expect(fetchMock).not.toHaveBeenCalled();

    vi.stubEnv("API_URL", "   ");
    const blank = await GET();
    expect(blank.status).toBe(503);
    expect(fetchMock).not.toHaveBeenCalled();

    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it("W-11.2-T10: API_URL не задан — не ходит на самого себя", async () => {
    // N-1 (слепая зона M18): прежний дефолт `http://127.0.0.1:8000` внутри
    // контейнера web указывает на сам web (api слушает 8000 в своём
    // контейнере). Дефолт удалён; отсутствие переменной — явная недоступность.
    // Мутация: вернуть дефолт → тест краснеет (fetch будет вызван).
    vi.stubEnv("API_URL", undefined as unknown as string);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const response = await GET();

    expect(response.status).toBe(503);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it("W-11.2-T11: трейлинг-слэш в API_URL не даёт двойного слэша", async () => {
    // N-1 (слепая зона M20): `.replace(/\/+$/, "")` не был покрыт. При
    // `API_URL=http://api:8000/` запрос уходил на `//health`, который api не
    // обслуживает — монитор краснел при живом api.
    // Мутация: снять замену → тест краснеет на `//health`.
    vi.stubEnv("API_URL", "http://api:8000/");
    await GET();
    let [url] = fetchMock.mock.calls[0] as [string];
    expect(String(url)).toBe("http://api:8000/health");

    fetchMock.mockClear();
    vi.stubEnv("API_URL", "http://api:8000///");
    await GET();
    [url] = fetchMock.mock.calls[0] as [string];
    expect(String(url)).toBe("http://api:8000/health");
  });

  it("W-11.2-T12: тело ответа upstream закрывается, а не остаётся в пуле", async () => {
    // N-3: на каждый опрос оставался занятый сокет. Мутация: убрать
    // `discardBody` — флаг `cancelled` останется false, тест краснеет.
    let cancelled = false;
    const stream = new ReadableStream({
      pull(controller) {
        controller.enqueue(new TextEncoder().encode("{\"status\":\"ok\"}"));
        controller.close();
      },
      cancel() {
        cancelled = true;
      },
    });
    fetchMock.mockResolvedValueOnce(
      new Response(stream, { status: 200, headers: { "content-type": "application/json" } }),
    );

    const response = await GET();

    expect(response.status).toBe(200);
    expect(cancelled).toBe(true);
  });
});

/**
 * Поведенческие тесты на живом `http.Server`.
 *
 * **Почему без мока `fetch`.** CR 2026-10-27 доказал probe-ом: с моком
 * снятие `redirect: "manual"` и снятие `AbortSignal.timeout` проходят зелёными
 * (M15/M16/M20 — 8/8 passed). Оба свойства — поведение стандартного `fetch`,
 * а не нашего кода, и проверяются только реальным сокетом (правило 18.2).
 */
describe("GET /api/health — поведение с живым api", () => {
  let server: Server | undefined;
  let baseUrl = "";

  afterEach(async () => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    await new Promise<void>((resolve) => {
      if (!server) return resolve();
      server.closeAllConnections?.();
      server.close(() => resolve());
      server = undefined;
    });
  });

  /** Поднимает сервер на свободном порту и возвращает его адрес. */
  async function listen(handler: Parameters<typeof createServer>[1]): Promise<string> {
    server = createServer(handler);
    await new Promise<void>((resolve) => server?.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;
    return `http://127.0.0.1:${port}`;
  }

  it(
    "W-11.2-T9: редирект api (302 → 200) не зеленет монитор",
    async () => {
      // N-2: MAJOR. Без `redirect: "manual"` `fetch` переходит по 302 и
      // возвращает 200 — монитор зелёный при мёртвом api, который отдаёт
      // редирект (captive-portal, `http→https`, сломанный прокси).
      // Мутация: убрать `redirect: "manual"` → тест краснеет (200 вместо 503).
      baseUrl = await listen((req, res) => {
        if (req.url === "/health") {
          res.writeHead(302, { location: "/portal" });
          res.end();
          return;
        }
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ status: "ok" }));
      });
      vi.stubEnv("API_URL", baseUrl);

      const response = await GET();

      expect(response.status).toBe(503);
      expect(((await response.json()) as Record<string, unknown>).status).toBe(
        "unavailable",
      );
    },
    15_000,
  );

  it(
    "W-11.2-T2b: зависший api отдаёт 503 за таймаут, а не висит",
    async () => {
      // N-1 (слепая зона M15/M16): сервер принимает соединение и молчит.
      // Мутация «убрать AbortSignal.timeout» → запрос висит, тест падает по
      // таймауту (15 с) вместо зелёного через 5 с.
      // Мутация «5_000 → 60_000» → то же самое.
      baseUrl = await listen(() => {
        // Намеренно не отвечаем: имитация api, который принял соединение и встал.
      });
      vi.stubEnv("API_URL", baseUrl);

      const startedAt = Date.now();
      const response = await GET();
      const elapsed = Date.now() - startedAt;

      expect(response.status).toBe(503);
      // Ждём не дольше таймаута маршрута: 5 с — это 5000, даём запас на CI.
      expect(elapsed).toBeLessThan(9_000);
      expect(elapsed).toBeGreaterThanOrEqual(4_000);
    },
    15_000,
  );

  it("W-11.2-T13: живой api 200 → наш 200", async () => {
    // Контроль к двум тестам выше: без него «всегда 503» проходил бы всё.
    baseUrl = await listen((_req, res) => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ status: "ok" }));
    });
    vi.stubEnv("API_URL", baseUrl);

    const response = await GET();

    expect(response.status).toBe(200);
    expect(((await response.json()) as Record<string, unknown>).status).toBe("ok");
  });
});
