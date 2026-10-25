"use client";

/**
 * Форма подтверждения 18+ и согласия на обработку ПД (Task 8.3–8.6; CR 1.6: 4, 12, 18).
 *
 * Клиентский компонент, потому что API принимает JSON, а обычная HTML-форма
 * отправила бы `application/x-www-form-urlencoded` — FastAPI ответил бы `422`
 * на каждую попытку. Отправка идёт через BFF (`/api/proxy/auth/consent`):
 * cookie должны ставиться на той же origin, где открыта страница.
 *
 * AC-03: кнопка недоступна, пока не выполнены все три условия. Проверка
 * на клиенте — удобство, а не защита: API отвечает `422 consent_required` /
 * `422 gender_required` даже при обходе формы.
 *
 * Патч 4 код-ревью 1.6: тело запроса собирается из **состояния формы**,
 * а не из хардкода `{age_confirmed: true, consent: true}`. Хардкод означал,
 * что любая рассинхронизация состояния (сброс `checked` после ошибки,
 * будущее автозаполнение, повторная отправка из истории) отправляла согласие,
 * которого пользователь не давал — а согласие по 152-ФЗ должно быть явным.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

/** Коды отказов API, переведённые в текст (паттерн `phone-login-form`). */
const ERRORS: Record<string, string> = {
  consent_required: "Отметьте оба пункта: 18+ и согласие на обработку данных.",
  gender_required: "Выберите пол — он нужен для подбора.",
  // Решение 2 код-ревью 1.6: `PATCH /auth/gender` в контракте нет, пол меняется
  // только поддержкой. Текст ведёт на страницу с контактом, а не в пустоту.
  gender_locked: "Пол уже зафиксирован. Чтобы исправить выбор, напишите в поддержку.",
  user_blocked: "Доступ к аккаунту ограничен. Напишите в поддержку.",
  unauthenticated: "Сессия истекла. Войдите снова.",
  // Патч 12: без этой записи пользователь видел «попробуйте ещё раз»,
  // повторял заведомо провальный запрос и оставался на месте.
  session_expired: "Сессия истекла. Войдите снова.",
  consent_race: "Не удалось сохранить из-за одновременного запроса. Обновите страницу.",
};

/** Коды, после которых оставаться на форме бессмысленно: нужен повторный вход. */
const REAUTH_CODES = new Set(["unauthenticated", "session_expired"]);

const DEFAULT_ERROR = "Не удалось сохранить подтверждение. Попробуйте ещё раз.";

/** Куда вести по ссылке «поддержка»: отдельной страницы пока нет (эпик 7, FR55). */
const SUPPORT_HREF = "/privacy#contact";

type Gender = "female" | "male";

