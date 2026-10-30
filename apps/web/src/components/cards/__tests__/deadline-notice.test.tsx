/**
 * Тесты `DeadlineNotice` (стори 12.5, AC-3, AC-4; UX §973-1000, З-9).
 *
 * **Проверяется отсутствие тиканья, а не наличие разметки.** Проектный урок
 * «заявление без доказательства»: тест на класс `text-warning` доказывает, что
 * класс написан, а не что таймера нет. Поэтому утверждения:
 *
 * - таймеров НОЛЬ (`vi.getTimerCount()`), а не «мы не вызывали setInterval»;
 * - `role="timer"` отсутствует в разметке — мутация «вернуть атрибут» краснит;
 * - в тексте нет остатка («осталось», «мин», «ч») — мутация «вернуть формат
 *   `formatRemaining`» краснит;
 * - порог «меньше часа» даёт ОДНУ плашку `role="status"`.
 */

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DeadlineNotice,
  formatDeadlineTime,
  formatShortDeadline,
} from "@/components/cards/deadline-notice";

// Якоря фиксированы: тест не должен зависеть от текущей даты.
const SERVER_NOW = "2026-10-29T12:00:00+03:00";
const DEADLINE_SOON = "2026-10-29T13:00:00+03:00"; // меньше часа
const DEADLINE_FAR = "2026-10-31T15:00:00+03:00"; // больше часа
const DEADLINE_PAST = "2026-10-29T11:00:00+03:00"; // уже прошло

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("DeadlineNotice: статичный срок (AC-3, AC-4, З-9)", () => {
  it("не создаёт ни одного таймера (U-9)", () => {
    render(<DeadlineNotice expiresAt={DEADLINE_FAR} serverTime={SERVER_NOW} />);

    // Мутация «вернуть setInterval(..., 1_000)» — тест краснеет.
    expect(vi.getTimerCount()).toBe(0);
  });

  it("в разметке нет role=\"timer\" (U-10)", () => {
    const { container } = render(
      <DeadlineNotice expiresAt={DEADLINE_SOON} serverTime={SERVER_NOW} />,
    );

    // Мутация «добавить role=\"timer\"» — тест краснеет.
    expect(container.querySelector('[role="timer"]')).toBeNull();
  });

  it("текст не содержит остатка времени (U-11)", () => {
    const { container } = render(
      <DeadlineNotice expiresAt={DEADLINE_FAR} serverTime={SERVER_NOW} />,
    );

    const text = container.textContent ?? "";
    // Мутация «заменить формат на formatRemaining» — тест краснеет.
    expect(text).not.toMatch(/осталось/i);
    expect(text).not.toMatch(/\d+\s*мин/i);
    expect(text).not.toMatch(/\d+\s*ч\b/i);
    expect(text).not.toMatch(/меньше минуты/i);
  });

  it("показывает срок «до сб, 13:00» обычным текстом (AC-3)", () => {
    render(<DeadlineNotice expiresAt={DEADLINE_SOON} serverTime={SERVER_NOW} />);

    // Срок читается без aria-hidden: статичная информация.
    expect(screen.getByText("до чт, 13:00")).toBeInTheDocument();
  });

  it("плашка за час — одна статичная role=\"status\" (UX §973-1000)", () => {
    render(<DeadlineNotice expiresAt={DEADLINE_SOON} serverTime={SERVER_NOW} />);

    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(1);
    expect(statuses[0]).toHaveTextContent("Окно закроется в 13:00");
  });

  it("дальше часа — плашки нет (порог не срабатывает раньше)", () => {
    render(<DeadlineNotice expiresAt={DEADLINE_FAR} serverTime={SERVER_NOW} />);

    // Пара к предыдущему тесту: без неё мутация «показывать всегда» неотличима
    // от «порог работает».
    expect(screen.queryAllByRole("status")).toHaveLength(0);
  });

  it("порог — именно час, а не «сутки» (CR 12.5, H-3)", () => {
    // Дедлайн через 5 часов: больше часа, но меньше суток.
    //
    // Зачем отдельный тест. Мутация `NOTICE_THRESHOLD_MS = 3_600_000 →
    // 86_400_000` (порог «сутки») прошла ЗЕЛЁНОЙ через 12 тестов: `DEADLINE_FAR`
    // отстоит на 51 час, и при пороге в сутки плашки тоже нет — тест выше
    // не отличает «час» от «сутки». Через 5 часов отличает: плашка обязана
    // молчать, а при пороге-сутках она бы появилась.
    const fiveHoursLater = "2026-10-29T17:00:00+03:00";
    render(<DeadlineNotice expiresAt={fiveHoursLater} serverTime={SERVER_NOW} />);

    expect(screen.queryAllByRole("status")).toHaveLength(0);
    // Срок при этом виден всегда — прячется только плашка «скоро».
    expect(screen.getByText("до чт, 17:00")).toBeInTheDocument();
  });

  it("после истечения — «Окно закрыто. Компания собрана.» (UX §973-1000)", () => {
    render(<DeadlineNotice expiresAt={DEADLINE_PAST} serverTime={SERVER_NOW} />);

    expect(screen.getByText("Окно закрыто. Компания собрана.")).toBeInTheDocument();
    expect(screen.queryAllByRole("status")).toHaveLength(0);
  });

  it("без server_time рендер не падает и таймеров не создаёт", () => {
    render(<DeadlineNotice expiresAt={DEADLINE_FAR} />);

    expect(vi.getTimerCount()).toBe(0);
  });

  it("битая дата ничего не рисует (не бросает)", () => {
    const { container } = render(<DeadlineNotice expiresAt="not-a-date" serverTime={SERVER_NOW} />);

    expect(container.textContent).toBe("");
  });
});

describe("formatShortDeadline / formatDeadlineTime", () => {
  it("«до чт, 13:00» — день и время без остатка", () => {
    expect(formatShortDeadline(DEADLINE_SOON)).toBe("до чт, 13:00");
  });

  it("«13:00» — только время", () => {
    expect(formatDeadlineTime(DEADLINE_SOON)).toBe("13:00");
  });

  it("битая дата — пустая строка, не `Invalid Date`", () => {
    expect(formatShortDeadline("nope")).toBe("");
    expect(formatDeadlineTime("nope")).toBe("");
  });
});
