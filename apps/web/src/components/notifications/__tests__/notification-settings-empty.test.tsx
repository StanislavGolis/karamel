/**
 * Защитная пустота `NotificationSettings` §8.3 (долг L-DS4-3, волна 4).
 *
 * Сервер обязан отдавать 11 типов (D-9.3), поэтому пустой `initial` — это
 * сбой на нашей стороне, а не нормальный экран. Ветка защитная: она
 * включается при ПРЯМОМ рендере компонента, и её легко удалить незаметно.
 *
 * Проверяется: пустой список даёт `EmptyState` с объяснением «это сбой»,
 * а не пустой `<ul>` и не пустую страницу настроек.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { NotificationSettings } from "@/components/notifications/notification-settings";

describe("NotificationSettings: защитная пустота §8.3", () => {
  it("пустой список типов даёт EmptyState, а не пустой список", () => {
    render(<NotificationSettings initial={[]} />);

    expect(
      screen.getByRole("heading", { level: 2, name: "Пока нет уведомлений" }),
    ).toBeInTheDocument();
    // Объяснение честное: причина на сервере, а не «вы всё отключили».
    expect(screen.getByText(/Список типов приходит с сервера/)).toBeInTheDocument();
    // Переключателей нет — иначе пустота маскировала бы сломанный контракт.
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });

  it("непустой список показывает переключатели без пустоты", () => {
    render(
      <NotificationSettings
        initial={[
          { type: "company.assembled", enabled: true, required: false },
          { type: "attendance.result", enabled: true, required: true },
        ]}
      />,
    );

    expect(screen.queryByText("Пока нет уведомлений")).toBeNull();
    expect(screen.getByText("Компания собрана")).toBeInTheDocument();
    expect(screen.getByText("Результат явки")).toBeInTheDocument();
  });
});
