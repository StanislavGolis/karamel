#!/usr/bin/env node
/**
 * ГЕЙТ 4 (§14.7) — контраст ОБЕИХ тем (расширение гейта 2).
 *
 * Правило «текст >= 4.5:1 к bg/surface/surface-raised; границы >= 3:1;
 * текст на своём тинте >= 4.5:1» применяется к КАЖДОМУ набору отдельно.
 *
 * **Почему нельзя проверить только светлую.** `danger` в тёмной меняет
 * светлоту на противоположную (#A0281C -> #E0705E), и светлая пара ничего
 * не говорит о тёмной. Тинты в тёмной — «тёмные подложки»: при alpha 14%
 * `danger` на своём тинте даёт 4.37:1, ниже нормы (§17.2). Без отдельного
 * прогона такой провал не виден: ошибки сборки нет, бейдж просто нечитаем.
 *
 * В отчёт прикладываются обе таблицы рядом — как Приложение Е.
 */

import { resolve } from "node:path";

import { GLOBALS_CSS_PATH, loadTokens } from "./lib/tokens.mjs";
import { printTable, verifyTheme } from "./lib/verify.mjs";

function main() {
  const tokens = loadTokens(GLOBALS_CSS_PATH);

  if (!tokens.darkColors) {
    console.error('\n❌ ГЕЙТ 4: нет блока [data-theme="dark"] — тёмную тему проверить нечем\n');
    process.exit(1);
  }

  const light = verifyTheme(tokens.lightColors, "светлая");
  const dark = verifyTheme(tokens.darkColors, "тёмная");

  printTable(light.rows, "КОНТРАСТЫ — СВЕТЛАЯ ТЕМА");
  printTable(dark.rows, "КОНТРАСТЫ — ТЁМНАЯ ТЕМА");

  const warnings = [...light.warnings, ...dark.warnings];
  if (warnings.length > 0) {
    console.log("\n⚠️  Не проверено:");
    for (const w of warnings) console.log(`  • ${w}`);
  }

  const errors = [...light.errors, ...dark.errors];
  if (errors.length > 0) {
    console.error(`\n❌ ГЕЙТ 4 (контраст, обе темы): ${errors.length} провалов\n`);
    for (const e of errors) console.error(`  • ${e}`);
    process.exit(1);
  }

  console.log(
    `\n✅ ГЕЙТ 4: светлая ${light.rows.length} пар, тёмная ${dark.rows.length} пар — обе прошли`,
  );
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  main();
}
