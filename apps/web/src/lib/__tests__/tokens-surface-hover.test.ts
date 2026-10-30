/**
 * Токены `surface-hover` и `disabled` тёмной темы (стори 11.9; AC-1, AC-5).
 *
 * **Почему контраст считается, а не сравнивается с литералом.** Тест
 * `expect(css).toContain("#46403A")` прошёл бы при любом значении в файле и
 * ничего не говорил бы о видимости hover. Здесь значение извлекается и
 * считается по WCAG 2.1 — подмена токена на невидимый (например, на `#2A2823`
 * из первой редакции AC, дающий 1.14:1) роняет тест.
 *
 * **Почему читается файл, а не CSSOM.** jsdom не вычисляет `@theme`-переменные
 * и `[data-theme="dark"]`, поэтому единственный честный источник значений —
 * сам `globals.css`. Гейты 2–4 делают то же самое средствами node.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const CSS_PATH = join(import.meta.dirname, "../../app/globals.css");
const CSS = readFileSync(CSS_PATH, "utf8");

/**
 * Достаёт тело CSS-блока по селектору.
 *
 * Между селектором и `{` допускаются только пробелы и переводы строк: иначе
 * `[data-theme="dark"]` нашёлся бы внутри `@custom-variant dark (...)` — там
 * нет тела, и наивный поиск вернул бы светлую палитру под видом тёмной
 * (ровно этот дефект был в гейте 4, §14.7).
 */
function extractBlock(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`${escaped}\\s*\\{`);
  const match = re.exec(css);
  if (match === null) throw new Error(`блок ${selector} не найден`);
  let depth = 0;
  const start = match.index + match[0].length - 1;
  for (let i = start; i < css.length; i += 1) {
    if (css[i] === "{") depth += 1;
    else if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) return css.slice(start + 1, i);
    }
  }
  throw new Error(`блок ${selector} не закрыт`);
}

/** Значение токена внутри блока; падает, если токена нет. */
function token(block: string, name: string): string {
  const re = new RegExp(`${name}\\s*:\\s*(#[0-9A-Fa-f]{6})`);
  const match = re.exec(block);
  if (match === null) throw new Error(`токен ${name} не задан hex-значением`);
  return match[1].toUpperCase();
}

