"use client";

/**
 * Форма обращения в поддержку (стори 9.5; AC-01, AC-04; Task 6.2).
 *
 * **Ввод НЕ теряется при ошибке** (UX 955–965). Самая дорогая ошибка формы —
 * стереть длинный текст из-за `429` или обрыва сети. Поэтому `body` живёт в
 * состоянии и не сбрасывается ни на одном отказе: очищается только после
 * `201`, когда сервер подтвердил, что текст сохранён.
 *
 * **Кнопка недоступна при пустом НОРМАЛИЗОВАННОМ теле.** Проверка «только
 * пробелы» повторяет серверное правило (`trim` + проверка на невидимые
 * символы): иначе человек нажимал бы кнопку и получал `422` без объяснения.
 * Нормализация здесь — ПОДСКАЗКА, не рубеж: настоящий рубеж остаётся в
 * Pydantic (`min_length=1` + `mode="before"`).
 *
 * **Отправка через BFF** (`/api/proxy/support/tickets`), не напрямую в API:
 * cookie сессии ставится на origin страницы (D-10 стори 2.3), и запрос из
 * браузера в другой origin не понёс бы её.
 *
 * **`Retry-After` показывается человеку.** `429` без числа заставил бы
 * нажимать кнопку снова и снова; с числом — «попробуйте через 42 минуты».
 */

import { useState } from "react";

import type { SupportTicketView } from "@/components/support/support-list";

/** Предел тела обращения — копия серверной константы (`BODY_MAX_LENGTH`). */
export const BODY_MAX_LENGTH = 2000;

/**
 * Коды отказа API, переведённые в текст (паттерн `notification-settings`).
 *
 * `rate_limited` отдельным текстом без числа: число подставляется из
 * заголовка `Retry-After` в обработчике — иначе пришлось бы держать две
 * строки на один случай.
 */
const ERRORS: Record<string, string> = {
  body_required: "Опишите проблему — пустое обращение отправить нельзя.",
  invalid_request: "Слишком длинное обращение. Сократите текст и попробуйте снова.",
  unauthenticated: "Сессия истекла. Войдите снова.",
  session_expired: "Сессия истекла. Войдите снова.",
  user_blocked: "Доступ к аккаунту ограничен. Напишите на почту поддержки.",
  consent_required: "Нужно согласие на обработку персональных данных.",
  upstream_unavailable: "Сервис недоступен. Попробуйте через минуту.",
};

const DEFAULT_ERROR = "Не удалось отправить обращение. Попробуйте ещё раз.";

/**
 * Проверяет, есть ли в тексте хоть один значимый символ.
 *
 * Невидимые пробелы (NBSP, ZWSP, BOM) считаются пустотой: сервер снимает их
 * нормализацией, и без этой проверки кнопка была бы активна на тексте, который
 * заведомо получит `422`.
 */
function hasVisibleText(value: string): boolean {
  return /[^\s\u00a0\u0085\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]/.test(value);
}

/**
 * Человеческий текст для `429`: секунды из `Retry-After` → минуты.
 *
 * Часы показываются только когда ждать действительно долго (окно — час):
 * «подождите 3600 секунд» человек не читает. Округление ВВЕРХ — обещать
 * меньше, чем нужно, значит гарантировать повторный `429`.
 */
function formatRetryAfter(raw: string | null): string {
  const seconds = Number.parseInt(raw ?? "", 10);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "Слишком много обращений подряд. Попробуйте позже.";
  }
  const minutes = Math.ceil(seconds / 60);
  return `Слишком много обращений подряд. Попробуйте через ${minutes} мин.`;
}

export function SupportForm({ onCreated }: { onCreated: (ticket: SupportTicketView) => void }) {
  const [body, setBody] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  const canSend = !pending && hasVisibleText(body) && body.length <= BODY_MAX_LENGTH;

  async function submit(): Promise<void> {
    if (!canSend) return;

    setPending(true);
    setError(null);
    setSent(false);

    try {
      const response = await fetch("/api/proxy/support/tickets", {
        method: "POST",
        headers: { "content-type": "application/json" },
        // BFF живёт на той же origin; без этого cookie сессии не уехала бы
        // в прокси при кросс-доменной сборке.
        credentials: "same-origin",
        body: JSON.stringify({ body }),
      });

      if (!response.ok) {
        const payload = (await response.json().catch(() => ({}))) as { code?: string };
        // `429` получает отдельный текст: код `rate_limited` есть в общем
        // словаре, но без числа из `Retry-After` он бесполезен.
        if (payload.code === "rate_limited" || response.status === 429) {
          setError(formatRetryAfter(response.headers.get("Retry-After")));
        } else {
          setError((payload.code ? ERRORS[payload.code] : undefined) ?? DEFAULT_ERROR);
        }
        // Ввод НЕ очищается: текст — работа человека, а не мусор формы.
        return;
      }

      // AC-01, D-9.5-5: `201` отдаёт полный элемент — вставляем его в список
      // без второго запроса, поэтому экран не мигает.
      const created = (await response.json()) as SupportTicketView;
      onCreated(created);
      // Очистка ТОЛЬКО после подтверждения сервера.
      setBody("");
      setSent(true);
    } catch {
      // Сеть отвалилась — тот же принцип: текст остаётся у человека.
      setError(DEFAULT_ERROR);
    } finally {
      setPending(false);
    }
  }

  return (
    <form
      className="flex flex-col gap-3 rounded-2xl border border-[#E2D9CB] px-4 py-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label htmlFor="support-body" className="flex flex-col gap-1">
        <span className="text-base leading-relaxed">Что случилось</span>
        <span className="text-sm text-[#5A5249]">
          Опишите шаги: что вы делали и что ожидали увидеть.
        </span>
      </label>
      <textarea
        id="support-body"
        name="body"
        value={body}
        onChange={(event) => {
          setBody(event.target.value);
          // Ошибка снимается на первой правке: она относилась к прошлой
          // попытке отправки, а не к новому тексту.
          if (error) setError(null);
        }}
        rows={6}
        maxLength={BODY_MAX_LENGTH}
        aria-describedby="support-counter"
        className="min-h-32 rounded-2xl border border-[#E2D9CB] px-4 py-3 text-base leading-relaxed focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#7A3E12]"
      />
      <div className="flex items-center justify-between gap-3">
        {/* AC-04, Task 6.2: счётчик `0 / 2000` — человек видит предел заранее,
            а не узнаёт о нём по `422`. */}
        <span id="support-counter" className="text-sm text-[#5A5249]">
          {body.length} / {BODY_MAX_LENGTH}
        </span>
        <button
          type="submit"
          disabled={!canSend}
          className="rounded-2xl border border-[#8A7A66] px-4 py-3 text-base transition-colors hover:border-[#7A3E12] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {pending ? "Отправляем…" : "Отправить"}
        </button>
      </div>
      {error ? (
        <p role="alert" className="text-sm text-[#A0281C]">
          {error}
        </p>
      ) : null}
      {sent ? (
        <p role="status" className="text-sm text-[#5A5249]">
          Обращение отправлено. Ответ придёт в уведомления.
        </p>
      ) : null}
    </form>
  );
}
