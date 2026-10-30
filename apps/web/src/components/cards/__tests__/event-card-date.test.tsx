/**
 * Тесты даты карточки (стори 12.5, AC-3): форматирование и относительная надпись.
 *
 * До этой стори `formatStartsAt` **не покрывался ни одним тестом** (замер: 0
 * совпадений в `*.test.tsx`), а брал зону браузера. Здесь фиксируется контракт
 * формата и поведение на битой дате — без правки зоны (долг `W-12.5-a`).
 */

import { describe, expect, it } from "vitest";

import {
  formatRelativeDay,
  formatStartsAt,
  formatStartsAtAccessible,
} from "@/components/cards/event-card";

// Фиксированные якоря: тест не зависит от текущей даты.
const NOW = "2026-10-29T12:00:00+03:00";

describe("formatStartsAt (AC-3)", () => {
  it("«Чт, 19:00» — день с заглавной и время через двоеточие", () => {
    expect(formatStartsAt("2026-10-29T19:00:00+03:00")).toBe("Чт, 19:00");
  });

  it("однозначный час дополняется нулём", () => {
    expect(formatStartsAt("2026-10-29T09:05:00+03:00")).toBe("Чт, 09:05");
  });

  it("битая дата — пустая строка, не `Invalid Date` (U-12)", () => {
    // Мутация «убрать isNaN-ветку» — тест краснеет на `Invalid Date, NaN:NaN`.
    expect(formatStartsAt("not-a-date")).toBe("");
    expect(formatStartsAt("")).toBe("");
  });
});

describe("formatStartsAtAccessible (AC-3)", () => {
  it("«в четверг в 19:00» — форма для aria-label", () => {
    expect(formatStartsAtAccessible("2026-10-29T19:00:00+03:00")).toBe("в четверг в 19:00");
  });

  it("битая дата — пустая строка", () => {
    expect(formatStartsAtAccessible("nope")).toBe("");
  });
});

describe("formatRelativeDay (AC-3)", () => {
  it("«через 2 дня» для +48 ч (U-13)", () => {
    // Мутация «порог 2 дня заменить на 1 день» — тест краснеет.
    expect(formatRelativeDay("2026-10-31T12:00:00+03:00", NOW)).toBe("через 2 дня");
  });

  it("«завтра» для +20 ч", () => {
    expect(formatRelativeDay("2026-10-30T08:00:00+03:00", NOW)).toBe("завтра");
  });

  it("пусто для +3 ч (сегодня) — дата уже видна рядом", () => {
    expect(formatRelativeDay("2026-10-29T15:00:00+03:00", NOW)).toBe("");
  });

  it("пусто для прошедшего", () => {
    expect(formatRelativeDay("2026-10-28T12:00:00+03:00", NOW)).toBe("");
  });

  it("битые аргументы — пустая строка, не исключение", () => {
    expect(formatRelativeDay("nope", NOW)).toBe("");
    expect(formatRelativeDay("2026-10-31T12:00:00+03:00", "nope")).toBe("");
  });

  it("календарные сутки, а не 24 часа: 23:00 → 02:00 = «завтра»", () => {
    // Разница 3 часа, но календарно это следующие сутки. Мутация «просто
    // поделить разницу на 86_400_000» — тест краснеет.
    expect(formatRelativeDay("2026-10-30T02:00:00+03:00", "2026-10-29T23:00:00+03:00")).toBe(
      "завтра",
    );
  });
});