/** sRGB-канал в линейное пространство (WCAG 2.1). */
function linear(channel: number): number {
  const c = channel / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** Относительная яркость hex-цвета. */
function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(linear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Коэффициент контраста двух цветов, ≥ 1. */
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const LIGHT = extractBlock(CSS, "@theme");
const DARK = extractBlock(CSS, "[data-theme=\"dark\"]");

/** Минимум, при котором наведение различимо (AC-1). */
const HOVER_MIN = 1.5;

describe("surface-hover: наведение видно в обеих темах", () => {
  it.each([
    ["светлая", LIGHT],
    ["тёмная", DARK],
  ])("AC-1: %s тема — контраст к surface ≥ 1.5", (_theme, block) => {
    const hover = token(block, "--color-surface-hover");
    const surface = token(block, "--color-surface");
    const ratio = contrast(hover, surface);
    // Краснеет при возврате значений первой редакции AC: #2A2823 к surface
    // даёт 1.14, #EDE8DF — 1.01, то есть hover невидим.
    expect(ratio).toBeGreaterThanOrEqual(HOVER_MIN);
  });

  it.each([
    ["светлая", LIGHT],
    ["тёмная", DARK],
  ])("AC-1: %s тема — контраст к --color-bg ≥ 1.5", (_theme, block) => {
    const hover = token(block, "--color-surface-hover");
    const bg = token(block, "--color-bg");
    // Кнопки `secondary`/`quiet` лежат на фоне страницы, а не на surface:
    // проверка только к surface оставила бы половину мест невидимой.
    expect(contrast(hover, bg)).toBeGreaterThanOrEqual(HOVER_MIN);
  });

  it.each([
    ["светлая", "#191B1F", LIGHT],
    ["тёмная", "#F2EFE9", DARK],
  ] as const)("AC-1: %s тема — ink на surface-hover читается (≥ 4.5)", (_theme, ink, block) => {
    const hover = token(block, "--color-surface-hover");
    // Подъём контраста фона съедает контраст текста: без этой проверки можно
    // «починить» hover и сделать подписи кнопок нечитаемыми.
    expect(contrast(ink, hover)).toBeGreaterThanOrEqual(4.5);
  });
});

describe("disabled тёмной темы", () => {
  it("AC-5: фон #6B6862, текст #2A2823", () => {
    expect(token(DARK, "--color-disabled-bg")).toBe("#6B6862");
    expect(token(DARK, "--color-disabled-ink")).toBe("#2A2823");
  });

  it("AC-5: disabled отличим от активной кнопки, но текст ниже AA осознанно", () => {
    const bgDisabled = token(DARK, "--color-disabled-bg");
    const inkDisabled = token(DARK, "--color-disabled-ink");
    const surface = token(DARK, "--color-surface");
    // Подложка различима: иначе неактивная кнопка «сливается» с активной.
    expect(contrast(bgDisabled, surface)).toBeGreaterThanOrEqual(3);
    // Пара текст/фон — намеренно ниже 4.5 (WCAG 1.4.3 исключает неактивные).
    // Верхняя граница важна не меньше нижней: 5.58:1 (surface+ink-muted)
    // означало, что disabled выглядит как обычный текст.
    expect(contrast(inkDisabled, bgDisabled)).toBeLessThan(4.5);
  });

  it("AC-5: светлая тема не тронута", () => {
    expect(token(LIGHT, "--color-disabled-bg")).toBe("#DFDBD2");
    expect(token(LIGHT, "--color-disabled-ink")).toBe("#9AA0A6");
  });
});

describe("CR 11.9 H-1: `ink-muted` на hover-поверхности запрещён", () => {
  it("нет класса `text-ink-muted` рядом с `hover:bg-surface-hover`", () => {
    // Замер З-1: `ink-muted` на hover даёт 3.09 (светлая) и 3.39 (тёмная) — ниже
    // AA 4.5. Dev Notes 11.9 объявляли этот запрет гардом, но гарда не было:
    // мутация CR «text-ink-secondary → text-ink-muted в quiet-варианте Button»
    // проходила ВСЕ тесты (43 passed). Запрет жил только в комментарии.
    //
    // Ограничение: проверяются строки целиком, поэтому многострочный
    // `className` с переносом между `text-ink-muted` и `hover:bg-surface-hover`
    // не ловится. В текущем коде таких нет (замер CR); при появлении —
    // расширять до разбора className-блоков.
    const srcRoot = join(import.meta.dirname, "../..");
    const offenders: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name === "__tests__" || entry.name === "node_modules") continue;
          walk(full);
          continue;
        }
        if (!/\.tsx?$/.test(entry.name) || !statSync(full).isFile()) continue;
        const lines = readFileSync(full, "utf8").split("\n");
        lines.forEach((line, index) => {
          if (!line.includes("hover:bg-surface-hover")) return;
          if (!line.includes("text-ink-muted")) return;
          offenders.push(`${full}:${index + 1}`);
        });
      }
    };
    walk(srcRoot);
    expect(offenders).toEqual([]);
  });
});

describe("AC-2: hover больше не использует surface", () => {
  it("в компонентах нет `hover:bg-surface` без суффикса", () => {
    // Гейт 5 ловит литеральные цвета, но не «не тот токен»: `hover:bg-surface`
    // — валидный класс, который просто невидим. Проверка нужна отдельная.
    const srcRoot = join(import.meta.dirname, "../..");
    const offenders: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name === "__tests__" || entry.name === "node_modules") continue;
          walk(full);
          continue;
        }
        if (!/\.tsx?$/.test(entry.name) || !statSync(full).isFile()) continue;
        const content = readFileSync(full, "utf8");
        // `hover:bg-surface-hover` и `hover:bg-surface-raised` — законны.
        if (/hover:bg-surface(?![-\w])/.test(content)) offenders.push(full);
      }
    };
    walk(srcRoot);
    expect(offenders).toEqual([]);
  });
});
