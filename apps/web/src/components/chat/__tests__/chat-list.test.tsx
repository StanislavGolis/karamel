/**
 * UI-тесты `ChatList` (стори 6.6; AC-01, AC-02, AC-04).
 *
 * Проверяются ПОВЕДЕНИЯ, обещанные в стори, а не разметка:
 *
 * - список рендерит карточку, превью последнего сообщения и время (AC-01);
 * - загрузка — СКЕЛЕТОНЫ с `aria-busy`, а не спиннер (UX-правило проекта);
 * - счётчик непрочитанных — ТЕКСТОМ (NFR57: цвет не единственный носитель);
 * - пустой список даёт `EmptyState`, а не пустой `<div>`;
 * - `404`/ошибка дают понятный текст, а не «вечную загрузку» (ADR-13).
 *
 * `fetch` подменяется вручную: MSW-инфраструктуры в проекте нет, а маршрутов
 * здесь два (`GET /chats`, `POST /chats/{uuid}/read`) — прямой мок читается
 * легче, чем обработчик сервера.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatList } from "@/components/chat/chat-list";

/**
 * `useRouter` требует смонтированного App Router (Next), которого в jsdom
 * нет: без мока компонент падал бы `invariant expected app router to be
 * mounted` ДО проверки поведения. `vi.hoisted` обязателен: `vi.mock`
 * поднимается к началу модуля, и обычная переменная была бы ещё не
 * инициализирована (ReferenceError вместо поведения мока).
 */
const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn(), refresh: vi.fn() }),
}));

const CHAT_UUID = "11111111-1111-4111-8111-111111111111";
const CARD_UUID = "22222222-2222-4222-8222-222222222222";
const SENDER_UUID = "33333333-3333-4333-8333-333333333333";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function listResponse(
  items: unknown[],
  extra: { total?: number; cursor?: string | null } = {},
): Response {
  return jsonResponse({
    items,
    total: extra.total ?? items.length,
    cursor: extra.cursor ?? null,
  });
}

function chatItem(overrides: Record<string, unknown> = {}) {
  return {
    chat_uuid: CHAT_UUID,
    card_uuid: CARD_UUID,
    card_title: "Кофе в центре",
    last_message: {
      uuid: "44444444-4444-4444-8444-444444444444",
      sender_uuid: SENDER_UUID,
      body: "Буду в 19:00",
      created_at: "2026-10-24T10:00:00Z",
    },
    unread_count: 2,
    last_activity_at: "2026-10-24T10:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ChatList", () => {
  it("AC-01: показывает карточку, превью последнего сообщения и счётчик", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(listResponse([chatItem()])));

    render(<ChatList />);

    await waitFor(() => {
      expect(screen.getByText("Кофе в центре")).toBeInTheDocument();
    });
    expect(screen.getByText(/Буду в 19:00/)).toBeInTheDocument();
    // NFR57: счётчик — ТЕКСТОМ, а не только цветом/точкой.
    expect(screen.getByText("2 непрочитанных")).toBeInTheDocument();
    // Ссылка ведёт в чат по публичному `uuid` (D-2: без внутренних id).
    expect(screen.getByRole("link", { name: /Кофе в центре/ })).toHaveAttribute(
      "href",
      `/app/chats/${CHAT_UUID}`,
    );
  });

  it("UX: при загрузке — скелетоны с aria-busy, спиннеров нет", () => {
    // Промис не разрешается: проверяется ИМЕННО состояние загрузки.
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));

    const { container } = render(<ChatList />);

    const busy = container.querySelector('[aria-busy="true"]');
    expect(busy).toBeInTheDocument();
    // Спиннер проекта запрещён: он сдвигает контент и читается как «зависло».
    expect(container.querySelector(".animate-spin")).toBeNull();
  });

  it("AC-04: чат без сообщений показывается с подписью, а не пропадает", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          listResponse([chatItem({ last_message: null, unread_count: 0 })]),
        ),
    );

    render(<ChatList />);

    await waitFor(() => {
      expect(screen.getByText("Кофе в центре")).toBeInTheDocument();
    });
    expect(screen.getByText("Сообщений пока нет")).toBeInTheDocument();
    // Счётчик при нуле не рисуется: «0 непрочитанных» — шум, а не факт.
    expect(screen.queryByText(/непрочитанных/)).not.toBeInTheDocument();
  });

  it("AC-04: пустой список даёт EmptyState с объяснением", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(listResponse([])));

    render(<ChatList />);

    await waitFor(() => {
      expect(screen.getByText("Чатов пока нет")).toBeInTheDocument();
    });
    expect(
      screen.getByText(/Чат открывается сам, когда компания собрана/),
    ).toBeInTheDocument();
  });

  it("ADR-13: при 404 показывается «Чат недоступен» без деталей", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ code: "chat_not_found" }, 404)),
    );

    render(<ChatList />);

    await waitFor(() => {
      expect(screen.getByText(/Чат недоступен/)).toBeInTheDocument();
    });
    // Ответ не должен раскрывать, существует ли чат (ADR-13).
    expect(screen.queryByText(/не найден в базе/)).not.toBeInTheDocument();
  });

  it("AC-01: ошибка сети даёт понятный текст, а не пустой экран", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network")));

    render(<ChatList />);

    await waitFor(() => {
      expect(screen.getByText(/Не удалось загрузить чаты/)).toBeInTheDocument();
    });
  });
});
