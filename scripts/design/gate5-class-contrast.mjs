#!/usr/bin/env node
/**
 * ГЕЙТ 5 — литеральные цветовые классы в исходниках и контраст их пар.
 *
 * **Зачем он нужен, если есть гейты 1–4.** Гейты 2 и 4 считают контраст
 * ТОКЕНОВ. Это правильная проверка палитры, но она ничего не говорит о том,
 * что реально написано в компоненте: `text-[#1F1B17]` — литерал, он не знает
 * про `[data-theme="dark"]`, и в тёмной теме остаётся тёмно-коричневым на
 * тёмном фоне. Именно так прод-дефект волны 1 прошёл все четыре гейта
 * зелёными: палитра была безупречна, а экран нечитаем.
 *
 * Поэтому гейт 5 смотрит не на палитру, а на ИСХОДНИКИ:
 *   1. литеральный цветовой класс (`bg-[#…]`, `text-[#…]`, `bg-white`,
 *      `text-black`, `text-white`) = нарушение;
 *   2. для пар «фон × текст» внутри одного `className` считается контраст
 *      по WCAG 2.1 и печатается таблица — чтобы нарушение было видно числом,
 *      а не только фактом.
 *
 * **Почему ratchet, а не «ноль сразу».** В проекте остаются legacy-файлы
 * (волны 3–5). Гейт, красный с первого дня на 19 файлах, отключают — ровно
 * то, о чём §14.4: «предупреждение в 51 месте не читают». Поэтому текущее
 * состояние зафиксировано в `legacy-hex-baseline.json` (файл → число), и
 * гейт краснеет, если счётчик ВЫРОС или появился НОВЫЙ файл. Волны уменьшают
 * счётчики и переписывают baseline вниз; вверх — нельзя.
 *
 * `--strict` — режим приёмки волн 3–5 и проверки самого гейта: требует ноль
 * литеральных классов во всех файлах, независимо от baseline.
 * `--write-baseline` — осознанное понижение ratchet'а после волны.
 *
 * Исключения: `__tests__` и `*.test.ts(x)` (тесты вправе фиксировать литерал
 * как ожидаемое значение), `app/globals.css` не сканируется (не тот тип файла).
 * Сгенерированные shadcn-файлы в `components/ui/*` отдельного исключения не
 * требуют: после волны 2 в них нет литеральных цветовых классов, и гейт
 * проверяет их наравне со всеми — иначе «исключение для ui» тихо разрешило бы
 * hex в самом заметном каталоге.
 */

import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { WEB_ROOT, contrastRatio, parseHex, round2 } from "./lib/tokens.mjs";

const SRC = resolve(WEB_ROOT, "src");
const BASELINE_PATH = resolve(import.meta.dirname, "legacy-hex-baseline.json");

/** Минимум WCAG 2.1 AA для обычного текста. */
const TEXT_MIN = 4.5;

/**
 * Литеральный цветовой класс.
 *
 * Произвольное значение (`bg-[#1F1B17]`) и именованный литерал (`bg-white`)
 * ловятся раздельно: у них разные причины появления, и сообщение должно
 * подсказывать правильную замену.
 */
