/**
 * Ошибка поля «Где» видна в DOM (стори 13.1; H-3 CR 2026-10-31).
 *
 * **Почему этот тест появился только после CR.** Замена `<Field label="Где"
 * error={errors.place_uuid}>` на `<div>` с жёстким `error={undefined}`
 * прошла все прежние тесты: схема `formSchema` требовала место, `submit`
 * писал сообщение в `errors.place_uuid`, но до DOM оно не доходило. Форма
 * молча не отправлялась — регрессия 4.2/12.x.
 *
 * **Почему проверяется DOM, а не стейт.** Тест «в стейте есть ошибка»
 * зеленел бы и при `error={undefined}`: стейт заполняется верно, теряется
 * только отрисовка. Человек видит DOM, значит проверять надо его.
 *
 * **Пара:** мутация `error={errors.place_uuid}` → `error={undefined}`
 * обязана покраснеть (проверено вручную, см. отчёт DS).
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CardCreateForm } from "@/components/cards/card-create-form";

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

describe("CardCreateForm: ошибка места (H-3 CR)", () => {
  it("отправка без места показывает текст ошибки в DOM", async () => {
    // Сеть не нужна: валидация отбивает форму ДО запроса.
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<CardCreateForm places={PLACES} />);

    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));

    // Краснеет при `error={undefined}`: сообщение уходит в стейт и не
    // отрисовывается, пользователь видит «ничего не произошло».
    expect(await screen.findByText("Выберите место из справочника")).toBeInTheDocument();

    // И оно привязано к полю «Где», а не показано где попало.
    const message = screen.getByText("Выберите место из справочника");
    expect(message.getAttribute("role")).toBe("alert");

    // Запрос КАРТОЧКИ не ушёл: форма не заполнена. `PlacePicker` при этом
    // законно грузит центр карты (`/address/city`) — это не отправка.
    const cardCalls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).includes("/api/proxy/cards"),
    );
    expect(cardCalls).toEqual([]);
  });

  it("после выбора места ошибка исчезает", async () => {
    // Пара к предыдущему: сообщение не «прилипло» навсегда.
    vi.stubGlobal("fetch", vi.fn());

    render(<CardCreateForm places={PLACES} />);

    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));
    expect(await screen.findByText("Выберите место из справочника")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Где"), PLACES[0].uuid);

    expect(screen.queryByText("Выберите место из справочника")).not.toBeInTheDocument();
  });
});
