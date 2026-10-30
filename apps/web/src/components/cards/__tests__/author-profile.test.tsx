/**
 * Тесты `AuthorProfile` (стори 12.7, AC-6; UX §780-802, §919).
 *
 * **Проверяется состояние, а не разметка.** Проектный урок «заявление без
 * доказательства»: тест на наличие `<img>` доказывает, что тег написан, а не
 * что архивный экран объясняет пользователю, почему анкеты нет. Поэтому
 * утверждения:
 *
 * - данные → имя, возраст, город, описание видны;
 * - `null`-поля НЕ рисуют пустых строк (`Город:` без значения читался бы
 *   как сбой загрузки, §4 project-context);
 * - архив → дословный текст UX §919 и ссылка «Открыть ленту», анкеты нет;
 * - сбой сети → «не удалось загрузить», отличное от архива (P-26: пустой
 *   экран ≠ «недоступно»);
 * - даты рождения нет ни в одном состоянии (AC-2, 12.3).
 */

import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  AuthorProfile,
  type CardAuthorProfile,
  loadCardAuthorProfile,
} from "@/components/cards/author-profile";

const PROFILE: CardAuthorProfile = {
  uuid: "0f1c0000-0000-0000-0000-000000000001",
  name: "Анна",
  age: 28,
  city: "Томск",
  bio: "Люблю выставки и кофе без спешки",
  photo_url: "https://storage.example.test/p.jpg?X-Amz-Signature=x",
  photo_expires_in: 300,
};

describe("AuthorProfile: данные", () => {
  it("показывает имя, возраст, город и описание", () => {
    render(<AuthorProfile state={{ kind: "ok", data: PROFILE }} />);

    expect(screen.getByText(/Анна/)).toBeTruthy();
    expect(screen.getByText(/28/)).toBeTruthy();
    expect(screen.getByText(/Томск/)).toBeTruthy();
    expect(screen.getByText(/Люблю выставки/)).toBeTruthy();
  });

  it("не рисует пустых строк для null-полей", () => {
    render(
      <AuthorProfile
        state={{
          kind: "ok",
          data: { ...PROFILE, name: null, age: null, city: null, bio: null, photo_url: null },
        }}
      />,
    );

    // Мутация «рисовать `Город: {city}` без условия» — тест краснеет.
    expect(screen.queryByText(/Город:/)).toBeNull();
    expect(document.body.textContent).not.toContain("null");
  });

  it("не показывает дату рождения ни в каком виде (AC-2, 12.3)", () => {
    const { container } = render(<AuthorProfile state={{ kind: "ok", data: PROFILE }} />);

    expect(container.textContent ?? "").not.toContain("birth_date");
    expect(container.textContent ?? "").not.toContain("Дата рождения");
  });
});

describe("AuthorProfile: архив (AC-6, UX §919)", () => {
  it("показывает дословный текст UX и ссылку на ленту", () => {
    render(<AuthorProfile state={{ kind: "archived" }} />);

    expect(
      screen.getByText(/Эта встреча завершена. Анкета автора больше не показывается./),
    ).toBeTruthy();
    expect(screen.getByText("Открыть ленту").closest("a")?.getAttribute("href")).toBe("/app");
  });

  it("не показывает анкету на архиве", () => {
    render(<AuthorProfile state={{ kind: "archived" }} />);

    expect(screen.queryByText(/Люблю выставки/)).toBeNull();
  });
});

