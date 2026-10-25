/**
 * UI-тесты `SystemMessage` (стори 6.7, AC-04; UX §9).
 *
 * Проверяются именно инварианты, обещанные в стори:
 *
 * - системное сообщение ОТОБРАЖАЕТСЯ без автора (AC-04);
 * - кнопки удаления нет (AC-04: «нельзя удалить»);
 * - текст выводится как ТЕКСТ, а не HTML (D-6.7-5: шаблон только серверный);
 * - объявлено `role="status"` — пояснение события, не реплика переписки.
 *
 * «Нет кнопки удаления» проверяется запросом по роли, а не по классу: класс
 * переименуют, а роль останется. Тест обязан краснеть от появления кнопки,
 * а не от смены стилей.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SystemMessage } from "@/components/chat/system-message";

const CREATED_AT = "2026-10-24T10:00:00Z";


describe("SystemMessage", () => {
  it("отображает системный текст без автора", () => {
    render(<SystemMessage body="Участник отменил участие" createdAt={CREATED_AT} />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Участник отменил участие");
    // AC-04: ни аватара, ни имени автора — их не из чего взять.
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("не даёт удалить системное сообщение", () => {
    render(<SystemMessage body="Автор отменил встречу" createdAt={CREATED_AT} />);

    // AC-04: ни одной кнопки, тем более «Удалить».
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryByText(/удал/i)).toBeNull();
  });

  it("выводит тело как текст, а не как HTML", () => {
    const hostile = '<img src=x onerror="alert(1)">';
    const { container } = render(<SystemMessage body={hostile} createdAt={CREATED_AT} />);

    // D-6.7-5: строка отрисована текстом; HTML-узла из неё не появилось.
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent(hostile);
  });

  it("показывает время события", () => {
    render(<SystemMessage body="В компании новый участник" createdAt={CREATED_AT} />);

    // Время в локальной зоне: проверяется форма «чч:мм», не конкретная зона.
    expect(screen.getByText(/^\d{2}:\d{2}$/)).toBeInTheDocument();
  });
});
