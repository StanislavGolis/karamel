#!/usr/bin/env node
/**
 * ГЕЙТ 2 (§14.5) — скрипт контраста в CI (светлая тема).
 *
 * Читает значения токенов из `globals.css` (`@theme`), считает WCAG 2.1 и
 * падает, если нарушено хотя бы одно правило:
 *
 * 1. текстовая роль < 4.5:1 к `bg`, `surface` или `surface-raised`;
 * 2. `border-input` / `focus-ring` < 3:1;
 * 3. текст на `accent` < 4.5:1;
 * 4. текст статуса на СВОЁМ тинте < 4.5:1 — ревью Н-1: без этой проверки
 *    провал Б-2 (нечитаемый бейдж) прошёл бы;
 * 5. текст на сплошной статусной заливке < 4.5:1.
 *
 * **Почему в CI, а не «проверим руками».** Расхождение `--border-input:
 * #8a7f76` из §1.2 артефакта — ровно результат ручной проверки: значение
 * поставили, контраст не посчитали. Ручная проверка не оставляет следа и
 * не повторяется на следующем PR.
 *
 * **Формат вывода** — таблица (как Приложение А) + код выхода 1 при провале:
 * артефакт прикладывается к PR, и видно, что изменилось.
 *
 * Гейт 4 (§14.7) — то же самое для ОБЕИХ тем. Он импортирует эту же функцию
 * проверки: «расширение» означает «та же логика на втором наборе», а не
 * «второй скрипт с похожей логикой», который разойдётся через месяц.
 */

import { resolve } from "node:path";

import { GLOBALS_CSS_PATH, loadTokens } from "./lib/tokens.mjs";
import { printTable, verifyTheme } from "./lib/verify.mjs";

function main() {
  const tokens = loadTokens(GLOBALS_CSS_PATH);
  const { rows, errors, warnings } = verifyTheme(tokens.lightColors, "светлая");

  printTable(rows, "КОНТРАСТЫ — СВЕТЛАЯ ТЕМА");

  if (warnings.length > 0) {
    console.log("\n⚠️  Не проверено:");
    for (const w of warnings) console.log(`  • ${w}`);
  }

  if (errors.length > 0) {
    console.error(`\n❌ ГЕЙТ 2 (контраст, светлая): ${errors.length} провалов\n`);
    for (const e of errors) console.error(`  • ${e}`);
    process.exit(1);
  }

  console.log(`\n✅ ГЕЙТ 2: ${rows.length} пар, минимум пройден`);
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  main();
}
