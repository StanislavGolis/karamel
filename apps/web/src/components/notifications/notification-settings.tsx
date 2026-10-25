"use client";

/**
 * Переключатели канала уведомлений (стори 9.3; AC-01, AC-02; Task 5.2).
 *
 * Клиентский компонент: `PATCH` отправляется из браузера через BFF
 * (`/api/proxy/notifications/settings`), чтобы cookie ставились на той же
 * origin, где открыта страница (D-10 стори 2.3).
 *
 * **Оптимистичное обновление с откатом.** Переключатель меняет состояние
 * сразу — иначе при медленной сети пользователь нажимал бы его дважды,
 * не понимая, сработало ли. При ошибке состояние ВОЗВРАЩАЕТСЯ к прежнему:
 * оставить «включено» после `409`/`422` значило бы показать настройку,
 * которой на сервере нет (следующее открытие экрана показало бы обратное).
 *
 * **Неотключаемые — `disabled` с пояснением** (D-9.3-5). Клиент НЕ выводит
 * их сам: список приходит с сервера (`required`), потому что это продуктовое
 * решение, и его изменение не должно требовать релиза фронта.
 *
 * **Ошибки кладутся рядом с переключателем, а не в тост.** Настройка — форма:
 * пользователь должен видеть, КАКОЙ тип не применился, а тост исчезает и
 * оставляет его в догадках.
 */

import { useState } from "react";

/** Один тип в том объёме, который нужен экрану (поля совпадают с схемой API). */
export type NotificationSettingView = {
  type: string;
  enabled: boolean;
  required: boolean;
};

/**
 * Человеческие подписи типов.
 *
 * Неизвестный тип НЕ теряется: показывается его техническое имя
 * (`NFR60`: клиент игнорирует неизвестное значение, но не скрывает строку —
 * иначе новый тип, добавленный на сервере, не появился бы в UI никогда).
 */
const LABELS: Record<string, string> = {
  "attendance.request": "Подтвердите явку",
  "attendance.result": "Результат явки",
  "card.closed_impersonal": "Карточка закрыта",
  "company.assembled": "Компания собрана",
  "report.created": "Жалоба на вас",
  "report.resolved": "Решение по жалобе",
  "request.received": "Новая заявка на вашу карточку",
  "request.selected": "Вашу заявку выбрали",
  "sanction.issued": "Ограничение доступа",
  "support.replied": "Ответ поддержки",
  "window.expiring": "Окно решения истекает",
};

/** Коды отказов API, переведённые в текст (паттерн `profile-form`). */
const ERRORS: Record<string, string> = {
  notification_type_required: "Этот тип отключить нельзя: он сообщает о решении.",
  invalid_notification_type: "Сервис не знает такого типа уведомлений.",
  unauthenticated: "Сессия истекла. Войдите снова.",
  session_expired: "Сессия истекла. Войдите снова.",
  user_blocked: "Доступ к аккаунту ограничен. Напишите в поддержку.",
  consent_required: "Нужно согласие на обработку персональных данных.",
  upstream_unavailable: "Сервис недоступен. Попробуйте через минуту.",
};

const DEFAULT_ERROR = "Не удалось сохранить настройку. Попробуйте ещё раз.";

/** Состояние одного переключателя, которое реально видит пользователь. */
type ItemState = { enabled: boolean; pending: boolean; error: string | null };

export function NotificationSettings({ initial }: { initial: NotificationSettingView[] }) {
  // Начальное состояние — из серверного ответа. Локальная копия ЗДЕСЬ
  // допустима: это не дублирование серверного состояния ради синхронизации,
  // а состояние формы с ошибками и «в полёте» (project-context §11).
  const [items, setItems] = useState<ItemState[]>(() =>
    initial.map((item) => ({ enabled: item.enabled, pending: false, error: null })),
  );

  // Типы вынесены в отдельный массив: `toggle` обращается к ним по индексу,
  // и чтение `initial[index].type` внутри замыкания захватило бы список,
  // который React может заменить при повторном рендере.
  const types = initial.map((item) => item.type);

  /**
   * Переключает ОДИН тип: оптимистично, с откатом при отказе.
   *
   * `previous` захватывается ДО изменения: `setState` асинхронен, и читать
   * текущее значение в обработчике ошибки было бы гонкой — откат вернул бы
   * не то состояние, что было до нажатия.
   */
  async function toggle(index: number, next: boolean): Promise<void> {
    const previous = items[index]?.enabled ?? false;

    setItems((current) =>
      current.map((item, position) =>
        position === index ? { enabled: next, pending: true, error: null } : item,
      ),
    );

    try {
      const response = await fetch("/api/proxy/notifications/settings", {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        // `credentials: "same-origin"` — BFF живёт на той же origin, и без
        // этого cookie сессии не уехала бы в прокси при кросс-доменной сборке.
        credentials: "same-origin",
        body: JSON.stringify({ type: types[index], enabled: next }),
      });

      if (!response.ok) {
        const payload = (await response.json().catch(() => ({}))) as { code?: string };
        const message = (payload.code ? ERRORS[payload.code] : undefined) ?? DEFAULT_ERROR;
        // Откат: состояние сервера не изменилось, значит и переключатель
        // должен вернуться. Иначе UI утверждал бы настройку, которой нет.
        setItems((current) =>
          current.map((item, position) =>
            position === index
              ? { enabled: previous, pending: false, error: message }
              : item,
          ),
        );
        return;
      }

      // AC-02: сервер подтвердил — берём ЕГО значение, а не наше.
      // Расхождение возможно, если тип стал обязательным между рендером
      // и запросом; верить своему `next` значило бы показать неверное.
      const saved = (await response.json()) as NotificationSettingView;
      setItems((current) =>
        current.map((item, position) =>
          position === index
            ? { enabled: saved.enabled, pending: false, error: null }
            : item,
        ),
      );
    } catch {
      setItems((current) =>
        current.map((item, position) =>
          position === index
            ? { enabled: previous, pending: false, error: DEFAULT_ERROR }
            : item,
        ),
      );
    }
  }

  return (
    <ul className="flex flex-col gap-3">
      {initial.map((item, index) => {
        const state = items[index] ?? { enabled: item.enabled, pending: false, error: null };
        const label = LABELS[item.type] ?? item.type;
        const inputId = `notification-${item.type}`;

        return (
          <li
            key={item.type}
            className="flex flex-col gap-1 border-b border-[#E2D9CB] pb-3 last:border-b-0"
          >
            <div className="flex items-start justify-between gap-3">
              <label htmlFor={inputId} className="flex flex-col gap-1">
                <span className="text-base leading-relaxed">{label}</span>
                {item.required ? (
                  /* D-9.3-5: пояснение рядом с переключателем — пользователь
                     должен понимать, ПОЧЕМУ он недоступен, а не искать причину. */
                  <span className="text-sm text-[#5A5249]">
                    Отключить нельзя: это решение по жалобе или ограничение.
                  </span>
                ) : null}
              </label>
              <button
                id={inputId}
                type="button"
                role="switch"
                aria-checked={state.enabled}
                aria-label={label}
                disabled={item.required || state.pending}
                onClick={() => void toggle(index, !state.enabled)}
                className="relative h-11 w-11 shrink-0 rounded-full border border-[#8A7A66] text-sm transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#7A3E12] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {/* Состояние передаётся ТЕКСТОМ, не только цветом (AC-10 UX). */}
                {state.enabled ? "вкл" : "выкл"}
              </button>
            </div>
            {state.error ? (
              <p role="alert" className="text-sm text-[#A0281C]">
                {state.error}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
