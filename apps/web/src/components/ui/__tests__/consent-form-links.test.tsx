/**
 * Ссылки на документы в форме согласия (стори 10.4; AC-03.4, D-10.4-6).
 *
 * **Почему тест создаётся с нуля.** На момент стори у `consent-form` не было
 * ни одного теста (аудит: 0 файлов). Правка продакшен-компонента без теста —
 * ровно тот класс, из-за которого появилось правило «что нельзя проверить,
 * нельзя обещать»: ссылка на правила могла исчезнуть при следующей правке
 * формы, и никто бы не заметил.
 *
 * **Что именно проверяется.**
 * 1. Три документа доступны в момент согласия: политика ПД, оферта, правила.
 * 2. Ссылки открываются в новой вкладке (`target="_blank"`) без передачи
 *    `Referer` (`rel="noreferrer"`) — как соседние ссылки, единый приём.
 * 3. Ссылки НЕ находятся внутри `<label>` (патч 18 код-ревью 1.6): клик по
 *    ссылке внутри метки переключал чекбокс, и пользователь возвращался с
 *    уже отмеченным согласием, которого не давал. Правка добавляет третью
 *    ссылку — гард обязан проверить, что она не попала в метку.
 * 4. Ссылки связаны с чекбоксом через `aria-describedby`.
 *
 * `next/navigation` замокан: `useRouter` без смонтированного App Router
 * бросает `invariant expected app router to be mounted` ДО проверок.
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConsentForm } from "@/components/ui/consent-form";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

/** Документы, которые обязаны быть доступны при согласии. */
const DOC_HREFS = ["/privacy", "/terms", "/rules"] as const;

describe("ConsentForm: документы в момент согласия", () => {
  it("содержит ссылки на политику ПД, оферту и правила", () => {
    // AC-03.4: без ссылки на /rules правила существуют, но их не видно
    // там, где пользователь принимает решение.
    render(<ConsentForm />);

    for (const href of DOC_HREFS) {
      expect(screen.getByRole("link", { name: new RegExp(hrefName(href)) })).toHaveAttribute(
        "href",
        href,
      );
    }
  });

  it("открывает документы в новой вкладке без Referer", () => {
    // Единый приём с /privacy и /terms: форма согласия не должна закрываться.
    render(<ConsentForm />);

    const docs = screen.getByText(/Условия — в/).closest("p");
    expect(docs).not.toBeNull();
    for (const link of within(docs as HTMLElement).getAllByRole("link")) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noreferrer");
    }
  });

  it("ни одна ссылка не находится внутри label", () => {
    // Патч 18: клик по ссылке внутри label переключает чекбокс — согласие,
    // которого пользователь не давал.
    const { container } = render(<ConsentForm />);

    for (const link of container.querySelectorAll("a")) {
      expect(link.closest("label"), `ссылка внутри label: ${link.getAttribute("href")}`).toBeNull();
    }
  });

  it("чекбокс согласия ссылается на абзац с документами через aria-describedby", () => {
    // Связь сохраняется, даже когда ссылки вынесены из label.
    render(<ConsentForm />);

    const consent = screen.getByRole("checkbox", { name: /согласие на обработку/i });
    expect(consent).toHaveAttribute("aria-describedby", "consent-hint consent-docs");
  });
});

/**
 * Имя ссылки для поиска по роли: текст ссылки в форме.
 * @param href путь документа
 */
function hrefName(href: string): string {
  switch (href) {
    case "/privacy":
      return "политике обработки персональных данных";
    case "/terms":
      return "оферте";
    case "/rules":
      return "правилах";
    default:
      return href;
  }
}
