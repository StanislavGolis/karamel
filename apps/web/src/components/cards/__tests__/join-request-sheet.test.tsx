/**
 * Тесты `JoinRequestSheet` — три состояния §8.3, волна 3 миграции.
 *
 * **Почему эти три.** Матрица §8.3 задаёт для `JoinRequestSheet` ровно
 * `error`, `success` и `offline`; `loading`/`empty`/`stale` к панели заявки не
 * применяются. Каждое состояние обязано быть проверено тестом (§8.5: «мутация
 * „убрал состояние“ обязана покрасить тест»), иначе через месяц кто-то
 * «упростит» панель до одной кнопки, и тесты останутся зелёными.
 *
 * **Сеть мокается, а не поднимается.** `joinCard` — единственная точка выхода;
 * мок позволяет проверить ВЕТКУ, не поднимая BFF. Это тот же приём, что в
 * `chat-screen.test.tsx`.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JoinActions, JoinRequestSheet } from "@/components/cards/join-actions";

const joinCard = vi.fn();
const leaveCard = vi.fn();
const refresh = vi.fn();

vi.mock("@/lib/queries/cards", () => ({
  joinCard: (...args: unknown[]) => joinCard(...args),
  leaveCard: (...args: unknown[]) => leaveCard(...args),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

const UUID = "11111111-1111-1111-1111-111111111111";

beforeEach(() => {
  joinCard.mockReset();
  leaveCard.mockReset();
  refresh.mockReset();
});

describe("JoinRequestSheet: успех (§8.3)", () => {
  it("после успешной отправки показывает карточку «Заявка отправлена»", async () => {
    joinCard.mockResolvedValue({ ok: true, data: null });
    const onDone = vi.fn();

    render(<JoinRequestSheet uuid={UUID} open onOpenChange={() => {}} onDone={onDone} />);

    await userEvent.click(screen.getByRole("button", { name: "Присоединиться" }));

    // §8.3: success — карточка «Заявка отправлена» с `role="status"`.
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Заявка отправлена");
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(joinCard).toHaveBeenCalledWith(UUID);
  });
});

describe("JoinRequestSheet: ошибка (§8.3, §8.4 п.3)", () => {
  it("inline-сообщение под полем, панель остаётся открытой", async () => {
    joinCard.mockResolvedValue({ ok: false, code: "CARD_IS_FULL" });

    render(<JoinRequestSheet uuid={UUID} open onOpenChange={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: "Присоединиться" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Свободных мест больше нет");
    // §8.4 п.3: ввод (содержимое панели) сохранён — заголовок панели на месте,
    // то есть панель не закрылась и не заменилась сообщением.
    expect(screen.getByText("Присоединиться к встрече?")).toBeInTheDocument();
  });

  it("неизвестный код не показывает сырой код пользователю", async () => {
    joinCard.mockResolvedValue({ ok: false, code: "http_500" });

    render(<JoinRequestSheet uuid={UUID} open onOpenChange={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Присоединиться" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).not.toContain("http_500");
  });
});

describe("JoinRequestSheet: offline (§8.3)", () => {
  it("кнопка заблокирована и есть пояснение", () => {
    render(<JoinRequestSheet uuid={UUID} open offline onOpenChange={() => {}} />);

    const button = screen.getByRole("button", { name: /Присоединиться/ });
    expect(button).toBeDisabled();
    expect(screen.getByText(/Нет связи\. Присоединиться можно будет/)).toBeInTheDocument();
  });

  it("при offline запрос не уходит", async () => {
    render(<JoinRequestSheet uuid={UUID} open offline onOpenChange={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: /Присоединиться/ }));
    expect(joinCard).not.toHaveBeenCalled();
  });
});

describe("JoinActions: переход на панель вместо мгновенного запроса", () => {
  it("«Присоединиться» открывает панель, а не шлёт POST сразу", async () => {
    render(<JoinActions uuid={UUID} participates={false} joinable />);

    await userEvent.click(screen.getByRole("button", { name: "Присоединиться" }));

    // Волна 3: одно касание больше не ставит в компанию — нужен шаг подтверждения.
    expect(joinCard).not.toHaveBeenCalled();
    expect(await screen.findByText("Присоединиться к встрече?")).toBeInTheDocument();
  });

  it("«Отменить участие» шлёт запрос и перечитывает карточку", async () => {
    leaveCard.mockResolvedValue({ ok: true, data: null });

    render(<JoinActions uuid={UUID} participates joinable />);
    await userEvent.click(screen.getByRole("button", { name: "Отменить участие" }));

    await waitFor(() => expect(leaveCard).toHaveBeenCalledWith(UUID));
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("ни одного действия, когда карточка не принимает участие", () => {
    render(<JoinActions uuid={UUID} participates={false} joinable={false} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
