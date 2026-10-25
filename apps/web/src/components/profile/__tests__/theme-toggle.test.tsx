/**
 * Тесты `ThemeToggle` (§17.9 артефакта).
 *
 * **Главный тест — клик перерисовывает выбор.** Прошлая версия хука была
 * четырьмя чистыми функциями: клик менял атрибут на `<html>`, но `aria-checked`
 * оставался на прежней опции, и переключатель выглядел «залипшим» (Б-8).
 *
 * Остальные тесты фиксируют решения §17.9, которые иначе теряются при
 * рефакторинге: `radio`, а не `switch`; roving tabindex; стрелки; тап-зона 44px;
 * текст рядом с иконкой (NFR57).
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ThemeToggle } from "@/components/profile/theme-toggle";
import { THEME_STORAGE_KEY } from "@/lib/theme";

describe("ThemeToggle", () => {
  it("это radiogroup из двух опций, а не switch (§17.9)", () => {
    render(<ThemeToggle />);

    const group = screen.getByRole("radiogroup");
    expect(group).toBeInTheDocument();

    const options = screen.getAllByRole("radio");
    expect(options).toHaveLength(2);

    // Решение осознанное: `switch` подразумевал бы «тема одна, её можно
    // выключить». Проверяем именно отсутствие переключателей.
    expect(screen.queryAllByRole("switch")).toHaveLength(0);
  });

  it("обе опции имеют видимый текст (NFR57)", () => {
    render(<ThemeToggle />);

    expect(screen.getByRole("radio", { name: /Светлая/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Тёмная/ })).toBeInTheDocument();
  });

  it("по умолчанию выбрана светлая", () => {
    render(<ThemeToggle />);

    expect(screen.getByRole("radio", { name: /Светлая/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /Тёмная/ })).toHaveAttribute("aria-checked", "false");
  });

  it("клик по «Тёмная» перерисовывает выбор (Б-8 — главный тест волны 1)", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    await user.click(screen.getByRole("radio", { name: /Тёмная/ }));

    // Если убрать notifyAll() из apply(), `aria-checked` останется на
    // «Светлая»: атрибут data-theme сменится, React не перерисуется.
    expect(screen.getByRole("radio", { name: /Тёмная/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /Светлая/ })).toHaveAttribute("aria-checked", "false");

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  it("выбор применяется сразу, без кнопки «Сохранить»", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    await user.click(screen.getByRole("radio", { name: /Тёмная/ }));

    // Никакой второй кнопки в разметке нет: применение — побочный эффект клика.
    expect(screen.queryByRole("button", { name: /Сохранить/ })).toBeNull();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("roving tabindex: в Tab-порядке только выбранная опция", () => {
    render(<ThemeToggle />);

    expect(screen.getByRole("radio", { name: /Светлая/ })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: /Тёмная/ })).toHaveAttribute("tabindex", "-1");
  });

  it("стрелка вправо переключает на тёмную и переносит фокус", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    const light = screen.getByRole("radio", { name: /Светлая/ });
    light.focus();

    await user.keyboard("{ArrowRight}");

    const dark = screen.getByRole("radio", { name: /Тёмная/ });
    expect(dark).toHaveAttribute("aria-checked", "true");
    expect(dark).toHaveFocus();
  });

  it("стрелка влево переключает обратно на светлую", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    await user.click(screen.getByRole("radio", { name: /Тёмная/ }));
    await user.keyboard("{ArrowLeft}");

    expect(screen.getByRole("radio", { name: /Светлая/ })).toHaveAttribute("aria-checked", "true");
  });

  it("стрелки ходят по кругу", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    // Из «Светлая» влево — попадаем на «Тёмная» (последний элемент).
    screen.getByRole("radio", { name: /Светлая/ }).focus();
    await user.keyboard("{ArrowLeft}");

    expect(screen.getByRole("radio", { name: /Тёмная/ })).toHaveAttribute("aria-checked", "true");
  });

  it("каждая опция имеет тап-зону не меньше 44px", () => {
    render(<ThemeToggle />);

    // Классы min-h-11 / min-w-11 = 44px по 4px-шкале (§4.1, §4.6).
    // Проверяем токен-класс, а не вычисленный размер: jsdom не считает layout,
    // и `getBoundingClientRect()` вернул бы нули, то есть проверка была бы
    // фиктивной.
    for (const option of screen.getAllByRole("radio")) {
      expect(option.className).toContain("min-h-11");
      expect(option.className).toContain("min-w-11");
    }
  });

  it("фокус виден: 2px focus-ring с отступом 2px", () => {
    render(<ThemeToggle />);

    for (const option of screen.getAllByRole("radio")) {
      expect(option.className).toContain("focus-visible:outline-2");
      expect(option.className).toContain("focus-visible:outline-offset-2");
      expect(option.className).toContain("focus-visible:outline-focus-ring");
    }
  });

  it("не использует hex и произвольные цвета (гейт 1 в миниатюре)", () => {
    render(<ThemeToggle />);

    for (const option of screen.getAllByRole("radio")) {
      expect(option.className).not.toMatch(/#[0-9a-fA-F]{3,8}/);
      expect(option.className).not.toMatch(/\[#/);
    }
  });
});
