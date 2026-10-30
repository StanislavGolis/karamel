/**
 * Действия на карточке в ДВУХ режимах — открытый набор и заявка
 * (стори 11.6; AC-1, AC-2, AC-3, AC-4; П-2 «ноль шагов»).
 *
 * **Почему файл переписан, а не дополнен.** До 11.6 здесь стоял тест
 * «„Присоединиться“ открывает панель, а не шлёт POST сразу» — исполняемое
 * утверждение нарушения П-2 (открытый набор требовал двух шагов при
 * обратимом действии). `AC-1` стори 11.6 это утверждение ОТМЕНЯЕТ: инверсия
 * ниже явная и объяснённая ссылкой на AC, а не подгонка теста под код
 * (P-25: гард проверяет эффект, а не форму; §18.2: тест обязан краснеть).
 *
 * **Что обязано краснеть.**
 * - M1: вернуть `<JoinRequestSheet>` в ветку открытого набора → краснеет
 *   «одно касание: joinCard вызван, диалога нет» (`queryByRole("dialog")`).
 * - M2: убрать `disabled` при `offline` → краснеет `offline`-тест.
 * - M3: отрисовать форму инлайн вместо панели при `capacity=1` → краснеет
 *   «поля нет до открытия панели».
 * - M4: убрать `pending`-гард → краснеет тест двойного клика (второй `POST`).
 *
 * **Сеть мокается, а не поднимается.** `joinCard`/`leaveCard`/
 * `submitJoinRequest` — единственные точки выхода; мок проверяет ВЕТКУ, не
 * поднимая BFF. `Sheet` рендерит в портал: работаем через `screen.*`,
 * `container.*` не используем (§Project Structure Notes).
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JoinActions, JoinRequestSheet } from "@/components/cards/join-actions";

const joinCard = vi.fn();
const leaveCard = vi.fn();
const submitJoinRequest = vi.fn();
const cancelJoinRequest = vi.fn();
const refresh = vi.fn();

vi.mock("@/lib/queries/cards", () => ({
  joinCard: (...args: unknown[]) => joinCard(...args),
  leaveCard: (...args: unknown[]) => leaveCard(...args),
  submitJoinRequest: (...args: unknown[]) => submitJoinRequest(...args),
  cancelJoinRequest: (...args: unknown[]) => cancelJoinRequest(...args),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

const UUID = "11111111-1111-1111-1111-111111111111";

beforeEach(() => {
  joinCard.mockReset();
  leaveCard.mockReset();
  submitJoinRequest.mockReset();
  cancelJoinRequest.mockReset();
  refresh.mockReset();
});

describe("AC-1: открытый набор — одно касание", () => {
  it("один клик зовёт joinCard и НЕ открывает панель (инверсия П-2)", async () => {
    // ИНВЕРСИЯ КОНТРАКТА, осознанная. До стори 11.6 этот файл утверждал
    // обратное: `expect(joinCard).not.toHaveBeenCalled()` + появление
    // «Присоединиться к встрече?». То был исполняемый дефект П-2: шаг
    // подтверждения стоял на ОБРАТИМОМ действии (участие отменяется кнопкой
    // «Отменить участие» на той же карточке, FR104). AC-1 стори 11.6 требует
    // одно касание, поэтому ассерты перевёрнуты явно — с ссылкой на AC-1,
    // а не под код.
    joinCard.mockResolvedValue({ ok: true, data: null });

    render(<JoinActions uuid={UUID} capacity={3} participates={false} joinable />);

    await userEvent.click(screen.getByRole("button", { name: "Присоединиться" }));

    // AC-1.1: запрос ушёл сразу, промежуточного шага нет.
    await waitFor(() => expect(joinCard).toHaveBeenCalledWith(UUID));
    expect(joinCard).toHaveBeenCalledTimes(1);
    // AC-1.1: панели нет ни в одном состоянии.
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("Присоединиться к встрече?")).toBeNull();
    // AC-1.3: успех — перечитывание карточки, а не локальный статус.
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("AC-1.2: пока запрос в полёте, второго POST нет", async () => {
    // M4: снятый `pending`-гард делает этот тест красным — второй клик
    // отправил бы второй `POST`, а `join` на сервере НЕ идемпотентен
    // (`ALREADY_JOINED` на действии, которое только что прошло).
    let resolveJoin: ((value: unknown) => void) | undefined;
    joinCard.mockImplementation(
      () => new Promise((resolve) => { resolveJoin = resolve; }),
    );

    render(<JoinActions uuid={UUID} capacity={3} participates={false} joinable />);
    const button = screen.getByRole("button", { name: "Присоединиться" });

    await userEvent.click(button);
    await userEvent.click(button);

    expect(joinCard).toHaveBeenCalledTimes(1);
    // AC-1.2: кнопка объявляет занятость через `aria-busy` примитива.
    expect(button).toHaveAttribute("aria-busy", "true");

    resolveJoin?.({ ok: true, data: null });
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("AC-1.4: отказ — инлайн под кнопкой, кнопка на месте", async () => {
    joinCard.mockResolvedValue({ ok: false, code: "CARD_IS_FULL" });

    render(<JoinActions uuid={UUID} capacity={3} participates={false} joinable />);
    await userEvent.click(screen.getByRole("button", { name: "Присоединиться" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Свободных мест больше нет");
    // Кнопка не исчезла: человек может попробовать снова, когда место будет.
    expect(screen.getByRole("button", { name: "Присоединиться" })).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("AC-1.5: offline — кнопка недоступна с причиной, запрос не уходит", async () => {
    // M2: снятый `disabled` при `offline` делает тест красным.
    render(<JoinActions uuid={UUID} capacity={3} participates={false} joinable offline />);

    const button = screen.getByRole("button", { name: /Присоединиться/ });
    expect(button).toBeDisabled();
    const describedBy = button.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent("Нет связи");

    await userEvent.click(button);
    expect(joinCard).not.toHaveBeenCalled();
  });

  it("AC-2.1: «Отменить участие» шлёт leaveCard и перечитывает карточку", async () => {
    leaveCard.mockResolvedValue({ ok: true, data: null });

    render(<JoinActions uuid={UUID} capacity={3} participates joinable />);
    await userEvent.click(screen.getByRole("button", { name: "Отменить участие" }));

    await waitFor(() => expect(leaveCard).toHaveBeenCalledWith(UUID));
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("AC-2.1: участник видит отмену и при joinable=false (full/closed)", () => {
    render(<JoinActions uuid={UUID} capacity={3} participates joinable={false} />);

    // Выход из компании не зависит от приёма новых — иначе человек заперт.
    expect(screen.getByRole("button", { name: "Отменить участие" })).toBeInTheDocument();
  });

  it("ни одного действия, когда карточка не принимает участие", () => {
    render(<JoinActions uuid={UUID} capacity={3} participates={false} joinable={false} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("AC-3: заявка — форма внутри панели (capacity=1)", () => {
  it("AC-3.1/3.2: кнопка «Подать заявку», поля на карточке нет, панель открывается кликом", async () => {
    // M3: инлайн-форма вместо панели делает этот тест красным —
    // `queryByRole("textbox")` до клика был бы непустым.
    render(<JoinActions uuid={UUID} capacity={1} participates={false} joinable />);

    // AC-3.2: подпись совпадает с лентой (`actionLabel`, `event-card.tsx:140`).
    const button = screen.getByRole("button", { name: "Подать заявку" });
    expect(screen.queryByRole("button", { name: "Присоединиться" })).toBeNull();
    // AC-3.1: до открытия панели поля нет.
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();

    await userEvent.click(button);

    // AC-3.1: панель открылась и внутри — форма заявки.
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Заявка на встречу")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(screen.getByText(/из 300/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeInTheDocument();
  });

  it("AC-4.2: один клик открывает панель, второй (в поле и кнопку) отправляет", async () => {
    submitJoinRequest.mockResolvedValue({ ok: true, data: null });

    render(<JoinActions uuid={UUID} capacity={1} participates={false} joinable />);
    await userEvent.click(screen.getByRole("button", { name: "Подать заявку" }));

    await userEvent.type(screen.getByRole("textbox"), "Готов на субботу");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() =>
      expect(submitJoinRequest).toHaveBeenCalledWith(UUID, "Готов на субботу"),
    );
  });

  it("AC-3.3: после отправки панель закрывается и карточка перечитывается", async () => {
    submitJoinRequest.mockResolvedValue({ ok: true, data: null });

    render(<JoinActions uuid={UUID} capacity={1} participates={false} joinable />);
    await userEvent.click(screen.getByRole("button", { name: "Подать заявку" }));
    await userEvent.type(screen.getByRole("textbox"), "Пойду");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    // §7.5: «после отправки sheet закрывается».
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("AC-3.5: offline — кнопка недоступна, панель не открывается", async () => {
    render(<JoinActions uuid={UUID} capacity={1} participates={false} joinable offline />);

    const button = screen.getByRole("button", { name: /Подать заявку/ });
    expect(button).toBeDisabled();

    await userEvent.click(button);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("AC-3.6: joinable=false, заявки и участия нет — ни кнопки, ни формы", () => {
    render(<JoinActions uuid={UUID} capacity={1} participates={false} joinable={false} />);

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("JoinRequestSheet: панель заявки как обёртка формы", () => {
  it("рендерит форму, а не подтверждение присоединения", () => {
    render(<JoinRequestSheet uuid={UUID} open onOpenChange={() => {}} />);

    expect(screen.getByText("Заявка на встречу")).toBeInTheDocument();
    // Заголовок подтверждения присоединения ОТСУТСТВУЕТ: этот компонент
    // обслуживает только режим заявки (AC-5.1).
    expect(screen.queryByText("Присоединиться к встрече?")).toBeNull();
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("offline прокинут в форму: причина видна, кнопка недоступна", () => {
    render(<JoinRequestSheet uuid={UUID} open offline onOpenChange={() => {}} />);

    expect(screen.getByText(/Нет связи/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
  });
});
