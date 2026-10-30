/**
 * Пометка «вне ваших рамок» в строке заявившегося (стори 12.6, AC-3, Task 7.4).
 *
 * **Кому видно.** Только автору: строка живёт в панели заявок карточки, куда
 * заявитель не попадает. Заявитель признака не получает вовсе (AC-4, FR81) —
 * отказ безличен, и сервер этого поля в `GET /my/requests` не отдаёт.
 *
 * **Тон.** `text-ink-muted`, а не `danger`: это констатация расхождения, а не
 * ошибка человека. Заявитель вне рамок остаётся заявителем — фильтр мягкий
 * (ADR-19), и заявка видна автору целиком.
 *
 * Ключевое различие держится парой (P-21):
 * - `false` → пометка есть;
 * - `true` → пометки НЕТ (прошедший рамки не должен читать «вне рамок»);
 * - `null` → пометки НЕТ (у карточки рамок нет — писать «вне рамок» было бы
 *   ложью).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RequestListRow } from "@/components/requests/request-list-row";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn() }),
}));

const REQUEST_UUID = "66666666-6666-6666-6666-666666666666";

const PERSON = {
  uuid: "77777777-7777-7777-7777-777777777777",
  name: "Анна",
  age: 28,
  photo_url: null,
};

function renderRow(withinBounds: boolean | null) {
  return render(
    <ul>
      <RequestListRow
        requestUuid={REQUEST_UUID}
        person={PERSON}
        message="пойдём вместе"
        selectionAvailable
        cardPublished
        withinBounds={withinBounds}
      />
    </ul>,
  );
}

describe("RequestListRow — пометка рамок аудитории (AC-3, Task 7.4)", () => {
  it("заявитель вне рамок — пометка видна автору", () => {
    renderRow(false);

    expect(screen.getByText("Вне ваших рамок")).toBeInTheDocument();
  });

  it("заявитель в рамках — пометки НЕТ (пара к предыдущему)", () => {
    renderRow(true);

    expect(screen.queryByText("Вне ваших рамок")).toBeNull();
  });

  it("у карточки рамок нет (`null`) — пометки НЕТ: это было бы ложью", () => {
    renderRow(null);

    expect(screen.queryByText("Вне ваших рамок")).toBeNull();
  });

  it("пометка не отменяет ни сообщения, ни кнопки выбора", () => {
    renderRow(false);

    // Мягкий фильтр (FR81, ADR-19): заявка остаётся полноценной.
    expect(screen.getByText("пойдём вместе")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Выбрать" })).toBeInTheDocument();
  });
});
