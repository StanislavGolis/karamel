#!/usr/bin/env node
/**
 * ГЕЙТ 3 (§14.6) — «токен без пары в тёмной теме = ошибка сборки».
 *
 * **Что ловит.** Токен, объявленный в `@theme` (светлая), но отсутствующий в
 * блоке `[data-theme="dark"]`. Такой токен наследует светлое значение: в
 * тёмной теме появляется светлая плашка. Это самый частый дефект тёмных тем
 * и самый незаметный — ошибки сборки нет, цвет просто «не тот».
 *
 * **Проверка в обе стороны.** Лишний токен в тёмной — тоже ошибка: значит его
 * удалили из светлой и забыли убрать здесь. Иначе блок постепенно накапливает
 * мёртвые значения, и «полный блок» перестаёт быть полным.
 *
 * **`--shadow-*` проверяется отдельно.** `--shadow-overlay` — не цвет, но
 * тоже обязан иметь пару (§17.12: тень на тёмном чернее фона).
 *
 * **`color-scheme` — не токен `@theme`,** а обычное CSS-свойство. Проверяется
 * наличием `color-scheme: dark` в блоке тёмной темы напрямую: без него
 * нативные контролы и скроллбар останутся светлыми (§17.8).
 *
 * **Б-1 проверяется здесь же:** правило светлой схемы обязано быть
 * `:root:not([data-theme="dark"])`. Простое `:root { color-scheme: light }`
 * имеет ту же специфичность, что `[data-theme="dark"]`, и, стоя ниже, гасит
 * тёмную схему.
 */

import { join, resolve } from "node:path";
import { readFileSync } from "node:fs";

import { WEB_ROOT, loadTokens } from "./lib/tokens.mjs";

const GLOBALS_CSS = resolve(WEB_ROOT, "src/app/globals.css");

/**
 * Сравнивает два набора ключей и возвращает расхождения.
 * @param {Set<string>} light
 * @param {Set<string>} dark
 * @param {string} label
 * @returns {string[]}
 */
export function diffSets(light, dark, label) {
  const errors = [];
  for (const key of light) {
    if (!dark.has(key)) {
      errors.push(`${label}: «${key}» есть в светлой, нет в [data-theme="dark"] — унаследует светлое значение`);
    }
  }
  for (const key of dark) {
    if (!light.has(key)) {
      errors.push(`${label}: «${key}» есть в тёмной, нет в @theme — мёртвый токен, удалите`);
    }
  }
  return errors;
}

export function checkPairs(tokens) {
  const errors = [];

  if (!tokens.darkColors) {
    return ['globals.css: нет блока [data-theme="dark"] — тёмная тема не объявлена'];
  }

  errors.push(...diffSets(new Set(tokens.lightColors.keys()), new Set(tokens.darkColors.keys()), "цвета"));
  errors.push(...diffSets(new Set(tokens.lightShadows.keys()), new Set(tokens.darkShadows.keys()), "тени"));

  // --- color-scheme в тёмной теме (§17.8) ---
  const darkRaw = tokens.darkBlockRaw ?? "";
  if (!/color-scheme\s*:\s*dark/.test(darkRaw)) {
    errors.push('globals.css: в [data-theme="dark"] нет `color-scheme: dark` — скроллбар и нативные контролы останутся светлыми');
  }

  // --- Б-1: светлая схема только через :not([data-theme="dark"]) ---
  const css = tokens.css;
  const bareRootLight = /:root\s*\{[^}]*color-scheme\s*:\s*light/.test(css);
  const guardedRootLight = /:root:not\(\[data-theme="dark"\]\)\s*\{[^}]*color-scheme\s*:\s*light/.test(css);

  if (bareRootLight && !guardedRootLight) {
    errors.push(
      'globals.css (Б-1): `:root { color-scheme: light }` без :not([data-theme="dark"]) — специфичность равна тёмной, побеждает последнее правило, тёмная тема получит белый скроллбар',
    );
  }
  if (!guardedRootLight) {
    errors.push('globals.css: нет `:root:not([data-theme="dark"]) { color-scheme: light }` — светлая схема не объявлена корректно (Б-1)');
  }

  return errors;
}

function main() {
  const tokens = loadTokens(GLOBALS_CSS);
  const errors = checkPairs(tokens);

  const lightCount = tokens.lightColors.size;
  const darkCount = tokens.darkColors ? tokens.darkColors.size : 0;

  if (errors.length > 0) {
    console.error(`\n❌ ГЕЙТ 3 (пары токенов): ${errors.length} нарушений\n`);
    for (const e of errors) console.error(`  • ${e}`);
    console.error("\nПравило: каждый токен @theme обязан иметь пару в [data-theme=\"dark\"] (§14.6, §17.4).\n");
    process.exit(1);
  }

  console.log(`✅ ГЕЙТ 3: цвета ${lightCount}/${darkCount}, тени и color-scheme в паре`);
}

// Запуск только при прямом вызове.
if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  main();
}
