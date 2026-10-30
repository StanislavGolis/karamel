/**
 * Показ рамок аудитории в карточке ленты (стори 12.6, AC-2, Task 7.3).
 *
 * **Проверяется то, ради чего AC написан:** рамки видны заявителю ДО подачи.
 * До Task 7 замер CR давал 0 совпадений по `gender_filter`/`age_min` в
 * `apps/web/src` — то есть фича была недостижима через интерфейс, и AC-2
 * оставался невыполненным при формально закрытой задаче.
 *
 * Ключевое различие, которое обязано держаться парой (P-21):
 * - рамки заданы → подпись есть;
 * - рамок нет → подписи НЕТ вовсе («ищу любого» не пишем, UX §1048).
 * Один зелёный тест не отличил бы «показывает рамки» от «показывает пустоту».
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EventCard, type FeedCard } from "@/components/cards/event-card";

function makeCard(overrides: Partial<FeedCard> = {}): FeedCard {
  return {
    uuid: "11111111-1111-1111-1111-111111111111",
    title: "Кино в субботу",
    description: null,
    category: "cinema",
    kind: "date",
    activity: "cinema",
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
    place: {
      uuid: "22222222-2222-2222-2222-222222222222",
      name: "Кино Прага",
      address_text: "Прага",
    },
    author: { uuid: "33333333-3333-3333-3333-333333333333", name: "Анна", age: 28 },
    chat_uuid: null,
    viewer_participates: false,
    viewer_requested: false,
    viewer_request_uuid: null,
    ...overrides,
  };
}

describe("EventCard — рамки аудитории (AC-2, Task 7.3)", () => {
  it("рамки заданы — подпись видна до подачи заявки", () => {
    render(<EventCard card={makeCard({ gender_filter: "female", age_min: 25, age_max: 35 })} />);

    expect(screen.getByText("Ищет женщин, 25–35 лет")).toBeInTheDocument();
  });

  it("рамок нет — подписи НЕТ вовсе (UX §1048: «только если задано»)", () => {
    render(<EventCard card={makeCard()} />);

    // Пара к предыдущему: без неё «подпись есть» не отличить от
    // «в карточке всегда что-то написано про рамки».
    expect(screen.queryByText(/^Ищет/)).toBeNull();
  });

  it("полуоткрытая пара — «Ищет возраст: от 30» (AC-1a)", () => {
    render(<EventCard card={makeCard({ age_min: 30 })} />);

    expect(screen.getByText("Ищет возраст: от 30")).toBeInTheDocument();
  });

  it("только пол — «Ищет мужчин»", () => {
    render(<EventCard card={makeCard({ gender_filter: "male" })} />);

    expect(screen.getByText("Ищет мужчин")).toBeInTheDocument();
  });
});
