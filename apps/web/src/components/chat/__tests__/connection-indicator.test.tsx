/**
 * Тесты индикатора соединения (стори 6.5; TD 6.5: V3, V7).
 *
 * Проверяются ПОВЕДЕНИЯ, обещанные в AC-03 и UX-правилах проекта:
 * тонкая полоса вместо модалки, отсутствие при `online`, текстовый статус
 * (NFR57 — не только цвет).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConnectionIndicator } from "@/components/chat/connection-indicator";

describe("ConnectionIndicator", () => {
  it("V3: при online не рисуется вовсе", () => {
    const { container } = render(<ConnectionIndicator state="online" />);

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("V3: при reconnecting — тонкая полоса, НЕ модалка", () => {
    render(<ConnectionIndicator state="reconnecting" />);

    const strip = screen.getByRole("status");
    expect(strip).toBeInTheDocument();
    // Модалка определяется ролью `dialog` или `aria-modal` — ни того, ни
    // другого быть не должно: полоса не перекрывает переписку.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(strip).not.toHaveAttribute("aria-modal");
    expect(strip).toHaveAttribute("data-connection-state", "reconnecting");
  });

  it("V3: текст статуса доступен скринридеру (NFR57)", () => {
    render(<ConnectionIndicator state="reconnecting" />);

    expect(screen.getByText("Нет связи — переподключаемся")).toBeInTheDocument();
  });

  it("V3 (второе значение, §18.11): offline отличается от reconnecting", () => {
    render(<ConnectionIndicator state="offline" />);

    const strip = screen.getByRole("status");
    expect(strip).toHaveAttribute("data-connection-state", "offline");
    expect(screen.getByText("Нет связи")).toBeInTheDocument();
    expect(screen.queryByText("Нет связи — переподключаемся")).not.toBeInTheDocument();
  });

  it("V7: aria-live polite — не перебивает чтение переписки", () => {
    render(<ConnectionIndicator state="reconnecting" />);

    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
  });
});
