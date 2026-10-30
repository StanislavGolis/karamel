/**
 * Заявитель НЕ видит признак «вне рамок» (стори 12.6, AC-4, FR126, П12).
 *
 * **Требование жёсткое и односторонее.** Сервер отдаёт `within_bounds`
 * только в `GET /cards/{uuid}/requests` — то есть автору (гард-тест бэкенда
 * `test_within_bounds_not_in_my_requests.py`). Заявителю причина отказа не
 * сообщается вовсе: отказ безличен (`FR81`), и «вас отсеяли по возрасту» —
 * это ровно та персонализация, которую П12 запрещает.
 *
 * **Почему тест на отсутствие, а не на присутствие.** Утечка — это появление
 * текста там, где его быть не должно. Проверять «есть ли пометка у автора»
 * бессмысленно: она там обязана быть (Task 7.4). Проверять надо обратную
 * сторону — заявительские поверхности не содержат ни пометки, ни поля.
 *
 * Поверхности заявителя: карточка в ленте (`EventCard`) и панель подачи
 * заявки (`JoinRequestSheet` → `JoinRequestForm`). Обе рендерятся до того,
 * как человек подал заявку, — то есть в момент, когда он решает, стоит ли.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { JoinRequestSheet } from "@/components/cards/join-actions";
import { EventCard, type FeedCard } from "@/components/cards/event-card";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }),
}));

/**
 * Карточка с ЗАДАННЫМИ рамками: если утечка возможна, она возможна именно
 * здесь — на карточке без рамок пометка была бы ложью и не появилась бы
 * случайно. Проверка на пустой карточке прошла бы всегда.
 */
function makeCard(overrides: Partial<FeedCard> = {}): FeedCard {
  return {
    uuid: "11111111-1111-1111-1111-111111111111",
    title: "Кино в субботу",
    description: null,
    category: "cinema",
    kind: "date",
    activity: "cinema",
    gender_filter: "female",
    age_min: 25,
    age_max: 35,
    starts_at: "2026-11-01T19:00:00+03:00",
    expires_at: "2026-10-31T13:00:00+03:00",
    server_time: "2026-10-25T20:00:00+03:00",
    capacity: 1,
    filled_count: 0,
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

/**
 * Тексты, которыми утечка выражается. Регулярки, а не точные строки: перепиши
 * пометку «Вне ваших рамок» на «Вы вне рамок» — запрет должен остаться.
 * Тон констатации разрешён автору, но и там формулировки оценочные запрещены
 * (UX §1052: «не подходит», «не в вашем возрасте»).
 */
const LEAK_PATTERNS = [/вне\s+ваших\s+рамок/i, /вне\s+рамок/i, /не\s+подходит/i, /не\s+в\s+вашем\s+возрасте/i];

function expectNoLeak(container: HTMLElement) {
  const text = container.textContent ?? "";
  for (const pattern of LEAK_PATTERNS) {
    expect(text).not.toMatch(pattern);
  }
}

describe("заявитель не видит признак «вне рамок» (AC-4, FR126, П12)", () => {
  it("карточка в ленте: рамки показаны, пометки «вне рамок» НЕТ", () => {
    const { container } = render(<EventCard card={makeCard()} />);

    // Пара: рамки видны — значит человек знает, кого ждут (AC-2).
    expect(screen.getByText("Ищет женщин, 25–35 лет")).toBeInTheDocument();
    // И при этом не узнаёт вердикт про себя: его ещё не выносили.
    expectNoLeak(container);
  });

  it("панель подачи заявки: ни пометки, ни поля `within_bounds`", () => {
    const { container } = render(
      <JoinRequestSheet
        uuid="11111111-1111-1111-1111-111111111111"
        open
        onOpenChange={() => {}}
        onDone={() => {}}
      />,
    );

    expectNoLeak(container);
  });
});