describe("гейт AC-6 (Task 4.3): источник витрины — только карточка", () => {
  it("не содержит адреса /profiles/ и содержит адрес карточки", async () => {
    // Замок на ИСХОДНИК, а не на разметку: мутация U19 (заменить URL на
    // `/api/v1/profiles/{uuid}`) не красила ни один тест поведения, потому
    // что они рендерят готовое состояние и до `fetch` не доходят. ADR-22
    // запрещает постоянную витрину анкеты; единственный адрес — карточка.
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const source = readFileSync(
      join(process.cwd(), "src/components/cards/author-profile.tsx"),
      "utf8",
    );

    expect(source).not.toMatch(/\/profiles\//);
    // Положительная половина (P-21): адрес карточки в файле есть — иначе
    // тест проходил бы на пустом файле.
    expect(source).toMatch(/\/cards\/\$\{encodeURIComponent\(cardUuid\)\}\/author/);
  });
});

describe("AuthorProfile: null-поля не создают пустых блоков (CR M27)", () => {
  it("описание null — абзаца описания в DOM нет", () => {
    // Найдено мутацией CR M27 (`{profile.bio ? ... : null}` → `{true ? ...}`):
    // React рисует `{null}` как ничего, поэтому `textContent` не меняется
    // и текстовый ассерт молчит. Различие — в СТРУКТУРЕ: пустой `<p>` читается
    // как незагрузившийся блок, а `null` — как «поля нет» (§4 project-context).
    const { container } = render(
      <AuthorProfile state={{ kind: "ok", data: { ...PROFILE, bio: null } }} />,
    );

    const paragraphs = Array.from(container.querySelectorAll("p"));
    expect(paragraphs.some((p) => (p.textContent ?? "").includes("Люблю"))).toBe(false);
    // Пустых абзацев не появляется вовсе: их отсутствие и есть отличие.
    expect(paragraphs.filter((p) => (p.textContent ?? "").trim() === "")).toHaveLength(0);
  });
});

describe("loadCardAuthorProfile: разбор ответов API (CR M24, M28)", () => {
  // Покрывается САМ загрузчик: до CR ни один тест до `fetch` не доходил,
  // поэтому подмена `404 → unavailable` (M24) и проглатывание `5xx` (M28)
  // оставались незамеченными. Поведенческие тесты рендерят готовое
  // состояние и о сетевой ветке ничего не говорят.
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  function stubFetch(response: { status: number; json?: () => Promise<unknown> }): void {
    globalThis.fetch = (async () => ({
      status: response.status,
      ok: response.status >= 200 && response.status < 300,
      json: response.json ?? (async () => ({})),
    })) as unknown as typeof fetch;
  }

  it("404 → archived, а НЕ unavailable (M24)", async () => {
    stubFetch({ status: 404 });

    const state = await loadCardAuthorProfile("0f1c-1", null);

    // Архив штатен: карточка открывается `200` (FR73), анкета закрыта.
    // «Недоступно» предложило бы обновить страницу — действие, которое
    // не поможет и читается как сбой сервиса.
    expect(state.kind).toBe("archived");
  });

  it("500 → unavailable, а не ok и не archived (M28)", async () => {
    stubFetch({ status: 500, json: async () => ({ code: "internal" }) });

    const state = await loadCardAuthorProfile("0f1c-1", null);

    expect(state.kind).toBe("unavailable");
  });

  it("исключение сети → unavailable (не падение страницы)", async () => {
    globalThis.fetch = (async () => {
      throw new Error("ECONNREFUSED");
    }) as unknown as typeof fetch;

    const state = await loadCardAuthorProfile("0f1c-1", null);

    expect(state.kind).toBe("unavailable");
  });

  it("200 → ok с разобранным телом и без запроса по голому uuid", async () => {
    stubFetch({ status: 200, json: async () => PROFILE });
    const calls: string[] = [];
    globalThis.fetch = (async (url: string) => {
      calls.push(String(url));
      return { status: 200, ok: true, json: async () => PROFILE };
    }) as unknown as typeof fetch;

    const state = await loadCardAuthorProfile("0f1c-1", "a=b");

    expect(state).toEqual({ kind: "ok", data: PROFILE });
    // AC-6, ADR-22: единственный адрес — карточка.
    expect(calls[0]).toContain("/cards/0f1c-1/author");
    expect(calls[0]).not.toContain("/profiles/");
  });
});

describe("AuthorProfile: сбой сети (P-26)", () => {
  it("отличается от архива: просит обновить, а не сообщает о завершении", () => {
    render(<AuthorProfile state={{ kind: "unavailable" }} />);

    expect(screen.getByText(/Не удалось загрузить анкету/)).toBeTruthy();
    // Мутация «слить unavailable с archived» — тест краснеет.
    expect(screen.queryByText(/Анкета автора больше не показывается/)).toBeNull();
  });
});
