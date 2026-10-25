"use client";

/**
 * Обратный отсчёт окна решения (UX §832, `CountdownBadge`; стори 4.5, Task 7.1;
 * AC-09, NFR14, NFR57).
 *
 * Источник времени — **серверный `expires_at`**, не локальный таймер (NFR14):
 * часы устройства могут врать на минуты, и «осталось 0 минут» у одного
 * участника и «осталось 12 минут» у другого разрушали бы доверие к продукту.
 * Обновление раз в минуту; секунды не показываются — они создают ложную
 * точность и заставляют смотреть на таймер вместо встречи.
 *
 * **Доступность — критичная часть, а не украшение (UX §832):**
 *
 * - визуальный текст обёрнут в `aria-hidden="true"` — иначе скринридер
 *   прочитает «до сб, 13:00, осталось 5 ч 40 мин» вместе с тиканьем;
 * - рядом живёт `role="timer"` с `aria-live="off"` и СТАТИЧНЫМ текстом
 *   «Окно решения закрывается в субботу в 13:00» — его читают по запросу;
 * - живой регион для отсчёта **запрещён**: он бы бубнил каждую минуту
 *   (UX прямо это запрещает). Объявление одно — при переходе через час
 *   и при закрытии окна;
 * - при `prefers-reduced-motion` полоса не анимируется.
 */

import { useEffect, useState } from "react";

const WEEKDAYS = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"] as const;
const WEEKDAYS_FULL = [
  "воскресенье",
  "понедельник",
  "вторник",
  "среду",
  "четверг",
  "пятницу",
  "субботу",
] as const;

/** Сколько миллисекунд осталось до момента; отрицательное — уже прошло. */
function remainingMs(expiresAt: string, now: number): number {
  const target = Date.parse(expiresAt);
  if (Number.isNaN(target)) return 0;
  return target - now;
}

/**
 * «5 ч 40 мин» / «40 мин» / «меньше минуты».
 *
 * Часы показываются только когда они есть: «0 ч 40 мин» — шум, из-за которого
 * человек читает цифру «0» как «уже закрыто».
 */
export function formatRemaining(ms: number): string {
  if (ms <= 0) return "Окно закрыто";
  const totalMinutes = Math.floor(ms / 60_000);
  if (totalMinutes < 1) return "меньше минуты";
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours === 0) return `${minutes} мин`;
  if (minutes === 0) return `${hours} ч`;
  return `${hours} ч ${minutes} мин`;
}

/** «до сб, 13:00» — короткая форма для карточки. */
export function formatShortDeadline(expiresAt: string): string {
  const date = new Date(expiresAt);
  if (Number.isNaN(date.getTime())) return "";
  const day = WEEKDAYS[date.getDay()];
  const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  return `до ${day}, ${time}`;
}

/** «в субботу в 13:00» — статичный текст для скринридера. */
export function formatAccessibleDeadline(expiresAt: string): string {
  const date = new Date(expiresAt);
  if (Number.isNaN(date.getTime())) return "Окно решения закрыто";
  const day = WEEKDAYS_FULL[date.getDay()];
  const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  return `Окно решения закрывается в ${day} в ${time}`;
}

type CountdownBadgeProps = {
  /** Серверный `expires_at` (ISO 8601). */
  expiresAt: string;
  /**
   * Серверное «сейчас» (ISO 8601) — якорь отсчёта (AC-04 стори 5б.1, NFR14).
   *
   * Без него остаток считался бы по часам устройства, и участник с часами,
   * убежавшими на пять минут, видел бы «окно закрыто» там, где решение ещё
   * принимается. Один раз при монтировании вычисляется смещение
   * `serverTime - Date.now()`, дальше тикает локальный таймер: сеть на каждом
   * тике не нужна, а расхождение остаётся в пределах дрейфа часов за сессию.
   */
  serverTime?: string;
  /** Начальное «сейчас» для детерминированного рендера и тестов. */
  initialNow?: number;
};

export function CountdownBadge({ expiresAt, serverTime, initialNow }: CountdownBadgeProps) {
  // Смещение «сервер минус устройство». `NaN`/отсутствие якоря — работаем по
  // локальным часам, как до 5б.1: карточка без `server_time` (старый ответ из
  // кеша) не должна ломать рендер.
  const [clockOffset] = useState<number>(() => {
    if (serverTime === undefined) return 0;
    const parsed = Date.parse(serverTime);
    return Number.isNaN(parsed) ? 0 : parsed - Date.now();
  });
  const [now, setNow] = useState<number>(() => (initialNow ?? Date.now()) + clockOffset);

  // Обновление раз в минуту, не чаще: секунды не показываются (UX §832).
  // `setState` здесь живёт внутри колбэка интервала, а не в теле эффекта:
  // синхронный `setState` в теле вызвал бы каскадный рендер на каждом
  // монтировании (правило `react-hooks/set-state-in-effect`).
  useEffect(() => {
    // NFR14: расхождение показанного остатка с сервером ≤1 с — тик раз в
    // секунду, а не раз в минуту. Отображение по-прежнему в минутах
    // (`formatRemaining`), секунды не показываются: ложная точность.
    const timer = window.setInterval(() => setNow(Date.now() + clockOffset), 1_000);
    return () => window.clearInterval(timer);
  }, [clockOffset]);

  const ms = remainingMs(expiresAt, now);
  const closed = ms <= 0;
  const underHour = !closed && ms <= 3_600_000;

  // Объявление ОДНО на переход и вычисляется при рендере, без состояния:
  // живой регион для самого отсчёта запрещён (он бы бубнил каждую минуту).
  // Пока окно открыто и до часа — тишина; на пороге часа — предупреждение;
  // после закрытия — сообщение о закрытии.
  const announcement = closed
    ? "Окно решения закрыто"
    : underHour
      ? "Осталось меньше часа на решение"
      : "";

  if (closed) {
    return (
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-ink-secondary">Окно закрыто. Компания собрана.</p>
        <span role="status" className="sr-only">
          {announcement}
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <p aria-hidden="true" className="text-sm text-ink-secondary">
        {formatShortDeadline(expiresAt)} · осталось {formatRemaining(ms)}
      </p>
      <span role="timer" aria-live="off" className="sr-only">
        {formatAccessibleDeadline(expiresAt)}
      </span>
      {underHour ? (
        <p aria-hidden="true" className="text-sm font-semibold text-warning">
          меньше часа на решение
        </p>
      ) : null}
      {announcement ? (
        <span role="status" className="sr-only">
          {announcement}
        </span>
      ) : null}
    </div>
  );
}
