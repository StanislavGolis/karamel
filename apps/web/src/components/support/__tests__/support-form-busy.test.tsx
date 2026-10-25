/**
 * `aria-busy` на кнопке отправки `SupportForm` §8.3 (долг L-DS4-3, волна 4).
 *
 * Волна 4 убрала видимый текст «Отправляем…» (R-4 отчёта) и заменила его на
 * `aria-busy`. Видимое изменение продукта осталось БЕЗ теста: можно вернуть
 * смену текста (и получить скачущую по ширине кнопку, §6.4 п.3) — прогон
 * будет зелёным.
 *
 * Проверяется ПАРА: `aria-busy` появляется, а текст кнопки НЕ меняется.
 * Одно без другого не имеет смысла.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SupportForm } from "@/components/support/support-form";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SupportForm: §8.3 «формы»", () => {
  it("во время отправки кнопка получает aria-busy, но текст не меняется", async () => {
    // Промис не разрешается: проверяется именно СОСТОЯНИЕ отправки.
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>(() => {})));

    render(<SupportForm onCreated={vi.fn()} />);

    // Метка `support-body` содержит и подсказку («Опишите шаги…»), поэтому
    // ищем по доступному имени-префиксу, а не по точной строке.
    const textarea = screen.getByRole("textbox", { name: /Что случилось/ });
    await userEvent.type(textarea, "Не приходит письмо");

    const button = screen.getByRole("button", { name: "Отправить" });
    expect(button).not.toHaveAttribute("aria-busy");

    await userEvent.click(button);

    // Состояние объявлено доступностью…
    expect(await screen.findByRole("button", { name: "Отправить" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
    // …и видимый текст НЕ сменился на «Отправляем…» (§6.4 п.3: иначе кнопка
    // прыгает по ширине и layout shift).
    expect(screen.queryByText(/Отправляем/)).toBeNull();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeInTheDocument();
  });
});
