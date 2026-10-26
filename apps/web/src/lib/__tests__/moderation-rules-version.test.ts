/**
 * Гард версии правил модерации (стори 10.4; AC-03.3, D-10.4-3).
 *
 * **Что проверяется.** SHA-256 нормализованного текста разделов должен
 * совпадать с ожидаемым. Если формулировка изменилась, а `version` и хеш — нет,
 * этот тест красный. Без него AC-03 «версия фиксируется» был бы комментарием:
 * текст правят, версия остаётся `1.0`, и документ врёт о своей редакции.
 *
 * **Как чинить красный тест.** Не подставлять новый хеш молча. Порядок:
 * 1) решить, это правка смысла или опечатка; 2) если смысл — бампнуть
 * `MODERATION_RULES_VERSION`; 3) затем пересчитать `EXPECTED_SHA256`.
 * Хеш — следствие версии, а не замена ей.
 *
 * **Чего хеш не ловит.** Удаление раздела вместе с его упоминанием в тестах
 * страницы — отдельные проверки структуры живут в `rules-page.test.tsx`
 * (там проверяется наличие всех шести `h2` и раздела критериев).
 */

import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import {
  MODERATION_RULES_EFFECTIVE_FROM,
  MODERATION_RULES_SECTIONS,
  MODERATION_RULES_VERSION,
  formatEffectiveFrom,
  rulesTextForHash,
  rulesVersionLabel,
} from "@/lib/moderation-rules";

/**
 * Ожидаемый SHA-256 нормализованного текста редакции 1.0.
 *
 * Пересчитан командой:
 * `npx esbuild src/lib/moderation-rules.ts --format=esm --outfile=/tmp/mr.mjs`
 * и `createHash("sha256").update(rulesTextForHash()).digest("hex")`.
 */
const EXPECTED_SHA256 = "a8a7bdb3336610b642efb11ed3d7cdaf342789e9ffd312e33f5ee2face4b7f0c";

/** Сколько разделов обещано в D-10.4-7. */
const EXPECTED_SECTION_COUNT = 6;

describe("Правила модерации: версия", () => {
  it("версия объявлена строкой semver-подобного вида", () => {
    // AC-03.1: версия — одна константа, страница её читает.
    expect(MODERATION_RULES_VERSION).toMatch(/^\d+\.\d+$/);
  });

  it("дата редакции — валидная ISO-дата", () => {
    // AC-03.2: дата отображается на странице, значит должна быть разбираемой.
    expect(MODERATION_RULES_EFFECTIVE_FROM).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    const parsed = new Date(`${MODERATION_RULES_EFFECTIVE_FROM}T00:00:00Z`);
    expect(Number.isNaN(parsed.getTime())).toBe(false);
  });

  it("строка редакции собирается из констант и содержит дату по-русски", () => {
    // AC-03.2: «Редакция 1.0 · действует с 26 октября 2026».
    expect(rulesVersionLabel()).toBe(`Редакция ${MODERATION_RULES_VERSION} · действует с 26 октября 2026`);
    expect(formatEffectiveFrom("2026-10-26")).toBe("26 октября 2026");
    expect(formatEffectiveFrom("2026-01-05")).toBe("5 января 2026");
    // Неразбираемая дата возвращается как есть — страница не падает.
    expect(formatEffectiveFrom("мусор")).toBe("мусор");
    expect(formatEffectiveFrom("2026-13-01")).toBe("2026-13-01");
  });

  it("текст правил не менялся без бампа редакции (SHA-256)", () => {
    // AC-03.3: главный гард стори. Красный тест = текст изменился.
    const actual = createHash("sha256").update(rulesTextForHash(), "utf8").digest("hex");
    expect(
      actual,
      "Текст правил изменился без бампа редакции: поднимите MODERATION_RULES_VERSION и пересчитайте EXPECTED_SHA256",
    ).toBe(EXPECTED_SHA256);
  });

  it("нормализация хеша не зависит от пробелов по краям", () => {
    // Task 0.3: trim + один \n. Иначе косметика ломала бы гард.
    const text = rulesTextForHash();
    expect(text).toBe(text.trim());
    expect(text).not.toMatch(/\n\n/);
    expect(text).not.toContain("1.0"); // версия в хеш не входит
    expect(text).not.toContain(MODERATION_RULES_EFFECTIVE_FROM);
  });

  it("шесть разделов, у каждого непустые заголовок и пункты", () => {
    // D-10.4-7: структура — часть обещания, а не оформление.
    expect(MODERATION_RULES_SECTIONS).toHaveLength(EXPECTED_SECTION_COUNT);
    for (const section of MODERATION_RULES_SECTIONS) {
      expect(section.title.trim().length).toBeGreaterThan(0);
      expect(section.items.length).toBeGreaterThan(0);
      for (const item of section.items) {
        expect(item.trim().length).toBeGreaterThan(0);
      }
    }
    // Идентификаторы уникальны — иначе якоря и React key совпадут.
    const ids = MODERATION_RULES_SECTIONS.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
