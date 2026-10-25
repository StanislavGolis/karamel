/**
 * Проверка контрастов одной темы.
 *
 * Общая для гейта 2 (светлая) и гейта 4 (обе). Вынесена в модуль, потому что
 * §14.7 прямо определяет гейт 4 как «расширение §14.5»: это должна быть одна
 * и та же логика, применённая к двум наборам значений. Две копии разошлись бы
 * на первом же добавлении пары.
 */

import {
  BORDER_ROLES,
  ON_ACCENT,
  ON_STATUS_SOLID,
  STATUS_ON_TINT,
  SURFACES,
  TEXT_ROLES,
} from "./contrast.mjs";
import { contrastRatio, parseHex, round2 } from "./tokens.mjs";

/** Минимум для текста по WCAG 2.1 AA. */
export const TEXT_MIN = 4.5;
/** Минимум для нетекстовых элементов (границы, индикаторы). */
export const UI_MIN = 3;

/**
 * Строка отчёта. Печатается как таблица, чтобы в логе CI было видно, что
 * именно изменилось (§14.5: «артефакт таблицы прикладывается к PR»).
 *
 * @typedef {{ pair: string, ratio: number, min: number, ok: boolean }} Row
 */

/**
 * Разрешает значение токена в hex.
 *
 * Возвращает `null`, если токена нет или значение не hex (например
 * `color-mix(...)`). Такие пары не проверяются, но и не считаются пройденными:
 * вызывающая сторона добавит предупреждение, чтобы «не проверено» не выглядело
 * как «проверено и зелено».
 *
 * @param {Map<string,string>} colors
 * @param {string} name
 * @returns {{r:number,g:number,b:number,a:number}|null}
 */
function resolveColor(colors, name) {
  const raw = colors.get(name);
  if (!raw) return null;
  return parseHex(raw);
}

/**
 * Проверяет один набор токенов (одну тему).
 *
 * Проверки ровно по §14.5 и §14.7:
 * 1. каждая текстовая роль ≥4.5:1 к `bg`, `surface`, `surface-raised`;
 * 2. `border-input` / `focus-ring` ≥3:1 к трём фонам — рамку поля видно;
 * 3. текст на `accent`/`accent-hover`/`accent-pressed` ≥4.5:1;
 * 4. текст статуса на СВОЁМ тинте ≥4.5:1 (ревью Н-1);
 * 5. `accent-ink` на сплошных `success`/`warning`/`danger` ≥4.5:1.
 *
 * @param {Map<string,string>} colors
 * @param {string} themeLabel — «светлая» / «тёмная», для сообщений
 * @returns {{ rows: Row[], errors: string[], warnings: string[] }}
 */
export function verifyTheme(colors, themeLabel) {
  /** @type {Row[]} */
  const rows = [];
  const errors = [];
  const warnings = [];

  /**
   * Одна проверка пары.
   * @param {string} fgName
   * @param {string} bgName
   * @param {number} min
   * @param {string} kind
   */
  const check = (fgName, bgName, min, kind) => {
    const fg = resolveColor(colors, fgName);
    const bg = resolveColor(colors, bgName);

    if (!fg || !bg) {
      // Не молчим: пропущенная пара — это не «зелено», а «не проверено».
      warnings.push(
        `${themeLabel}: пара ${fgName} / ${bgName} не проверена (${!fg ? fgName + " отсутствует или не hex" : bgName + " отсутствует или не hex"})`,
      );
      return;
    }

    const ratio = round2(contrastRatio(fg, bg));
    const ok = ratio >= min;
    rows.push({ pair: `${fgName} на ${bgName}`, ratio, min, ok });

    if (!ok) {
      errors.push(
        `${themeLabel}: ${fgName} на ${bgName} = ${ratio}:1 < ${min}:1 (${kind})`,
      );
    }
  };

  // 1. Текст к трём фонам.
  for (const role of TEXT_ROLES) {
    for (const surface of SURFACES) {
      check(role, surface, TEXT_MIN, "текст");
    }
  }

  // 2. Границы к трём фонам.
  for (const role of BORDER_ROLES) {
    for (const surface of SURFACES) {
      check(role, surface, UI_MIN, "граница/фокус");
    }
  }

  // 3. Текст на заливке акцента (включая hover/pressed).
  for (const { fg, bg } of ON_ACCENT) {
    check(fg, bg, TEXT_MIN, "текст на акценте");
  }

  // 4. Текст статуса на своём тинте — провал Б-2 прошёл бы без этой проверки.
  for (const { fg, bg } of STATUS_ON_TINT) {
    check(fg, bg, TEXT_MIN, "статус на тинте");
  }

  // 5. Текст на сплошной статусной заливке.
  for (const { fg, bg } of ON_STATUS_SOLID) {
    check(fg, bg, TEXT_MIN, "текст на статусной заливке");
  }

  return { rows, errors, warnings };
}

/**
 * Печатает таблицу проверенных пар.
 *
 * Формат — как в Приложении А артефакта: «роль на фоне | отношение | норма».
 * Именно он прикладывается к PR, чтобы ревьюер видел дельту, а не только
 * «красный/зелёный».
 *
 * @param {Row[]} rows
 * @param {string} title
 */
export function printTable(rows, title) {
  console.log(`\n${title}`);
  const width = Math.max(...rows.map((r) => r.pair.length), 10);
  for (const row of rows) {
    const mark = row.ok ? "✅" : "❌";
    console.log(
      `  ${mark} ${row.pair.padEnd(width)} ${String(row.ratio).padStart(6)}:1  (≥${row.min})`,
    );
  }
}
