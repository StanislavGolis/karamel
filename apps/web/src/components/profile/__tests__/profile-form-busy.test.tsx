/**
 * `aria-busy` на кнопке сохранения `ProfileForm` §8.3 (долг L-DS4-3, волна 4).
 *
 * Волна 4 убрала видимый текст «Сохраняем…» (R-4 отчёта) и заменила его на
 * `aria-busy`. Изменение видимого поведения не было закреплено тестом:
 * вернуть смену текста можно, и прогон останется зелёным — при том что
 * §6.4 п.3 запрещает менять текст кнопки (она прыгает по ширине, CLS).
 *
 * Проверяется ПАРА: `aria-busy` появляется, а видимый текст не меняется.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProfileForm } from "@/components/profile/profile-form";

/**
 * `useRouter` требует смонтированного App Router (Next), которого в jsdom
 * нет: без мока компонент падал бы `invariant expected app router to be
 * mounted` ДО проверки поведения. `vi.hoisted` обязателен: `vi.mock`
 * поднимается к началу модуля, и обычная переменная была бы ещё не
 * инициализирована.
 */
const { replaceMock, refreshMock } = vi.hoisted(() => ({
  replaceMock: vi.fn(),
  refreshMock: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock, refresh: refreshMock }),
}));

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ProfileForm: §8.3 «формы»", () => {
  it("во время сохранения кнопка получает aria-busy, но текст не меняется", async () => {
    // Промис не разрешается: проверяется именно СОСТОЯНИЕ отправки.
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>(() => {})));

    render(<ProfileForm initial={null} />);

    // AC-01/AC-07: все четыре поля обязательны — иначе кнопка недоступна
    // и состояние отправки не наступит вовсе.
    await userEvent.type(screen.getByLabelText(/^Имя/), "Анна");
    await userEvent.type(screen.getByLabelText(/^Город/), "Томск");
    // Стори 12.2, AC-2: `type="date"` — ввод через `change`.
    fireEvent.change(screen.getByLabelText(/^Дата рождения/), {
      target: { value: "1994-03-14" },
    });
    await userEvent.type(screen.getByLabelText(/^О себе/), "Люблю кофе");

    const button = screen.getByRole("button", { name: "Сохранить анкету" });
    expect(button).not.toHaveAttribute("aria-busy");

    await userEvent.click(button);

    // Состояние объявлено доступностью…
    expect(await screen.findByRole("button", { name: "Сохранить анкету" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
    // …и видимый текст НЕ сменился на «Сохраняем…» (§6.4 п.3).
    expect(screen.queryByText(/Сохраняем/)).toBeNull();
    expect(screen.getByRole("button", { name: "Сохранить анкету" })).toBeInTheDocument();
  });
});
