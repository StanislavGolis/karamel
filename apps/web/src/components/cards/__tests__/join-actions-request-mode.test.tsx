/**
 * `JoinActions` в режиме заявок (стори 11.5; AC-1, AC-2, AC-4, AC-5, AC-6,
 * AC-7).
 *
 * **Почему отдельный файл.** `join-request-sheet.test.tsx` проверяет
 * открытый набор (`capacity = 2..6`, `FR83`): одно касание и мгновенный
 * `joinCard` (стори 11.6). Здесь — другой режим: `capacity = 1`, панель с
 * формой сообщения и «Моя заявка». Смешивать их в одном файле значило бы
 * получить тест, который проходит при сломанном ветвлении: обе ветки
 * зовут разные клиенты, и `joinCard` мог бы вызываться в режиме заявок.
 *
 * **Стори 11.6 (AC-4.3):** ассерты про `cancelJoinRequest`/`leaveCard` не
 * тронуты. Изменены только тесты, которые рендерили форму В КОРНЕ карточки:
 * с 11.6 форма живёт внутри панели (`§7.5`), поэтому её открывает клик по
 * «Подать заявку».
 *
 * **Что обязано краснеть.** `AC-7` — поле `message` НЕ рендерится при
 * `capacity > 1` (иначе открытый набор потребует текста). `AC-2` — после
 * подачи карточка показывает «Моя заявка». `AC-5` — отмена зовёт
 * `cancelJoinRequest` с `request_uuid`, а не `leaveCard` с `card_uuid`.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JoinActions } from "@/components/cards/join-actions";

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

const UUID = "55555555-5555-5555-5555-555555555555";
const REQUEST_UUID = "66666666-6666-6666-6666-666666666666";

beforeEach(() => {
  joinCard.mockReset();
  leaveCard.mockReset();
  submitJoinRequest.mockReset();
  cancelJoinRequest.mockReset();
  refresh.mockReset();
});

describe("JoinActions: ветвление по capacity (AC-1, AC-7)", () => {
  it("capacity=1 → кнопка «Подать заявку», форма открывается панелью", async () => {
    // Стори 11.6, AC-3.1: форма живёт В ПАНЕЛИ, а не в корне карточки (§7.5).
    // До 11.6 этот тест ждал `textbox` сразу после рендера — то было инлайн-
    // размещение, которое стори убрала.
    render(<JoinActions uuid={UUID} capacity={1} participates={false} joinable />);

    // Открытый набор сюда не попадает: подпись «Присоединиться» здесь ложна.
    expect(screen.queryByRole("button", { name: "Присоединиться" })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Подать заявку" }));

    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeInTheDocument();
  });

  it("capacity=4 → поля сообщения НЕТ, действие открытого набора (AC-7)", () => {
    render(<JoinActions uuid={UUID} capacity={4} participates={false} joinable />);

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByRole("button", { name: "Присоединиться" })).toBeInTheDocument();
  });

  it("capacity=1 и заявка подана → «Моя заявка» и «Отменить заявку» (AC-2)", () => {
    render(
      <JoinActions
        uuid={UUID}
        capacity={1}
        participates={false}
        joinable
        requested
        requestUuid={REQUEST_UUID}
      />,
    );

    expect(screen.getByText("Моя заявка")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отменить заявку" })).toBeInTheDocument();
    // Формы повторной подачи нет: вторая заявка на ту же карточку — `409`.
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("JoinActions: отмена заявки (AC-5)", () => {
  it("зовёт cancelJoinRequest с request_uuid, а не leaveCard с card_uuid", async () => {
    cancelJoinRequest.mockResolvedValue({ ok: true, data: null });

    render(
      <JoinActions
        uuid={UUID}
        capacity={1}
        participates={false}
        joinable
        requested
        requestUuid={REQUEST_UUID}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Отменить заявку" }));

    await waitFor(() => expect(cancelJoinRequest).toHaveBeenCalledWith(REQUEST_UUID));
    // Отмена ЗАЯВКИ и отмена УЧАСТИЯ — разные маршруты; путаница дала бы
    // `404 PARTICIPATION_NOT_FOUND` вместо отмены.
    expect(leaveCard).not.toHaveBeenCalled();
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("нетерминальный отказ: текст показан, карточка не перечитывается", async () => {
    // Нетерминальный код (`network_error`): заявка, возможно, ещё жива,
    // перечитывать нечего — иначе обрыв сети выглядел бы как «заявки нет».
    cancelJoinRequest.mockResolvedValue({ ok: false, code: "network_error" });

    render(
      <JoinActions
        uuid={UUID}
        capacity={1}
        participates={false}
        joinable
        requested
        requestUuid={REQUEST_UUID}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Отменить заявку" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).not.toBe("");
    expect(refresh).not.toHaveBeenCalled();
  });

  it("requested без request_uuid → отмены нет (нечего отменять), форма скрыта", () => {
    render(
      <JoinActions
        uuid={UUID}
        capacity={1}
        participates={false}
        joinable
        requested
        requestUuid={null}
      />,
    );

    expect(screen.getByText("Моя заявка")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отменить заявку" })).toBeNull();
  });
});

describe("JoinActions: терминальные коды отмены обновляют экран (CR 11.5 MED)", () => {
  it.each(["REQUEST_SELECTED", "REQUEST_NOT_ACTIVE", "request_not_found"])(
    "%s → текст отказа И перечитывание карточки",
    async (code) => {
      // Заявки уже нет: состояние на экране устарело, и без `refresh`
      // человек жал бы мёртвую кнопку, получая тот же отказ.
      cancelJoinRequest.mockResolvedValue({ ok: false, code });

      render(
        <JoinActions
          uuid={UUID}
          capacity={1}
          participates={false}
          joinable
          requested
          requestUuid={REQUEST_UUID}
        />,
      );
      await userEvent.click(screen.getByRole("button", { name: "Отменить заявку" }));

      await screen.findByRole("alert");
      await waitFor(() => expect(refresh).toHaveBeenCalled());
    },
  );
});

describe("JoinActions: участник режима заявок (регрессия FR104, CR 11.5 HIGH-2)", () => {
  it("capacity=1, заявку выбрали → «Отменить участие» доступна, а не пустота", async () => {
    // Сценарий пилота: `selected` → `viewer_participates=true`,
    // `viewer_requested=false`, карточка `full` → `joinable=false`. До фикса
    // выполнение уходило в `if (!joinable) return null`, и участник компании
    // из одного человека не мог из неё выйти, хотя сервер это допускает.
    leaveCard.mockResolvedValue({ ok: true, data: null });

    render(
      <JoinActions
        uuid={UUID}
        capacity={1}
        participates
        joinable={false}
        requested={false}
      />,
    );

    // Формы заявки нет: второй подачи быть не может.
    expect(screen.queryByRole("textbox")).toBeNull();
    const button = screen.getByRole("button", { name: "Отменить участие" });
    await userEvent.click(button);

    await waitFor(() => expect(leaveCard).toHaveBeenCalledWith(UUID));
    // Отмена УЧАСТИЯ — маршрут `/cards/{uuid}/join`, не `/requests/{uuid}`.
    expect(cancelJoinRequest).not.toHaveBeenCalled();
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("capacity=1, заявка pending И участие → «Моя заявка» побеждает", () => {
    // Состояние «заявка + участие» на одной карточке сервер не отдаёт, но
    // порядок ветвей обязан быть определён: `requested` проверяется раньше
    // `participates`, иначе человек видел бы «Отменить участие» на заявке.
    render(
      <JoinActions
        uuid={UUID}
        capacity={1}
        participates
        joinable={false}
        requested
        requestUuid={REQUEST_UUID}
      />,
    );

    expect(screen.getByText("Моя заявка")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отменить участие" })).toBeNull();
  });
});

describe("JoinActions: безличность истёкшей заявки (AC-6, FR81)", () => {
  it("нет заявки и карточка не joinable → ни формы, ни текста об отказе", () => {
    render(<JoinActions uuid={UUID} capacity={1} participates={false} joinable={false} />);

    expect(screen.queryByRole("textbox")).toBeNull();
    for (const forbidden of ["не выбрали", "отказ", "заявка отклонена", "автор предпочёл"]) {
      expect(screen.queryByText(new RegExp(forbidden, "i"))).toBeNull();
    }
  });
});
