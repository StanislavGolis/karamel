/**
 * Заглушка вместо изображения (Task 9.1; AC-01, AC-03, AC-05).
 *
 * D-3 стори 2.4: заглушка — компонент фронта, а не `placeholder_url`
 * в ответе API. Второй путь к картинке означал бы, что клиент может
 * показать нечто «похожее на фото» там, где сервер изображение не выдал.
 *
 * Состояния из UX §16 (`moderation-check` — четвёртое, добавлено VS 2.4):
 *
 *   * `pending` — фото на проверке; ссылки нет (`url = null`), и это норма,
 *     а не ошибка: `pending` не отдаётся вовсе (AC-03);
 *   * `rejected` — модератор отклонил, причина показывается рядом;
 *   * `moderation-check` — фото проверяется прямо сейчас (загрузка/ответ);
 *   * `no-photo` — фото ещё нет.
 *
 * Высота равна высоте реального фото в галерее: иначе при появлении
 * изображения контент прыгал бы (CLS). Это НЕ модалка и не оверлей —
 * заглушка живёт в потоке сетки фото.
 *
 * Доступность (project-context §10): статус — это цвет + текст + иконка,
 * а не только цвет; иконки декоративные (`aria-hidden`), смысл несёт текст.
 */

export type PhotoPlaceholderState = "pending" | "rejected" | "moderation-check" | "no-photo";

type Props = {
  state: PhotoPlaceholderState;
  /** Причина отказа; показывается только в состоянии `rejected` (AC-05). */
  reason?: string | null;
};

/** Высота плитки фото: совпадает с `<img>` в галерее (без CLS). */
export const PHOTO_TILE_HEIGHT = "h-48";

const CAPTIONS: Record<PhotoPlaceholderState, string> = {
  pending: "На проверке",
  rejected: "Фото отклонено",
  "moderation-check": "Проверяем фото",
  "no-photo": "Фото нет",
};

/**
 * Текст-пояснение под подписью. Разный для каждого состояния: «На проверке»
 * и «Фото нет» требуют разных действий от пользователя, и одинаковый текст
 * заставил бы его гадать, ждать или загружать заново.
 */
const HINTS: Record<PhotoPlaceholderState, string> = {
  pending: "Обычно это занимает несколько минут. Пока фото видят только вы.",
  rejected: "Замените его на другое — остальные фото останутся.",
  "moderation-check": "Это займёт несколько секунд.",
  "no-photo": "Добавьте фото, чтобы анкету чаще открывали.",
};

function StateIcon({ state }: { state: PhotoPlaceholderState }) {
  // `aria-hidden`: смысл дублирован текстом ниже, озвучивать иконку незачем.
  const common = {
    "aria-hidden": true as const,
    className: "h-8 w-8",
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.75,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };

  if (state === "pending" || state === "moderation-check") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7.5V12l3 2" />
      </svg>
    );
  }

  if (state === "rejected") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="9" />
        <path d="M9 9l6 6M15 9l-6 6" />
      </svg>
    );
  }

  return (
    <svg {...common}>
      <rect x="3.5" y="5" width="17" height="14" rx="2.5" />
      <path d="M3.5 16l4.5-4 3.5 3 3-2.5 6 5" />
    </svg>
  );
}

export function PhotoPlaceholder({ state, reason }: Props) {
  // AC-05: причина выводится только для отклонённого фото. Для остальных
  // состояний она либо отсутствует, либо не относится к делу.
  const showReason = state === "rejected" && Boolean(reason);

  return (
    <div
      className={`${PHOTO_TILE_HEIGHT} flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border-input bg-surface px-4 text-center text-ink-secondary`}
      // `role="status"`: скринридер сообщит о смене состояния без перехода
      // фокуса. Для `no-photo` это не нужно — состояние статичное.
      role={state === "no-photo" ? undefined : "status"}
      aria-live={state === "moderation-check" ? "polite" : undefined}
    >
      <StateIcon state={state} />
      <p className="text-base font-medium text-ink">
        {CAPTIONS[state]}
      </p>
      {showReason ? (
        // AC-05: причина — обычный текст, не код ошибки. Перенос строки
        // сохраняется (`whitespace-pre-line`), иначе абзац причины склеится
        // в одну строку и станет нечитаемым.
        <p className="whitespace-pre-line text-sm leading-relaxed">{reason}</p>
      ) : null}
      <p className="text-sm leading-relaxed text-ink-muted">
        {HINTS[state]}
      </p>
    </div>
  );
}
