/**
 * Состояние ошибки `ChatList` §8.3 (долг L-DS4-3, волна 4).
 *
 * Волна 4 заменила голую строку ошибки на `EmptyState` с действием повтора.
 * Теста не было: `ChatList` можно вернуть к `<p>`, и прогон останется зелёным.
 *
 * Проверяется ТРИ части одного состояния (ADR-13, §8.3):
 * 1) заголовок `EmptyState` (что случилось);
 * 2) причина из кода ответа (что делать) — для неизвестного кода это
 *    «Проверьте связь и попробуйте снова.»;
 * 3) действие «Повторить» — иначе экран ошибки тупиковый.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatList } from "@/components/chat/chat-list";

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn(), refresh: vi.fn() }),
}));

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

describe("ChatList: состояние ошибки §8.3", () => {
  it("неизвестный код → заголовок, причина и кнопка «Повторить»", async () => {
    // `server_error` не входит в `errorMessage` switch: попадает в `default`.
    // Именно эта ветка — страховка от «нового кода с сервера», и без неё
    // пользователь увидел бы пустое объяснение.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ code: "server_error" }, 500)),
    );

    render(<ChatList />);

    // Заголовок — `h2` из `EmptyState` (§7.8), не `<p>`: самописная строка
    // не даёт ни причины, ни действия.
    const heading = await screen.findByRole("heading", {
      level: 2,
      name: "Не удалось загрузить чаты",
    });
    expect(heading).toBeInTheDocument();
    // §8.3: причина НЕ повторяет заголовок — тест отличает их.
    expect(screen.getByText("Проверьте связь и попробуйте снова.")).toBeInTheDocument();
    // Действие повтора обязательно: без него ошибка — тупик.
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });

  it("«Повторить» перезапрашивает список, а не просто закрывает ошибку", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ code: "server_error" }, 500))
      .mockResolvedValueOnce(jsonResponse({ items: [], total: 0, cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    render(<ChatList />);

    const retry = await screen.findByRole("button", { name: "Повторить" });
    const before = fetchMock.mock.calls.length;
    await userEvent.click(retry);

    // Успешный повтор уводит из состояния ошибки в пустоту: это доказывает,
    // что `onAction` действительно вызвал `refresh`, а не сбросил состояние
    // без запроса (`state.kind = "loading"` без `fetch` дал бы вечную загрузку).
    await waitFor(() => {
      expect(screen.getByText("Чатов пока нет")).toBeInTheDocument();
    });
    expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
  });
});
