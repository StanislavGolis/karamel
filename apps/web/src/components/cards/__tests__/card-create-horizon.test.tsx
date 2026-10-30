/**
 * Горизонт 72 часа на клиенте (стори 12.5, AC-1, FR116).
 *
 * **Две стороны одного правила.** Сервер отвечает `422 STARTS_AT_TOO_FAR`;
 * клиент обязан (а) показать этот код человеку, (б) подсказать границу в
 * календаре. Без второго теста код ошибки уходил бы в общее «Не удалось
 * сохранить», и автор не понял бы, что именно не так.
 *
 * **Подсказка — не правило.** `max` у `datetime-local` можно стереть
 * атрибутом в devtools; тогда отказ придёт с сервера. Тест фиксирует, что
 * браузерная граница вычисляется от `now + 72 ч` и в локальном формате.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CARD_MAX_HORIZON_HOURS,
  CardCreateForm,
  horizonLimitLocal,
} from "@/components/cards/card-create-form";

const PLACES = [
  {
    uuid: "11111111-1111-1111-1111-111111111111",
    name: "Кофейня",
    address_text: "пр. Ленина, 1",
    category: "cafe",
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CARD_MAX_HORIZON_HOURS (AC-1)", () => {
  it("равен 72 — зеркало серверного `CARD_MAX_HORIZON_HOURS`", () => {
    // Краснеет, если горизонт на клиенте «поедет» отдельно от сервера.
    expect(CARD_MAX_HORIZON_HOURS).toBe(72);
  });
});

describe("horizonLimitLocal (AC-1)", () => {
  it("`now + 72 ч` в локальном формате datetime-local", () => {
    // 2026-10-29T12:00 локально → 2026-11-01T12:00 локально.
    expect(horizonLimitLocal(new Date(2026, 9, 29, 12, 0, 0))).toBe("2026-11-01T12:00");
  });

  it("секунды отброшены — шаг поля минуты", () => {
    // Краснеет, если формат получит секунды: `datetime-local` их не примет
    // и подсказка тихо исчезнет.
    expect(horizonLimitLocal(new Date(2026, 9, 29, 12, 0, 45))).toBe("2026-11-01T12:00");
  });

  it("однозначные месяц/день/час дополняются нулём", () => {
    expect(horizonLimitLocal(new Date(2026, 0, 5, 9, 7, 0))).toBe("2026-01-08T09:07");
  });
});

describe("CardCreateForm: код `STARTS_AT_TOO_FAR` (AC-1)", () => {
  it("422 с кодом горизонта показывает текст про трое суток", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        status: 422,
        ok: false,
        json: async () => ({ code: "STARTS_AT_TOO_FAR" }),
      } as unknown as Response),
    );
    render(<CardCreateForm places={PLACES} />);

    await userEvent.type(screen.getByLabelText("Что"), "Прогулка");
    // Дата внутри горизонта: клиентская подсказка не мешает отправке.
    await userEvent.type(screen.getByLabelText("Когда"), "2030-01-01T18:00");
    await userEvent.selectOptions(screen.getByLabelText("Где"), PLACES[0].uuid);
    // Стори 12.4: тип и занятие обязательны, иначе до сети дело не дойдёт.
    await userEvent.click(screen.getByRole("button", { name: "Компания" }));
    await userEvent.selectOptions(screen.getByLabelText("Чем займётесь"), "walk");

    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));

    const message = await screen.findByText("Встречу можно назначить не дальше трёх суток.");
    // Мутация «удалить ключ `STARTS_AT_TOO_FAR` из `ERRORS`» — тест краснеет:
    // вместо текста появится общее «Не удалось сохранить».
    expect(message).toBeInTheDocument();
    // Отказ API — тон `danger` (источник 2 по 11.9), не `warning`.
    expect(message.className).toContain("text-danger");
  });
});
