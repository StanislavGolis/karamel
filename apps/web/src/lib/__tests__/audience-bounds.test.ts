/**
 * Рамки аудитории: пара границ и подпись (стори 12.6, AC-1a/AC-2, Task 7.2–7.3).
 *
 * Каждое утверждение — парой (P-21): правило `<=` не отличить от константы
 * одним зелёным тестом, поэтому «валидная» и «перевёрнутая» проверяются
 * вместе, а полуоткрытые пары — отдельно (их законность легко потерять при
 * ужесточении до «обе или ни одной»).
 */

import { describe, expect, it } from "vitest";

import {
  AGE_RANGE_MESSAGE,
  ageRangeSchema,
  audienceBoundsText,
  isAgeRangeValid,
} from "@/lib/audience-bounds";

describe("isAgeRangeValid — пара возрастных границ", () => {
  it("перевёрнутая пара невалидна (иначе сервер вернёт 422 вместо inline-ошибки)", () => {
    expect(isAgeRangeValid(40, 30)).toBe(false);
  });

  it("прямая пара валидна — пара к предыдущему, иначе `false` не отличить от «всегда»", () => {
    expect(isAgeRangeValid(30, 40)).toBe(true);
  });

  it("равные границы валидны: CHECK `age_min <= age_max` разрешает равенство", () => {
    expect(isAgeRangeValid(30, 30)).toBe(true);
  });

  it("полуоткрытая пара валидна: «от 30» — легальный запрос (AC-1a)", () => {
    expect(isAgeRangeValid(30, undefined)).toBe(true);
  });

  it("полуоткрытая пара валидна: «до 35» — легальный запрос (AC-1a)", () => {
    expect(isAgeRangeValid(undefined, 35)).toBe(true);
  });

  it("обе границы пусты — валидно: «ищу любого» не ошибка", () => {
    expect(isAgeRangeValid(undefined, undefined)).toBe(true);
  });
});

describe("ageRangeSchema — Zod-схема пары", () => {
  it("перевёрнутая пара отклоняется с сообщением на поле `age_min`", () => {
    const parsed = ageRangeSchema.safeParse({ age_min: 40, age_max: 30 });
    expect(parsed.success).toBe(false);
    if (!parsed.success) {
      expect(parsed.error.issues[0]?.message).toBe(AGE_RANGE_MESSAGE);
      expect(parsed.error.issues[0]?.path).toEqual(["age_min"]);
    }
  });

  it("прямая пара проходит — пара к предыдущему", () => {
    const parsed = ageRangeSchema.safeParse({ age_min: 25, age_max: 35 });
    expect(parsed.success).toBe(true);
  });

  it("полуоткрытая пара проходит (верхняя граница не задана)", () => {
    expect(ageRangeSchema.safeParse({ age_min: 30 }).success).toBe(true);
  });

  it("полуоткрытая пара проходит (нижняя граница не задана)", () => {
    expect(ageRangeSchema.safeParse({ age_max: 35 }).success).toBe(true);
  });

  it("возраст ниже 18 отклоняется — границы аудитории начинаются с 18", () => {
    const parsed = ageRangeSchema.safeParse({ age_min: 17 });
    expect(parsed.success).toBe(false);
  });
});

describe("audienceBoundsText — подпись рамок в ленте", () => {
  it("рамок нет — подписи нет («ищу любого» не показываем, UX §1048)", () => {
    expect(audienceBoundsText({ gender_filter: null, age_min: null, age_max: null })).toBeNull();
  });

  it("только пол задан — «Ищет женщин»", () => {
    expect(
      audienceBoundsText({ gender_filter: "female", age_min: null, age_max: null }),
    ).toBe("Ищет женщин");
  });

  it("пол и полный возраст — «Ищет мужчин, 25–35 лет»", () => {
    expect(audienceBoundsText({ gender_filter: "male", age_min: 25, age_max: 35 })).toBe(
      "Ищет мужчин, 25–35 лет",
    );
  });

  it("только нижняя граница — «Ищет возраст: от 30» (полуоткрытая пара, AC-1a)", () => {
    expect(audienceBoundsText({ gender_filter: null, age_min: 30, age_max: null })).toBe(
      "Ищет возраст: от 30",
    );
  });

  it("только верхняя граница — «Ищет возраст: до 35»", () => {
    expect(audienceBoundsText({ gender_filter: null, age_min: null, age_max: 35 })).toBe(
      "Ищет возраст: до 35",
    );
  });
});