const ARBITRARY = /\b(bg|text|border|outline|ring|fill|stroke|from|via|to)-\[(#[0-9a-fA-F]{3,8})\]/g;
const NAMED = /\b(bg-white|bg-black|text-white|text-black)\b/g;

/** Расширения, которые сканируются. */
const CHECKED_EXT = /\.(tsx?|jsx?)$/;

/**
 * Рекурсивный обход с фильтром каталогов.
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
 * Тесты не сканируются: они вправе утверждать «класс равен литералу».
 * @param {string} file
 */
function isTest(file) {
  return /__tests__|\.test\.tsx?$/.test(file);
}

/**
 * Вырезает комментарии, чтобы «было #7A3E12» в пояснении не считалось цветом.
 * @param {string} content
 */
function stripComments(content) {
  return content.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

/**
 * Находит литеральные цветовые классы в одном файле.
 *
 * @param {string} content
 * @returns {{ line: number, token: string }[]}
 */
export function findLiteralColorClasses(content) {
  const hits = [];
  const lines = stripComments(content).split("\n");

  lines.forEach((line, index) => {
    for (const re of [ARBITRARY, NAMED]) {
      // `lastIndex` сбрасывается созданием нового литерала на каждой строке.
      const scanner = new RegExp(re.source, "g");
      let m;
      while ((m = scanner.exec(line)) !== null) {
        hits.push({ line: index + 1, token: m[0] });
      }
    }
  });

  return hits;
}

/**
 * Собирает пары «фон × текст» внутри ОДНОГО className.
 *
 * Почему в пределах строки, а не файла: классы из разных элементов к одному
 * тексту отношения не имеют, и такой «контраст» был бы выдумкой. Строка —
 * достаточное приближение: className в этом проекте не разносится по строкам
 * (это же требует prettier-конфиг команды).
 *
 * @param {string} content
 * @returns {{ line: number, fg: string, bg: string, ratio: number }[]}
 */
export function findClassPairs(content) {
  const pairs = [];
  const lines = stripComments(content).split("\n");

  lines.forEach((line, index) => {
    const fgs = [];
    const bgs = [];

    const textScanner = /\btext-\[(#[0-9a-fA-F]{3,8})\]/g;
    let m;
    while ((m = textScanner.exec(line)) !== null) fgs.push(m[1]);

    const bgScanner = /\bbg-\[(#[0-9a-fA-F]{3,8})\]/g;
    while ((m = bgScanner.exec(line)) !== null) bgs.push(m[1]);

    // Именованные литералы тоже образуют пару: text-white на bg-[#…] —
    // ровно тот случай, когда белый текст стоит на светлом фоне.
    if (/\btext-white\b/.test(line)) fgs.push("#ffffff");
    if (/\btext-black\b/.test(line)) fgs.push("#000000");
    if (/\bbg-white\b/.test(line)) bgs.push("#ffffff");
    if (/\bbg-black\b/.test(line)) bgs.push("#000000");

    for (const fg of fgs) {
      for (const bg of bgs) {
        const fgRgb = parseHex(fg);
        const bgRgb = parseHex(bg);
        if (!fgRgb || !bgRgb) continue;
        pairs.push({ line: index + 1, fg, bg, ratio: round2(contrastRatio(fgRgb, bgRgb)) });
      }
    }
  });

  return pairs;
}

/**
 * Сканирует `src` и возвращает счётчики и пары.
 * @returns {{ counts: Record<string, number>, details: Record<string, {line:number,token:string}[]>, pairs: Record<string, any[]> }}
 */
function scan() {
  const files = walk(SRC, (dir) => dir.endsWith("__tests__") || dir.endsWith("node_modules"))
    .filter((f) => CHECKED_EXT.test(f) && !isTest(f));

  const counts = {};
  const details = {};
  const pairs = {};

  for (const file of files) {
    const rel = relative(SRC, file).split("\\").join("/");
    const content = readFileSync(file, "utf8");

    const hits = findLiteralColorClasses(content);
    if (hits.length > 0) {
      counts[rel] = hits.length;
      details[rel] = hits;
    }

    const found = findClassPairs(content);
    if (found.length > 0) pairs[rel] = found;
  }

  return { counts, details, pairs };
}

/**
 * Читает baseline. Отсутствие файла — не ошибка: значит долгов ещё нет
 * и действует строгий режим (ноль).
 * @returns {Record<string, number>}
 */
function loadBaseline() {
  try {
    return JSON.parse(readFileSync(BASELINE_PATH, "utf8"));
  } catch {
    return {};
  }
}

function printPairs(pairs) {
  const rows = [];
  for (const [file, list] of Object.entries(pairs)) {
    for (const p of list) {
      rows.push({ file, ...p });
    }
  }
  if (rows.length === 0) return;

  console.log("\nКОНТРАСТ ЛИТЕРАЛЬНЫХ ПАР (фон × текст в одном className)");
  console.log("  файл:строка            текст      фон        контраст  минимум  итог");
  for (const r of rows) {
    const verdict = r.ratio >= TEXT_MIN ? "ok" : "ПРОВАЛ";
    console.log(
      `  ${`${r.file}:${r.line}`.padEnd(22)} ${r.fg.padEnd(10)} ${r.bg.padEnd(10)} ${String(r.ratio).padStart(8)} ${String(TEXT_MIN).padStart(8)}  ${verdict}`,
    );
  }
}

function main() {
  const args = new Set(process.argv.slice(2));
  const strict = args.has("--strict");
  const writeBaseline = args.has("--write-baseline");

  const { counts, details, pairs } = scan();
  const total = Object.values(counts).reduce((a, b) => a + b, 0);

  if (writeBaseline) {
    // Ключи сортируются: diff baseline'а должен показывать ИЗМЕНЕНИЕ счётчика,
    // а не перестановку строк.
    const sorted = Object.fromEntries(Object.entries(counts).sort(([a], [b]) => a.localeCompare(b)));
    writeFileSync(BASELINE_PATH, `${JSON.stringify(sorted, null, 2)}\n`, "utf8");
    console.log(`baseline записан: ${Object.keys(sorted).length} файлов, ${total} литеральных классов`);
    return;
  }

  const baseline = loadBaseline();
  const errors = [];

  for (const [file, count] of Object.entries(counts)) {
    const allowed = baseline[file] ?? 0;
    if (count > allowed) {
      const delta = allowed === 0 ? `${count} новых` : `${count} при разрешённых ${allowed}`;
      errors.push(`${file}: ${delta} литеральных цветовых классов`);
      for (const hit of details[file]) {
        errors.push(`    строка ${hit.line}: «${hit.token}» — замените на токен`);
      }
    }
  }

  // Уменьшение счётчика — не ошибка, но повод понизить baseline: иначе
  // ratchet разрешает вернуть долг обратно, и волна откатывается молча.
  const improvements = [];
  for (const [file, allowed] of Object.entries(baseline)) {
    const current = counts[file] ?? 0;
    if (current < allowed) {
      improvements.push(`${file}: было ${allowed}, стало ${current} — понизьте baseline`);
    }
  }

  printPairs(pairs);

  if (improvements.length > 0) {
    console.log("\n⚠️  Счётчики уменьшились (это хорошо, но baseline устарел):");
    for (const i of improvements) console.log(`  • ${i}`);
    console.log("  Обновить: node scripts/design/gate5-class-contrast.mjs --write-baseline");
  }

  if (strict && total > 0) {
    console.error(`\n❌ ГЕЙТ 5 (--strict): ${total} литеральных цветовых классов в ${Object.keys(counts).length} файлах\n`);
    for (const [file, count] of Object.entries(counts)) {
      console.error(`  • ${file}: ${count}`);
    }
    console.error("\nПравило: цвет только через токен — литерал не знает про тёмную тему.\n");
    process.exit(1);
  }

  if (errors.length > 0) {
    console.error(`\n❌ ГЕЙТ 5 (литеральные цвета): ${errors.length} нарушений\n`);
    for (const e of errors) console.error(`  • ${e}`);
    console.error("\nПравило: цвет только через токен — литерал не знает про тёмную тему.\n");
    process.exit(1);
  }

  const allowed = Object.keys(baseline).length;
  console.log(
    `\n✅ ГЕЙТ 5: новых литеральных цветовых классов нет (${Object.keys(counts).length} файлов в долге из ${allowed} в baseline, ${total} классов)`,
  );
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  main();
}
