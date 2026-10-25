/**
 * Состояния `ChatScreen` §8.3 (долг L-DS4-3, волна 4).
 *
 * Волна 4 добавила пустоту и ошибку отправки, но НЕ добавила тестов
 * (143 → 143). По уроку ER эпика 9 («заявление без доказательства»)
 * это значит: блок можно удалить молча, и ни один тест не покраснеет.
 *
 * Здесь проверяются именно состояния, а не факт рендера:
 * - пустая история → `EmptyState` «Напишите первым» (не пустой `<ul>`);
 * - отказ отправки → `role="alert"` + действие «Повторить» + сохранённый черновик.
 *
 * Загрузка (скелетоны с `aria-busy`) уже покрыта в `chat-screen.test.tsx`.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatScreen } from "@/components/chat/chat-screen";

const CHAT_UUID = "11111111-1111-4111-8111-111111111111";
const VIEWER_UUID = "22222222-2222-4222-8222-222222222222";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ChatScreen: состояния §8.3", () => {
  it("пустая история даёт EmptyState «Напишите первым», а не пустой список", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null })),
    );

    render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} />);

    // Заголовок пустоты — именно `h2` из `EmptyState` (§7.8): самописная
    // плашка обычно `<p>`/`<div>`, и тогда список выглядел бы «без ничего».
    expect(
      await screen.findByRole("heading", { level: 2, name: "Напишите первым" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Сообщений пока нет/)).toBeInTheDocument();
    // Пустой `<ul>` — ровно то, что §8.3 запрещает: экран без объяснения.
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("отказ отправки даёт alert «Сообщение не отправлено» и кнопку «Повторить»", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if ((init?.method ?? "GET") === "POST" && url.includes("/messages")) {
        return jsonResponse({ code: "service_unavailable" }, 503);
      }
      return jsonResponse({ items: [], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ChatScreen chatUuid={CHAT_UUID} viewerUuid={VIEWER_UUID} />);

    const textarea = await screen.findByLabelText("Сообщение");
    await userEvent.type(textarea, "Привет");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    // §8.3: ошибка отправки — текст ПЛЮС действие повтора, а не молчание.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Сообщение не отправлено");
    // NFR19: «сервис недоступен» отличается от «нет доступа» — текст честный.
    expect(alert).toHaveTextContent("Сервис недоступен");
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
    // UX 955–965: текст человека не стирается из-за отказа сети.
    expect((textarea as HTMLTextAreaElement).value).toBe("Привет");
  });
});
