/**
 * Пустота `SupportTicketList` §8.3 (долг L-DS4-3, волна 4).
 *
 * Волна 4 перевела самописную плашку на `EmptyState` — и не добавила теста.
 * Вернуть `<div>` с тем же текстом ничего не сломает, хотя §7.8 требует
 * единый компонент пустоты (роль заголовка, единые отступы, единый вид
 * на всех экранах).
 *
 * Проверяется именно КОМПОНЕНТ, а не текст: заголовок `h2` + сохранённая
 * ссылка на «Конфиденциальность» (единственный путь написать, если форма
 * даёт `403`).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SupportTicketList } from "@/components/support/support-list";

describe("SupportTicketList: пустота §8.3", () => {
  it("пустой массив даёт EmptyState с заголовком h2, а не самописную плашку", () => {
    render(<SupportTicketList items={[]} />);

    // `EmptyState` рендерит `title` как `h2` (§7.8). Самописная плашка
    // обычно `<div>`/`<p>` — тогда единый вид и структура заголовков теряются.
    expect(
      screen.getByRole("heading", { level: 2, name: "Обращений пока нет" }),
    ).toBeInTheDocument();
    // Причина объясняет, КУДА писать, а не только «пусто».
    expect(screen.getByText(/Опишите проблему в форме выше/)).toBeInTheDocument();
  });

  it("сохраняет ссылку на «Конфиденциальность» — путь, когда форма недоступна", () => {
    render(<SupportTicketList items={[]} />);

    // Блокированный аккаунт получает 403 на форме: почта в этом же
    // пустом состоянии — единственный способ связаться.
    const link = screen.getByRole("link", { name: /Конфиденциальность/i });
    expect(link).toHaveAttribute("href", "/privacy#contact");
  });

  it("непустой список НЕ показывает пустоту", () => {
    // Обратная сторона: `items.length === 0` не должно срабатывать всегда.
    render(
      <SupportTicketList
        items={[
          {
            uuid: "11111111-1111-4111-8111-111111111111",
            body: "Не приходит письмо",
            status: "open",
            created_at: "2026-10-24T10:00:00Z",
            answer: null,
            answered_at: null,
          },
        ]}
      />,
    );

    expect(screen.queryByText("Обращений пока нет")).toBeNull();
    expect(screen.getByText("Не приходит письмо")).toBeInTheDocument();
  });
});
