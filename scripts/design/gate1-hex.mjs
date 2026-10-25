#!/usr/bin/env node
/**
 * ГЕЙТ 1 (§14.4) — «hex в компоненте = ошибка».
 *
 * Что ловит:
 * 1. `#RRGGBB` / `#RGB` / `#RRGGBBAA` в `src/components/**` и `src/app/**`.
 * 2. Произвольные значения Tailwind: `bg-[#…]`, `text-[#…]`, `border-[#…]`.
 * 3. `rgb(` / `hsl(` в компонентах.
 * 4. Импорт файла, которого нет в манифесте или у которого `passport: 0`
 *    (для статусов, отличных от `legacy`).
 * 5. `@theme inline` в `globals.css` — с ним тёмная тема не работает (Н-3).
 *
 * **Почему в CI, а не в pre-commit.** В `package.json` нет `husky`/`lint-staged`,
 * и заводить эту инфраструктуру ради одного правила не оправдано (ревью Н-14).
 * Локально работает `npm run lint`, в CI — этот скрипт.
 *
 * **Почему исключение для `legacy`.** §14.11 п.1: `legacy` — «ещё не мигрирован,
 * импорт разрешён, hex не будет ошибкой — исключение до волны». Без этого
 * правила гейт краснел бы в день включения на 26 файлах, и его отключили бы —
 * ровно то, о чём §14.4: «предупреждение в 51 месте не читают».
 *
 * Исключение по файлам — только `src/app/globals.css` (единственное место, где
 * hex законен: там объявлены токены).
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { WEB_ROOT, loadTokens } from "./lib/tokens.mjs";

const SRC = resolve(WEB_ROOT, "src");
const COMPONENTS_DIR = join(SRC, "components");
const MANIFEST_PATH = join(COMPONENTS_DIR, "design-system.manifest.json");

/** Единственный файл, где hex разрешён: объявление токенов. */
const CSS_EXCEPTIONS = new Set([resolve(SRC, "app/globals.css")]);

/** Расширения, которые вообще проверяются. */
const CHECKED_EXT = /\.(tsx?|jsx?)$/;

/**
 * Рекурсивный обход с фильтрами по каталогам.
 * @param {string} dir
 * @param {(path: string) => boolean} skipDir
 * @returns {string[]}
 */
function walk(dir, skipDir = () => false) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (skipDir(full)) continue;
      out.push(...walk(full, skipDir));
    } else {
      out.push(full);
    }
  }
  return out;
}

/**
 * Находит нарушения «цвет вне токена» в одном файле.
 *
 * `color:var(--accent)` НЕ является нарушением: это ссылка на токен, а не
 * литерал. Проверяется только литеральный цвет. Поэтому `\[color:var(...)\]`
 * не попадает под шаблон `bg-\[#…\]` — там нет `#`.
 *
 * @param {string} content
 * @returns {string[]} — список человекочитаемых описаний
 */
