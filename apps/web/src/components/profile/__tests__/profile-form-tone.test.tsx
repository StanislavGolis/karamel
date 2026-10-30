/**
 * Тон ошибки в `ProfileForm` по источнику (стори 11.9; AC-4).
 *
 * **Почему здесь только `danger`, а AC-3 проверяется в другой форме.**
 * Клиентская ветка возраста в `submit()`
 * (`!Number.isFinite(parsedAge) → setErrorTone("warning")`) **недостижима
 * через интерфейс**. Замер 2026-10-27: поле объявлено `type="number"`
 * (`profile-form.tsx:332`), и браузер не принимает ни буквы, ни экспоненту
 * (`fireEvent.change` с `"1e999"`, `"не число"`, `"Infinity"` → `input.value
 * === ""`). А `canSubmit` требует непустой возраст, поэтому кнопка остаётся
 * недоступной и `submit()` не вызывается. Ветка осталась от CR 2.3 как защита
 * от `Number("")`; теперь её держит сам атрибут `type`.
 *
 * Тон клиентской валидации (`warning`) доказан там, где ветка живая:
 * `cards/__tests__/card-create-form-tone.test.tsx` (zod-схема, ошибка под полем).
 * Мёртвая ветка записана долгом `W-11.9-a`, а не «покрыта» формальным тестом.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProfileForm } from "@/components/profile/profile-form";

/** `useRouter` требует смонтированного App Router, которого в jsdom нет. */
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

describe("ProfileForm: тон ошибки по источнику", () => {
  it("AC-4: отказ сервера остаётся `danger`, не превращается в `warning`", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({ code: "bio_too_long" }),
      } as unknown as Response),
    );
    render(<ProfileForm initial={null} />);

    // Все четыре поля обязательны (`canSubmit`), иначе кнопка недоступна.
    await userEvent.type(screen.getByLabelText(/^Имя/), "Анна");
    await userEvent.type(screen.getByLabelText(/^Город/), "Томск");
    // Стори 12.2, AC-2: поле стало `type="date"` — ввод через `change`,
    // потому что `userEvent.type` в jsdom не набирает дату посимвольно.
    fireEvent.change(screen.getByLabelText(/^Дата рождения/), {
      target: { value: "1994-03-14" },
    });
    await userEvent.type(screen.getByLabelText(/^О себе/), "Люблю настольные игры");
    await userEvent.click(screen.getByRole("button", { name: /Сохранить анкету/ }));

    // Текст берётся из `ERRORS[bio_too_long]`: источник — ответ API, действие
    // не выполнено. Краснеет, если серверный отказ «унифицируют» до `warning`:
    // тогда «поле слишком длинное» станет неотличимо от «сервер не принял»,
    // и человек будет править валидное поле.
    const alert = await screen.findByRole("alert");
    expect(alert.className).toContain("text-danger");
    expect(alert.className).not.toContain("text-warning");
  });
});
