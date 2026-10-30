/**
 * Страница метрик: ответ `422 period_too_old` (стори 11.7; AC-8, P-26).
 *
 * **Что именно проверяется.** До 11.7 `loadMetrics` различал только `403`,
 * всё остальное сливалось в `unavailable` → текст «Не удалось загрузить
 * метрики. Обновите страницу». С нижней границей окна сервер начал отвечать
 * `422 period_too_old` на начало окна старше шести месяцев, и администратор
 * увидел бы ЛОЖНЫЙ сбой сервиса там, где дело в выбранной дате: он пошёл бы
 * обновлять страницу вместо того, чтобы выбрать доступный период.
 *
 * **Почему рендер, а не чтение исходника.** Утверждение AC-8 — про то, ЧТО
 * увидит человек. Гард по тексту `page.tsx` прошёл бы и на недостижимой
 * ветке: код может разбирать `422` и никогда не показать сообщение. Рендер
 * проверяет связку «ответ сервера → состояние → экран».
 *
 * **Почему моки `next/headers` и `fetch`.** Страница серверная: она читает
 * cookie и ходит в API. Тест без них сделал бы настоящий запрос (в jsdom его
 * нет) и упал бы на `cookies()` до проверки утверждения.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

// `vi.hoisted`: `vi.mock` поднимается сборщиком выше объявлений файла, и
// обычная `const` оказалась бы не инициализирована в момент подмены модуля
// (`Cannot access ... before initialization`).
const COOKIES_MOCK = vi.hoisted(() => vi.fn(async () => ({ getAll: () => [] })));

vi.mock("next/headers", () => ({ cookies: COOKIES_MOCK }));

import PilotMetricsPage from "@/app/(mod)/mod/metrics/page";

/** Окно внутри срока хранения: разбор адреса не должен поднимать `ignoredParams`. */
const SEARCH_PARAMS = Promise.resolve({ from: "2026-05-01", to: "2026-05-15" });

/**
 * HTML страницы при ответе API с указанным статусом и телом.
 *
 * `fetch` подменяется целиком: страница обязана получить ровно тот ответ,
 * который отдаёт роутер (`{"code": "period_too_old"}`), а не пересказ.
 */
async function renderWithResponse(status: number, body: unknown): Promise<string> {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  try {
    const element = await PilotMetricsPage({ searchParams: SEARCH_PARAMS });
    return renderToStaticMarkup(element);
  } finally {
    vi.unstubAllGlobals();
  }
}

describe("страница метрик — окно старше срока хранения (AC-8)", () => {
  beforeEach(() => {
    COOKIES_MOCK.mockClear();
  });

  it("422 period_too_old → сообщение про период, а не про недоступный сервис", async () => {
    const html = await renderWithResponse(422, { code: "period_too_old" });

    // Своё сообщение: человек понимает, что выбрать.
    expect(html).toContain("Начало окна старше 6 месяцев");
    expect(html).toContain("не раньше последних шести месяцев");
    // И это НЕ текст сбоя сервиса — иначе AC-8 не выполнен.
    expect(html).not.toContain("Не удалось загрузить метрики");
  });

  it("422 с другим кодом остаётся недоступностью, а не «старым окном»", async () => {
    // `invalid_period` сюда доходить не должен (UI строит валидное окно), но
    // если дойдёт — это дефект контракта, а не выбранная человеком дата:
    // сообщение «выберите период заново» здесь было бы ложью.
    const html = await renderWithResponse(422, { code: "invalid_period" });

    expect(html).toContain("Не удалось загрузить метрики");
    expect(html).not.toContain("Начало окна старше 6 месяцев");
  });

  it("403 остаётся отказом по роли", async () => {
    // Регресс-замок: ветка `403` не должна была пострадать от новой ветки `422`.
    const html = await renderWithResponse(403, { code: "forbidden" });

    expect(html).toContain("Недостаточно прав");
  });
});
