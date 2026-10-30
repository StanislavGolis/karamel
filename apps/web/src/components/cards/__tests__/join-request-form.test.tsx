/**
 * Форма заявки на карточку режима «заявки» (стори 11.5; AC-1, AC-3, AC-8).
 *
 * **Почему именно эти тесты.** Матрица §8.3 задаёт для формы состояния
 * `idle`/`sending`/`error`, а AC-3 добавляет «пустое сообщение → кнопка
 * недоступна с причиной». Каждый из них обязан краснеть от снятия кода:
 * снятый `disabled` при пустом вводе, потерянный `aria-describedby`, тон
 * `danger` вместо `warning`, потеря счётчика, отправка пустого тела.
 *
 * **Сеть мокается, а не поднимается.** `submitJoinRequest` — единственная
 * точка выхода; мок проверяет ВЕТКУ, не поднимая BFF (тот же приём, что в
 * `join-request-sheet.test.tsx`).
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JoinRequestForm } from "@/components/cards/join-request-form";

const submitJoinRequest = vi.fn();

vi.mock("@/lib/queries/cards", () => ({
  submitJoinRequest: (...args: unknown[]) => submitJoinRequest(...args),
}));

const UUID = "44444444-4444-4444-4444-444444444444";

beforeEach(() => {
  submitJoinRequest.mockReset();
});

describe("JoinRequestForm: пустое сообщение (AC-3, §6.4)", () => {
  it("кнопка недоступна, причина связана через aria-describedby", () => {
    render(<JoinRequestForm uuid={UUID} />);

    const button = screen.getByRole("button", { name: "Отправить" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-disabled", "true");

    const describedBy = button.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent(/Напишите хотя бы/);
  });

  it("причина в тоне warning, а не danger (P-4б)", () => {
    render(<JoinRequestForm uuid={UUID} />);

    const reason = screen.getByText(/Напишите хотя бы/);
    // Пустое поле — незавершённый ввод, а не сбой сервера: `danger` тут
    // обещал бы необратимость (решение владельца П-4б).
    expect(reason.className).toContain("warning");
    expect(reason.className).not.toContain("danger");
  });

  it("пробелы не считаются сообщением (min_length=1 после trim)", async () => {
    render(<JoinRequestForm uuid={UUID} />);

    await userEvent.type(screen.getByRole("textbox"), "   ");

    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
  });
});

describe("JoinRequestForm: счётчик остатка (AC-1, §13.3)", () => {
  it("считает от 300 и уменьшается на вводе", async () => {
    render(<JoinRequestForm uuid={UUID} />);

    expect(screen.getByText(/300/)).toBeInTheDocument();

    await userEvent.type(screen.getByRole("textbox"), "Привет, мир");

    // 300 − 11 = 289. Число проверяется по факту, а не «содержит 2»:
    // иначе тест прошёл бы и на неработающем счётчике.
    expect(screen.getByText("289 из 300")).toBeInTheDocument();
  });

  it("счётчик объявлен скринридеру через aria-live", () => {
    render(<JoinRequestForm uuid={UUID} />);

    const counter = screen.getByText(/из 300/);
    expect(counter).toHaveAttribute("aria-live", "polite");
  });
});

describe("JoinRequestForm: отправка (AC-2, AC-8)", () => {
  it("успех вызывает submitJoinRequest с сообщением и зовёт onDone", async () => {
    submitJoinRequest.mockResolvedValue({ ok: true, data: null });
    const onDone = vi.fn();

    render(<JoinRequestForm uuid={UUID} onDone={onDone} />);
    await userEvent.type(screen.getByRole("textbox"), "Возьму термос");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => expect(submitJoinRequest).toHaveBeenCalledWith(UUID, "Возьму термос"));
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });

  it("пробелы по краям обрезаются перед отправкой", async () => {
    submitJoinRequest.mockResolvedValue({ ok: true, data: null });

    render(<JoinRequestForm uuid={UUID} />);
    await userEvent.type(screen.getByRole("textbox"), "  Привет  ");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => expect(submitJoinRequest).toHaveBeenCalledWith(UUID, "Привет"));
  });

  it("ошибка сети: текст «Нет связи», форма открыта, ввод сохранён (AC-8)", async () => {
    submitJoinRequest.mockResolvedValue({ ok: false, code: "network_error" });

    render(<JoinRequestForm uuid={UUID} />);
    await userEvent.type(screen.getByRole("textbox"), "Привет");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Нет связи");
    // §8.4 п.3: ввод не теряется.
    expect(screen.getByRole("textbox")).toHaveValue("Привет");
  });

  it("JOIN_REQUEST_LIMIT_REACHED: кнопка НЕДОСТУПНА с причиной (AC-4, раскрытие 4.1)", async () => {
    // CR 11.5 HIGH-1/HIGH-3: код взят РЕАЛЬНЫЙ серверный
    // (`join_request_service.py:116` — `JOIN_REQUEST_LIMIT_REACHED`), а не
    // выдуманный `LIMIT_REACHED`. Прежний тест был зелёным на коде, которого
    // сервер не отдаёт, и проверял только текст, не состояние кнопки.
    submitJoinRequest.mockResolvedValue({ ok: false, code: "JOIN_REQUEST_LIMIT_REACHED" });

    render(<JoinRequestForm uuid={UUID} />);
    await userEvent.type(screen.getByRole("textbox"), "Привет");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    // Причина названа — иначе лимит выглядел бы необъяснимым сбоем.
    await screen.findByRole("alert");
    const button = screen.getByRole("button", { name: "Отправить" });
    // Раскрытие 4.1: кнопка НЕДОСТУПНА, а не «нажмите ещё раз».
    expect(button).toBeDisabled();
    const describedBy = button.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent(/2 активные заявки/);
  });

  it("JOIN_REQUEST_LIMIT_REACHED: причина в тоне warning, не danger", async () => {
    submitJoinRequest.mockResolvedValue({ ok: false, code: "JOIN_REQUEST_LIMIT_REACHED" });

    render(<JoinRequestForm uuid={UUID} />);
    await userEvent.type(screen.getByRole("textbox"), "Привет");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    // Причина и alert содержат один текст: ищем ПРИЧИНУ по её id, иначе
    // `findByText` вернул бы `role="alert"` с тоном `danger`.
    await screen.findByRole("alert");
    const reason = document.getElementById("join-request-reason");
    expect(reason).not.toBeNull();
    expect(reason!.textContent).toMatch(/2 активные заявки/);
    expect(reason!.className).toContain("warning");
    expect(reason!.className).not.toContain("danger");
  });

  it("rate_limited объясняет ожидание, а не «попробуйте ещё раз»", async () => {
    // CR 11.5 MED: технический лимитер подачи — без текста человек продлевает
    // блокировку повторными нажатиями.
    submitJoinRequest.mockResolvedValue({ ok: false, code: "rate_limited" });

    render(<JoinRequestForm uuid={UUID} />);
    await userEvent.type(screen.getByRole("textbox"), "Привет");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Подождите/);
  });
});
