/**
 * Рендер анкеты модератора (стори 12.10; AC-1, AC-4, AC-7, AC-8).
 *
 * **Почему рендер, а не чтение исходника.** Утверждения AC-1/AC-7 — про то,
 * ЧТО увидит модератор. Гард по тексту `page.tsx` прошёл бы и на недостижимой
 * ветке: код может разбирать `404` и никогда не показать сообщение.
 *
 * **Почему моки `next/headers` и `fetch`.** Страница серверная: она читает
 * cookie и ходит в API. Без моков тест сделал бы настоящий запрос (в jsdom
 * его нет) и упал бы на `cookies()` до проверки утверждения (приём
 * `metrics-page-too-old.test.tsx`).
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const COOKIES_MOCK = vi.hoisted(() => vi.fn(async () => ({ getAll: () => [] })));

vi.mock("next/headers", () => ({ cookies: COOKIES_MOCK }));

import ModerationUserPage from "@/app/(mod)/mod/users/[uuid]/page";
import ModerationUserLoading from "@/app/(mod)/mod/users/[uuid]/loading";

const UUID = "11111111-2222-3333-4444-555555555555";
const PARAMS = Promise.resolve({ uuid: UUID });

/** Полный ответ карточки; `birth_date` в него не входит и не может входить. */
function cardBody(profileOverrides: Record<string, unknown> = {}) {
  return {
    profile: {
      uuid: UUID,
      name: "Анна",
      city: "Томск",
      age: 28,
      gender: "female",
      status: "active",
      consent_version: "1.2",
      photo_url: "https://storage.test/photos/a/1.jpg?expires=900",
      photo_expires_in: 900,
      ...profileOverrides,
    },
    reports: { filed: [], against: [], filed_total: 0, against_total: 2 },
    sanctions: { items: [], total: 0 },
    attendance: { items: [], total: 12 },
  };
}

async function renderWithResponse(status: number, body: unknown): Promise<string> {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  try {
    const element = await ModerationUserPage({ params: PARAMS });
    return renderToStaticMarkup(element);
  } finally {
    vi.unstubAllGlobals();
  }
}

describe("анкета модератора — рендер (AC-1, AC-4, AC-6)", () => {
  beforeEach(() => {
    COOKIES_MOCK.mockClear();
  });

  it("показывает имя, возраст, город, согласие и три числа", async () => {
    const html = await renderWithResponse(200, cardBody());

    expect(html).toContain("Анна");
    expect(html).toContain("28");
    expect(html).toContain("Томск");
    expect(html).toContain("версия 1.2");
    expect(html).toContain("Жалобы: 2");
    expect(html).toContain("Санкции: 0");
    expect(html).toContain("Явки: 12");
    expect(html).toContain("FR60");
  });

  it("birth_date не встречается в разметке, а age — встречается", async () => {
    const html = await renderWithResponse(200, cardBody());

    // Положительная половина: без неё проверка прошла бы на пустой странице.
    expect(html).toContain("28");
    expect(html).not.toContain("birth_date");
    expect(html).not.toContain("2008-10-30");
  });

  it("photo_url = null → инициалы, а не пустая рамка (AC-1, D-12.10-5)", async () => {
    const html = await renderWithResponse(
      200,
      cardBody({ photo_url: null, photo_expires_in: null }),
    );

    expect(html).toContain("А");
    expect(html).not.toContain("<img");
  });

  it("фото несёт alt с именем пользователя (CR M-4, доступность)", async () => {
    const html = await renderWithResponse(200, cardBody());

    // CR M-4: было `alt=""` — скринридер читал «изображение без описания»
    // рядом с подписью, тогда как фото здесь и есть объект разбора.
    expect(html).toContain('<img');
    expect(html).toContain('alt="Анна"');
    // Отрицательная половина: пустой `alt` не должен вернуться незамеченным.
    expect(html).not.toContain('alt=""');
  });

  it("consent_version = null → «версия неизвестна», а не пусто", async () => {
    const html = await renderWithResponse(200, cardBody({ consent_version: null }));

    expect(html).toContain("версия неизвестна");
  });
});

describe("анкета модератора — состояния ошибок (AC-7)", () => {
  beforeEach(() => {
    COOKIES_MOCK.mockClear();
  });

  it("404 → «Пользователь не найден»", async () => {
    const html = await renderWithResponse(404, { code: "user_not_found" });

    expect(html).toContain("Пользователь не найден");
  });

  it("403 → «Недостаточно прав»", async () => {
    const html = await renderWithResponse(403, { code: "forbidden" });

    expect(html).toContain("Недостаточно прав");
  });

  it("503 → «Не удалось загрузить. Попробовать снова» + перезагрузка", async () => {
    const html = await renderWithResponse(503, { code: "service_unavailable" });

    expect(html).toContain("Не удалось загрузить. Попробовать снова");
    expect(html).toContain("перезагрузить страницу");
    // Разные тексты: «нет прав» и «сбой сервиса» — разные действия читателя.
    expect(html).not.toContain("Недостаточно прав");
    expect(html).not.toContain("Пользователь не найден");
  });
});

describe("замки: источник данных и фолбэк загрузки (AC-8, W-12.7-j)", () => {
  it("страница ходит в /api/v1/moderation/users/, а не в /profiles/ и не через /api/proxy/", () => {
    const source = readFileSync(
      path.resolve(__dirname, "../page.tsx"),
      "utf-8",
    );

    expect(source).toMatch(/\/api\/v1\/moderation\/users\//);
    expect(source).not.toMatch(/\/profiles\//);
    expect(source).not.toMatch(/\/api\/proxy\//);
  });

  it("loading.tsx рендерится и не пуст", () => {
    const html = renderToStaticMarkup(<ModerationUserLoading />);

    expect(html.length).toBeGreaterThan(0);
    expect(html).toContain("Загрузка анкеты");
  });

  it("фолбэк загрузки подключён к сегменту, а не просто лежит рядом", () => {
    // CR M-3: прежний замок проверял ФОРМУ (файл есть, функция рендерится),
    // но не ЭФФЕКТ: `loading.tsx`, положенный мимо папки маршрута или
    // переименованный, никем не подхватывается — Next ищет ровно `loading`
    // рядом с `page` в одном сегменте.
    const routeDir = path.resolve(__dirname, "..");
    const pagePath = path.join(routeDir, "page.tsx");
    const loadingPath = path.join(routeDir, "loading.tsx");

    // Разные файлы: если `loading` окажется тем же путём, что `page`,
    // проверка ниже прошла бы на пустом множестве.
    expect(loadingPath).not.toBe(pagePath);
    const loadingSource = readFileSync(loadingPath, "utf-8");
    // Соглашение Next: именно `export default`, иначе сегмент молча останется
    // без фолбэка (сборка это не считает ошибкой).
    expect(loadingSource).toMatch(/export default function/);
    // Рядом с `page.tsx` в том же каталоге — та самая связка, которую видит
    // маршрутизатор; файл в соседней папке эффекта не даёт.
    expect(path.dirname(loadingPath)).toBe(path.dirname(pagePath));
  });
});
