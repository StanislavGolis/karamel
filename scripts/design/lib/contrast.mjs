/**
 * Матрица пар контраста для гейтов 2 и 4.
 *
 * Почему отдельный модуль: гейт 2 (светлая) и гейт 4 (обе темы) обязаны
 * проверять ОДИН И ТОТ ЖЕ набор пар. Две копии списка разошлись бы на первом
 * добавлении токена, и «гейт 4 — расширение гейта 2» стало бы неправдой (§14.7).
 *
 * Источник пар — §2.5, §14.5, §17.2 артефакта. Минимальные значения по спеке:
 * текст >= 4.5:1 к трём фонам, границы >= 3:1, текст на accent >= 4.5:1,
 * текст статуса на СВОЁМ тинте >= 4.5:1 (ревью Н-1 — без этой проверки провал
 * Б-2 прошёл бы).
 */

/** Фоны, к которым проверяется каждый текстовый токен (§2.5). */
export const SURFACES = ["--color-bg", "--color-surface", "--color-surface-raised"];

/** Текстовые роли. `accent` входит: в направлении D акцент — это чернила. */
export const TEXT_ROLES = [
  "--color-ink",
  "--color-ink-secondary",
  "--color-ink-muted",
  "--color-accent",
  "--color-success",
  "--color-warning",
  "--color-danger",
];

/** Границы: норма 3:1 (не 4.5 — это не текст). */
export const BORDER_ROLES = ["--color-border-input", "--color-focus-ring"];

/**
 * Текст на заливке акцента.
 *
 * Проверяется и `accent`, и `accent-hover`/`accent-pressed`: кнопка меняет фон
 * при наведении и нажатии, и текст обязан оставаться читаемым во всех трёх
 * состояниях (§17.2).
 */
export const ON_ACCENT = [
  { fg: "--color-accent-ink", bg: "--color-accent" },
  { fg: "--color-accent-ink", bg: "--color-accent-hover" },
  { fg: "--color-accent-ink", bg: "--color-accent-pressed" },
];

/**
 * Текст статуса на СВОЁМ тинте (ревью Н-1).
 *
 * Без этой пары можно было объявить `danger-tint`, не проверить его и
 * получить нечитаемый бейдж ошибки — ровно провал Б-2.
 */
export const STATUS_ON_TINT = [
  { fg: "--color-success", bg: "--color-success-tint" },
  { fg: "--color-warning", bg: "--color-warning-tint" },
  { fg: "--color-danger", bg: "--color-danger-tint" },
];

/**
 * Текст на статусной заливке как на фоне.
 *
 * §14.5: «текст на статусной заливке — `accent-ink` на `danger` / `success` /
 * `warning` < 4.5:1». В продукте success/warning/danger не используются как
 * фон блока (§2.4: только тинт под текстом), но кнопка опасного действия в
 * кабинете модератора — исключение, и пара должна быть посчитана.
 */
export const ON_STATUS_SOLID = [
  { fg: "--color-accent-ink", bg: "--color-success" },
  { fg: "--color-accent-ink", bg: "--color-warning" },
  { fg: "--color-accent-ink", bg: "--color-danger" },
];
