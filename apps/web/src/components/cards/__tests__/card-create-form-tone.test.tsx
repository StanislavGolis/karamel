/**
 * Тон ошибки по ИСТОЧНИКУ (стори 11.9; AC-3, AC-4).
 *
 * **Почему проверяется класс, а не текст.** Тексты ошибок не менялись — менялся
 * тон. Тест на текст остался бы зелёным при полностью красной форме, то есть
 * не проверял бы ничего из того, что требует AC-3.
 *
 * **Два источника — два разных требования.**
 * - Ошибка ввода (zod на клиенте): человек исправляет сам → `warning`.
 * - Отказ API (ответ пришёл, действие не выполнено): → `danger`.
 * Один тест на каждую сторону обязателен: AC-4 без теста означает, что
 * «унификация» тонов в одну сторону пройдёт незамеченной.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CardCreateForm } from "@/components/cards/card-create-form";

/** Справочник мест: без него форма показывает «Нет доступных мест». */
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

describe("CardCreateForm: тон ошибки по источнику", () => {
  it("AC-3: пустое название (валидация клиента) — warning, не danger", async () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<CardCreateForm places={PLACES} />);

    // Отправка заведомо невалидной формы: название пустое. До сети дело не дойдёт.
    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));

    const message = await screen.findByText("Название не может быть пустым");
    // Краснеет, если tone снова станет `danger`: пользователь видит тревогу
    // на собственной опечатке, а `danger` зарезервирован под необратимое.
    expect(message.className).toContain("text-warning");
    expect(message.className).not.toContain("text-danger");
  });

  it("AC-4: отказ API — danger остаётся", async () => {
    // Валидные значения, чтобы дойти до сети: клиентская схема не сработает.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        status: 409,
        ok: false,
        json: async () => ({ code: "PLACE_NOT_AVAILABLE" }),
      } as unknown as Response),
    );
    render(<CardCreateForm places={PLACES} />);

    // Метки полей — «Что», «Когда», «Где» (§6.3, форма 4.2). Валидные значения
    // нужны, чтобы клиентская схема пропустила форму и запрос дошёл до сети.
    await userEvent.type(screen.getByLabelText("Что"), "Прогулка");
    await userEvent.type(screen.getByLabelText("Когда"), "2030-01-01T18:00");
    await userEvent.selectOptions(screen.getByLabelText("Где"), PLACES[0].uuid);
    // Стори 12.4 (AC-3): тип встречи и занятие обязательны. Без них форма
    // не доходит до сети (тест 11.9 писался до 12.4 и молча устарел).
    await userEvent.click(screen.getByRole("button", { name: "Компания" }));
    await userEvent.selectOptions(screen.getByLabelText("Чем займётесь"), "walk");

    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));

    const message = await screen.findByText("Это место недоступно. Выберите другое из справочника.");
    // Краснеет, если серверный отказ переведут в `warning`: «не удалось»
    // станет неотличимо от «вы не заполнили поле», и человек не поймёт,
    // что повторять отправку бессмысленно.
    expect(message.className).toContain("text-danger");
    expect(message.className).not.toContain("text-warning");
  });
});