export function findColorViolations(content) {
  const violations = [];

  // Комментарии вырезаются: пояснение «было #7A3E12» — не использование цвета.
  const stripped = content
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");

  const lines = stripped.split("\n");

  lines.forEach((line, index) => {
    const lineNo = index + 1;

    // Произвольные значения Tailwind — проверяются первыми: их надо назвать
    // «произвольным классом», а не просто hex, иначе сообщение не помогает.
    const arbitrary = /\b(bg|text|border|outline|ring|fill|stroke|from|via|to)-\[#?[^\]]*\]/g;
    let m;
    while ((m = arbitrary.exec(line)) !== null) {
      // `-[color:var(--accent)]` — законная ссылка на токен, не литерал.
      if (!/#|rgb\(|hsl\(/.test(m[0])) continue;
      violations.push(`строка ${lineNo}: произвольный цвет «${m[0]}» — используйте токен`);
    }

    if (/\b(rgb|hsl)a?\(/.test(line)) {
      violations.push(`строка ${lineNo}: rgb()/hsl() — используйте токен`);
    }

    // Голый hex в любом месте строки (стили, атрибуты, литералы).
    const hex = /#([0-9a-fA-F]{3,8})\b/g;
    while ((m = hex.exec(line)) !== null) {
      const raw = m[1];
      // Отсекаем не-цвета: `#fragment`, `#123456` ок, `#zzz` не совпадёт.
      if (![3, 4, 6, 8].includes(raw.length)) continue;
      violations.push(`строка ${lineNo}: hex «#${raw}» — используйте токен`);
    }
  });

  return violations;
}

/**
 * Читает манифест и строит карту «абсолютный путь → запись компонента».
 * @returns {{ byPath: Map<string, any>, raw: any }}
 */
export function loadManifest() {
  const raw = JSON.parse(readFileSync(MANIFEST_PATH, "utf8"));
  const byPath = new Map();
  for (const [name, entry] of Object.entries(raw.components ?? {})) {
    const abs = resolve(COMPONENTS_DIR, entry.path);
    byPath.set(abs, { name, ...entry });
  }
  return { byPath, raw };
}

/**
 * Проверяет, что каждый файл манифеста существует и что каждый компонентный
 * файл в манифесте присутствует.
 *
 * Обратная проверка обязательна: без неё новый компонент можно создать и
 * импортировать, не добавив в манифест, и правило §14.2 п.9 станет фикцией.
 */
function checkManifestCompleteness(byPath) {
  const errors = [];

  for (const [abs, entry] of byPath) {
    try {
      statSync(abs);
    } catch {
      errors.push(`манифест: ${entry.name} → «${entry.path}» не существует`);
    }
  }

  const onDisk = walk(COMPONENTS_DIR, (dir) => dir.endsWith("__tests__"))
    .filter((f) => /\.tsx$/.test(f))
    .map((f) => resolve(f));

  for (const abs of onDisk) {
    if (!byPath.has(abs)) {
      errors.push(
        `манифест: файл «${relative(COMPONENTS_DIR, abs)}» не объявлен в design-system.manifest.json`,
      );
    }
  }

  // passport: 0 допустим только для legacy — иначе импорт такого компонента
  // разрешён, хотя паспорта (§6/§7/§12) у него нет (§14.2 п.9).
  for (const [abs, entry] of byPath) {
    if (entry.status !== "legacy" && (entry.passport ?? 0) === 0) {
      errors.push(
        `манифест: ${entry.name} имеет status=${entry.status} и passport=0 — паспорт обязателен`,
      );
    }
  }

  return errors;
}

function main() {
  const errors = [];
  const { byPath } = loadManifest();

  // --- Н-3: `@theme inline` ломает тёмную тему без ошибки сборки ---
  const tokens = loadTokens();
  if (tokens.themeIsInline) {
    errors.push(
      "globals.css: `@theme inline` — inline встраивает значение в утилиту, и [data-theme=\"dark\"] не переопределит фон (Н-3)",
    );
  }

  // --- манифест: полнота и паспорта ---
  errors.push(...checkManifestCompleteness(byPath));

  // --- hex и произвольные цвета ---
  const targets = [
    ...walk(COMPONENTS_DIR, (dir) => dir.endsWith("__tests__")),
    ...walk(join(SRC, "app")),
  ].filter((f) => CHECKED_EXT.test(f) && !CSS_EXCEPTIONS.has(resolve(f)));

  let scanned = 0;
  for (const file of targets) {
    const rel = relative(WEB_ROOT, file);
    const content = readFileSync(file, "utf8");
    const violations = findColorViolations(content);
    scanned += 1;

    if (violations.length === 0) continue;

    // Статус берётся из манифеста. Файлы в `src/app/**` в манифест не входят:
    // страницы мигрируются волной 5, до неё они считаются legacy.
    const entry = byPath.get(resolve(file));
    const status = entry?.status ?? "legacy";
    const isTest = /__tests__|\.test\.tsx?$/.test(file);

    if (status === "legacy" || isTest) {
      continue;
    }

    for (const v of violations) errors.push(`${rel}: ${v}`);
  }

  if (errors.length > 0) {
    console.error(`\n❌ ГЕЙТ 1 (hex/токены): ${errors.length} нарушений\n`);
    for (const e of errors) console.error(`  • ${e}`);
    console.error("\nПравило: цвет только через токен (§2.4). Значения — globals.css.\n");
    process.exit(1);
  }

  console.log(`✅ ГЕЙТ 1: ${scanned} файлов проверено, hex/произвольных цветов нет`);
}

// Запуск только при прямом вызове: модуль импортируется тестами.
if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  main();
}
