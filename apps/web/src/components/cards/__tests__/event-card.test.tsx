/**
 * Тесты состояний `EventCard` (§8.3, §8.4, §2.3) — волна 3 миграции.
 *
 * **Проверяется семантика, не пиксели.** Проектный урок эпика 9 — «заявление
 * без доказательства»: тест на наличие класса `bg-success-tint` доказывает, что
 * класс написан, а не что человек увидел смысл. Поэтому утверждения — про
 * ТЕКСТ состояния и про доступность (NFR57: статус передаётся текстом).
 *
 * Что обязано покраснеть, если правило снять:
 * - убрать скелетон из `loading` → тест `loading` упадёт (найдёт разметку карточки);
 * - вернуть `danger` на `cancelled` → тест тона упадёт;
 * - убрать полосу `offline` → тест `offline` упадёт;
 * - убрать «Обновляем…» → тест `stale` упадёт.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EventCard, EventCardSkeleton, statusTone, type FeedCard } from "@/components/cards/event-card";

/** Минимальная карточка: остальные поля не влияют на состояния §8.3. */
function makeCard(overrides: Partial<FeedCard> = {}): FeedCard {
  return {
    uuid: "11111111-1111-1111-1111-111111111111",
    title: "Кино в субботу",
    description: null,
    category: "cinema",
    // Стори 12.4 (AC-3): `kind` и `activity` обязательны в `FeedCard`.
    // Без них фикстура не типизируется, и `tsc --noEmit` краснеет (Task 9.3).
    kind: "date",
    activity: "cinema",
    // Стори 12.6 (AC-2, Task 7.3): рамки в `FeedCard` обязательны в типе.
    // `null` — «не задано»: подпись рамок в этом случае НЕ рисуется.
    gender_filter: null,
    age_min: null,
    age_max: null,
    starts_at: "2026-11-01T19:00:00+03:00",
    expires_at: "2026-10-31T13:00:00+03:00",
    server_time: "2026-10-25T20:00:00+03:00",
    capacity: 4,
    filled_count: 2,
    status: "published",
    verification_status: "verified",
    place: { uuid: "22222222-2222-2222-2222-222222222222", name: "Кино Прага", address_text: "Прага" },
    author: { uuid: "33333333-3333-3333-3333-333333333333", name: "Анна", age: 28 },
    chat_uuid: null,
    viewer_participates: false,
    ...overrides,
  };
}

describe("EventCard: loading (§8.3)", () => {
  it("рисует скелетон 176px вместо карточки", () => {
    render(<EventCard loading />);

    const skeleton = document.querySelector('[data-slot="skeleton"]') as HTMLElement;
    expect(skeleton).not.toBeNull();
    // §8.4 п.1: скелетон повторяет габариты — 176px, иначе CLS.
    expect(skeleton.className).toContain("h-44");
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("без карточки тоже скелетон: полукарточка из undefined хуже заглушки", () => {
    render(<EventCard />);
    expect(document.querySelector('[data-slot="skeleton"]')).not.toBeNull();
  });

  it("экспортированный EventCardSkeleton имеет тот же размер", () => {
    render(<EventCardSkeleton />);
    const skeleton = document.querySelector('[data-slot="skeleton"]') as HTMLElement;
    expect(skeleton.className).toContain("h-44");
  });
});

describe("EventCard: offline и stale (§8.3, §8.4 п.4-5)", () => {
  it("offline: полоса warning сверху, чтение не блокируется", () => {
    render(<EventCard card={makeCard()} offline />);

    expect(screen.getByText(/Нет связи\. Показаны последние известные данные/)).toBeInTheDocument();
    // §8.4 п.5: карточка читается — ссылка на месте.
    expect(screen.getAllByRole("link").length).toBeGreaterThan(0);
  });

  it("stale: «Обновляем…» и действие остаётся активным", () => {
    render(<EventCard card={makeCard()} stale />);

    expect(screen.getByText("Обновляем…")).toBeInTheDocument();
    // §8.3: кнопка активна — «Присоединиться» не исчезает на время обновления.
    expect(screen.getByText("Присоединиться")).toBeInTheDocument();
  });

  it("без флагов полос и «Обновляем…» нет", () => {
    render(<EventCard card={makeCard()} />);

    expect(screen.queryByText(/Нет связи/)).toBeNull();
    expect(screen.queryByText("Обновляем…")).toBeNull();
  });
});

describe("EventCard: семантика статусов (§2.3)", () => {
  it("published со местами: success-тон и текст «Свободно 2 места»", () => {
    render(<EventCard card={makeCard({ capacity: 4, filled_count: 2 })} />);

    // NFR57: смысл — текстом, не цветом.
    expect(screen.getByText("Свободно 2 места")).toBeInTheDocument();
    expect(statusTone("published")).toBe("success");
  });

  it("full: «Компания собрана» и нейтральный тон", () => {
    render(<EventCard card={makeCard({ status: "full", filled_count: 4 })} />);

    expect(screen.getAllByText("Компания собрана").length).toBeGreaterThan(0);
    expect(statusTone("full")).toBe("neutral");
  });

  it("cancelled: «Встреча отменена автором» и НЕ danger", () => {
    render(<EventCard card={makeCard({ status: "cancelled" })} />);

    expect(screen.getByText("Встреча отменена автором")).toBeInTheDocument();
    // Жёсткое правило §2.3: `danger` ни к одному штатному состоянию карточки.
    expect(statusTone("cancelled")).toBe("muted");
  });

  it("ни один штатный статус не даёт danger", () => {
    const statuses: FeedCard["status"][] = ["draft", "published", "full", "closed", "finished", "cancelled"];
    for (const status of statuses) {
      expect(statusTone(status)).not.toBe("danger");
    }
  });

  it("у full и cancelled действия нет (UX §814)", () => {
    const { rerender } = render(<EventCard card={makeCard({ status: "full", filled_count: 4 })} />);
    expect(screen.queryByText("Присоединиться")).toBeNull();

    rerender(<EventCard card={makeCard({ status: "cancelled" })} />);
    expect(screen.queryByText("Присоединиться")).toBeNull();
  });

  it("aria-label несёт время, место и остаток мест", () => {
    render(<EventCard card={makeCard()} />);

    const link = screen.getByRole("link", { name: /Кино в субботу/ });
    expect(link.getAttribute("aria-label")).toContain("Кино Прага");
    expect(link.getAttribute("aria-label")).toContain("свободно 2");
  });

  it("нет hex и dark:-классов в разметке (§17.4)", () => {
    render(<EventCard card={makeCard()} stale offline />);

    const html = document.body.innerHTML;
    expect(html).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(html).not.toMatch(/\bdark:/);
  });
});