export function ConsentForm() {
  const router = useRouter();
  const [ageConfirmed, setAgeConfirmed] = useState(false);
  const [consent, setConsent] = useState(false);
  const [gender, setGender] = useState<Gender | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPending, setPending] = useState(false);

  // AC-03: все три условия обязательны — иначе кнопка недоступна.
  const canSubmit = ageConfirmed && consent && gender !== null && !isPending;

  /** Разбирает отказ API: `{code}` → текст. Неизвестный код — общая фраза. */
  async function fail(response: Response): Promise<void> {
    let code: string | undefined;
    try {
      const payload = (await response.json()) as { code?: string };
      code = payload.code;
    } catch {
      // Тело не JSON (502 от BFF, HTML от прокси) — общая фраза.
    }

    // Патч 12: истёкшая сессия — не «повторите попытку», а путь на вход.
    if (code !== undefined && REAUTH_CODES.has(code)) {
      router.replace("/login?error=session_expired");
      return;
    }

    setError((code && ERRORS[code]) || DEFAULT_ERROR);
  }

  async function submit(): Promise<void> {
    if (!canSubmit || gender === null) return;
    setError(null);
    setPending(true);
    try {
      const response = await fetch("/api/proxy/auth/consent", {
        method: "POST",
        headers: { "content-type": "application/json" },
        // Патч 4: значения берутся из состояния формы. Хардкод `true` отправлял
        // согласие независимо от того, что видит пользователь.
        body: JSON.stringify({ age_confirmed: ageConfirmed, consent, gender }),
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        await fail(response);
        return;
      }
      // Task 8.6: `replace`, чтобы «Назад» не возвращал на заполненную форму.
      // Заглушка ожидания живёт на `/app` и включается флагом API (Решение 3).
      router.replace("/app");
      router.refresh();
    } catch {
      setError(DEFAULT_ERROR);
    } finally {
      setPending(false);
    }
  }

  return (
    <form
      className="flex flex-col gap-6"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <fieldset className="flex flex-col gap-3">
        <legend className="pb-2 text-base font-medium text-ink">Пол</legend>
        <div className="flex flex-col gap-2 sm:flex-row">
          {(
            [
              { value: "female", label: "Женщина" },
              { value: "male", label: "Мужчина" },
            ] as const
          ).map((option) => (
            <label
              key={option.value}
              className="flex min-h-11 cursor-pointer items-center gap-3 rounded-md border border-border-input px-4 py-2 text-base has-checked:bg-surface"
            >
              <input
                type="radio"
                name="gender"
                value={option.value}
                required
                checked={gender === option.value}
                onChange={() => setGender(option.value)}
                className="h-5 w-5 accent-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
              />
              {option.label}
            </label>
          ))}
        </div>
        {/* AC-02: честно про следующий шаг, без обещания сроков и очереди. */}
        <p className="text-sm text-ink-secondary">
          Сейчас открыта регистрация для женщин. Мужчинам заявка откроется в следующем обновлении.
        </p>
      </fieldset>

      <div className="flex flex-col gap-3">
        <label className="flex items-start gap-3 text-base text-ink">
          <input
            type="checkbox"
            name="age"
            checked={ageConfirmed}
            onChange={(event) => setAgeConfirmed(event.target.checked)}
            aria-describedby="age-hint"
            className="mt-1 h-5 w-5 shrink-0 accent-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
          />
          <span id="age-hint">Мне исполнилось 18 лет</span>
        </label>

        {/*
         * Патч 18: `<a>` вынесены из `<label>`. Внутри метки клик по ссылке
         * одновременно переключал чекбокс: браузер считает клик по потомку
         * label кликом по самому label. Пользователь открывал политику и
         * возвращался с уже отмеченным согласием, которого не давал.
         * Поэтому структура: чекбокс в своей метке, ссылки — отдельным абзацем
         * и связаны с чекбоксом через `aria-describedby`.
         */}
        <div className="flex items-start gap-3 text-base text-ink">
          <input
            id="consent"
            type="checkbox"
            name="consent"
            checked={consent}
            onChange={(event) => setConsent(event.target.checked)}
            aria-describedby="consent-hint consent-docs"
            className="mt-1 h-5 w-5 shrink-0 accent-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
          />
          <label htmlFor="consent" id="consent-hint" className="cursor-pointer">
            Отмечая этот пункт, я даю согласие на обработку персональных данных.
          </label>
        </div>
        <p id="consent-docs" className="pl-8 text-sm text-ink-secondary">
          Условия — в{" "}
          <a
            href="/privacy"
            className="text-accent underline underline-offset-4"
            target="_blank"
            rel="noreferrer"
          >
            политике обработки персональных данных
          </a>{" "}
          и{" "}
          <a
            href="/terms"
            className="text-accent underline underline-offset-4"
            target="_blank"
            rel="noreferrer"
          >
            оферте
          </a>
          .
        </p>
      </div>

      {error !== null && (
        <p role="alert" className="rounded-md bg-danger-tint px-4 py-3 text-sm text-danger">
          {error}{" "}
          {/* Решение 2: рабочая ссылка вместо обещания «напишите в поддержку». */}
          <a href={SUPPORT_HREF} className="underline underline-offset-4">
            Контакт поддержки
          </a>
        </p>
      )}

      <button
        type="submit"
        disabled={!canSubmit}
        className="min-h-11 rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-ink transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:opacity-60"
      >
        {isPending ? "Сохраняем…" : "Продолжить"}
      </button>
    </form>
  );
}
