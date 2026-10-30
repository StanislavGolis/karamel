"use client";

/**
 * Срок окна решения (UX §973-1000, `DeadlineNotice`; стори 12.5, AC-3, AC-4, З-9).
 *
 * **Переименован из `CountdownBadge` 2026-10-28.** Обратный отсчёт убран как
 * запрещённый З-9: компонент показывает **срок**, а не оставшееся время.
 *
 * Что запрещено и почему:
 *
 * - `useEffect` + `setInterval` — тикающий счётчик создаёт ложную точность и
 *   заставляет смотреть на таймер вместо встречи (UX §575);
 * - `role="timer"` — объявляет тикающий элемент, которого здесь нет
 *   (UX §1224: «`role="timer"` отсутствует»);
 * - остаток («осталось 5 ч 40 мин») — прямое нарушение З-9.
 *
 * Источник времени — серверный `expires_at`, якорь — серверный `server_time`
 * (NFR14): часы устройства могут врать, и «окно закрыто» у одного участника
 * при открытом окне у другого разрушало бы доверие. Оба значения статичны:
 * меняются только новым ответом API.
 *
 * **Доступность (UX §973-1000):**
 *
 * - срок читается обычным текстом, без `aria-hidden`: это статичная
 *   информация, скрывать её от скринридера незачем;
 * - плашка за час — `role="status"`, объявляется ОДИН раз при появлении;
 * - при `prefers-reduced-motion` анимаций нет, потому что нет полосы и нет
 *   обновлений.
 */

const WEEKDAYS = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"] as const;

/** Час в миллисекундах — порог плашки «скоро закроется». */
const NOTICE_THRESHOLD_MS = 3_600_000;

/** «до сб, 13:00» — короткая форма для карточки. */
export function formatShortDeadline(expiresAt: string): string {
  const date = new Date(expiresAt);
  if (Number.isNaN(date.getTime())) return "";
  const day = WEEKDAYS[date.getDay()];
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `до ${day}, ${hours}:${minutes}`;
}

/** «13:00» — время закрытия для плашки. */
export function formatDeadlineTime(expiresAt: string): string {
  const date = new Date(expiresAt);
  if (Number.isNaN(date.getTime())) return "";
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}

type DeadlineNoticeProps = {
  /** Серверный `expires_at` (ISO 8601) — срок, а не остаток. */
  expiresAt: string;
  /**
   * Серверное «сейчас» (ISO 8601) — якорь для порога «меньше часа» (NFR14).
   *
   * Без него порог считался бы по часам устройства. Необязателен: карточка
   * без `server_time` (старый ответ из кеша) не должна ломать рендер — тогда
   * плашка просто не показывается, а срок показывается всегда.
   */
  serverTime?: string;
};

export function DeadlineNotice({ expiresAt, serverTime }: DeadlineNoticeProps) {
  // Оба значения — статичный рендер. Ни `useEffect`, ни `setInterval`:
  // тикающего элемента в компоненте нет и быть не должно (З-9).
  const deadline = new Date(expiresAt);
  const closed = Number.isNaN(deadline.getTime()) ? false : deadline.getTime() <= Date.parse(serverTime ?? "");

  if (closed) {
    // UX §973-1000: после истечения — «Окно закрыто. Компания собрана.»,
    // без слова «просрочено» (оно обвиняет человека, а не описывает факт).
    return (
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-ink-secondary">Окно закрыто. Компания собрана.</p>
      </div>
    );
  }

  const short = formatShortDeadline(expiresAt);
  if (!short) return null;

  // Порог «меньше часа» — один расчёт при рендере, не интервал.
  const anchor = Date.parse(serverTime ?? "");
  const leftMs = Number.isNaN(anchor) ? Number.NaN : deadline.getTime() - anchor;
  const underHour = Number.isFinite(leftMs) && leftMs > 0 && leftMs <= NOTICE_THRESHOLD_MS;
  const closeTime = formatDeadlineTime(expiresAt);

  return (
    <div className="flex flex-col gap-1">
      {/* UX §973-1000: срок — обычный текст, `aria-hidden` нет. */}
      <p className="text-sm text-ink-secondary">{short}</p>
      {underHour ? (
        // Одна статичная плашка, `role="status"`: объявляется один раз
        // при появлении. Не `role="timer"` — тикающего элемента нет.
        <p role="status" className="text-sm font-semibold text-warning">
          Окно закроется в {closeTime}
        </p>
      ) : null}
    </div>
  );
}
