/**
 * Общий парсер `globals.css` для всех гейтов дизайн-системы.
 *
 * Почему один модуль, а не четыре копии парсинга: гейты 2, 3 и 4 читают одни
 * и те же токены. Четыре независимых регулярки разошлись бы на первом же
 * изменении формата файла, и «гейт зелёный» перестал бы что-либо означать —
 * ровно тот класс дефекта, который проектная ретроспектива эпика 9 называет
 * «заявление без доказательства».
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../apps/web");

export const GLOBALS_CSS_PATH = resolve(WEB_ROOT, "src/app/globals.css");

/**
 * Извлекает тело CSS-блока по селектору.
 *
 * **Почему не `indexOf(selector)` + первый `{`.** Наивная версия ломается на
 * первом же реальном файле: `[data-theme="dark"]` встречается ВЫШЕ своего
 * блока — в `@custom-variant dark (&:where([data-theme="dark"], ...))`, у
 * которого нет тела. Наивный поиск брал `{` от следующего правила (`@theme`)
 * и возвращал СВЕТЛУЮ палитру под видом тёмной. Гейт 4 при этом «проходил»:
 * он дважды проверял светлую тему. Это дефект класса «тест-дублёр тестирует
 * себя» — зелёный CI без проверки.
 *
 * Поэтому между концом селектора и `{` допускаются только пробелы и переводы
 * строк. Вхождение внутри другого правила или внутри списка параметров
 * функции (`:where(...)`) отбрасывается: после него идёт `,`, `)` или `*`.
 *
 * Берётся первое ПОДХОДЯЩЕЕ вхождение. Если подходящих нет — блока нет.
 *
 * @param {string} css
 * @param {string} selector — например `@theme` или `[data-theme="dark"]`
 * @returns {string|null}
 */
export function extractBlock(css, selector) {
  let from = 0;

  while (from <= css.length) {
    const index = css.indexOf(selector, from);
    if (index === -1) return null;

    const afterSelector = index + selector.length;
    const rest = css.slice(afterSelector);

    // Между селектором и `{` — только пробельные символы.
    const match = /^\s*\{/.exec(rest);
    if (!match) {
      from = afterSelector;
      continue;
    }

    const open = afterSelector + match[0].length - 1;

    let depth = 0;
    for (let i = open; i < css.length; i += 1) {
      const ch = css[i];
      if (ch === "{") depth += 1;
      else if (ch === "}") {
        depth -= 1;
        if (depth === 0) return css.slice(open + 1, i);
      }
    }
    return null;
  }

  return null;
}

/**
 * Собирает CSS-переменные из тела блока в Map.
 *
 * Значение нормализуется (trim + схлопывание пробелов): многострочные записи
 * `--shadow-overlay: 0 8px 24px\n    rgba(...)` иначе сравнивались бы как
 * разные строки при одинаковом смысле.
 *
 * @param {string} block
 * @returns {Map<string, string>}
 */
export function parseVars(block) {
  const vars = new Map();
  // `[^;{}]+` — значение не содержит `;` и не переходит в следующий блок.
  // Комментарии уже удалены вызывающей стороной.
  const re = /(--[a-zA-Z0-9-_]+)\s*:\s*([^;{}]+);/g;
  let m;
  while ((m = re.exec(block)) !== null) {
    vars.set(m[1], m[2].trim().replace(/\s+/g, " "));
  }
  return vars;
}

/** Удаляет CSS-комментарии: `@theme` содержит пояснения с `--color-*` внутри. */
export function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/**
 * Читает обе палитры из `globals.css`.
 *
 * @returns {{
 *   css: string,
 *   light: Map<string, string>,
 *   dark: Map<string, string>|null,
 *   lightColors: Map<string, string>,
 *   darkColors: Map<string, string>|null,
 *   lightShadows: Map<string, string>,
 *   darkShadows: Map<string, string>|null,
 *   darkBlockRaw: string|null,
 *   themeIsInline: boolean,
 * }}
 */
export function loadTokens(cssPath = GLOBALS_CSS_PATH) {
  const raw = readFileSync(cssPath, "utf8");
  const css = stripComments(raw);

  // Гейт 1 требует снять `inline`: иначе [data-theme="dark"] не переопределит
  // значения, и тёмная тема не заработает без единой ошибки сборки (Н-3).
  const themeIsInline = /@theme\s+inline\s*\{/.test(css);

  // `@theme inline` — тоже совпадение по `@theme`: блок извлекается в обоих
  // случаях, иначе гейт 3 не смог бы сравнить множества.
  const lightBlock = extractBlock(css, "@theme") ?? "";
  const darkBlock = extractBlock(css, '[data-theme="dark"]');

  const light = parseVars(lightBlock);
  const dark = darkBlock ? parseVars(darkBlock) : null;

  const filterPrefix = (map, prefix) => {
    const out = new Map();
    if (!map) return out;
    for (const [k, v] of map) if (k.startsWith(prefix)) out.set(k, v);
    return out;
  };

  return {
    css,
    light,
    dark,
    lightColors: filterPrefix(light, "--color-"),
    darkColors: dark ? filterPrefix(dark, "--color-") : null,
    lightShadows: filterPrefix(light, "--shadow-"),
    darkShadows: dark ? filterPrefix(dark, "--shadow-") : null,
    darkBlockRaw: darkBlock,
    themeIsInline,
  };
}

/**
 * Разбирает hex-цвет (`#RGB`, `#RRGGBB`, `#RRGGBBAA`) в `{r,g,b,a}` 0..255.
 * Возвращает `null`, если значение не hex (например `color-mix(...)`).
 */
export function parseHex(value) {
  const m = /^#([0-9a-fA-F]{3,8})$/.exec(value.trim());
  if (!m) return null;
  let hex = m[1];
  if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
  if (hex.length !== 6 && hex.length !== 8) return null;
  return {
    r: parseInt(hex.slice(0, 2), 16),
    g: parseInt(hex.slice(2, 4), 16),
    b: parseInt(hex.slice(4, 6), 16),
    a: hex.length === 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 1,
  };
}

/**
 * Относительная яркость по WCAG 2.1 §Relative luminance.
 * @param {{r:number,g:number,b:number}} rgb
 */
export function relativeLuminance({ r, g, b }) {
  const channel = (v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/**
 * Контраст двух цветов по WCAG 2.1. Возвращает отношение ≥1.
 * Порядок аргументов не важен: формула всегда делит светлое на тёмное.
 */
export function contrastRatio(fg, bg) {
  const l1 = relativeLuminance(fg);
  const l2 = relativeLuminance(bg);
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

/** Округление до двух знаков для отчёта: 4.78, а не 4.77654. */
export function round2(n) {
  return Math.round(n * 100) / 100;
}
