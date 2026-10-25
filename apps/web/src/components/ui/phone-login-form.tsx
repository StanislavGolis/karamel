"use client";

/**
 * Вход по телефону на экране `/login` (Решение 2 код-ревью CR 1.5).
 *
 * Клиентский компонент, потому что API принимает JSON (`PhoneRequest`), а
 * обычная HTML-форма отправляет `application/x-www-form-urlencoded` — FastAPI
 * ответил бы `422` на каждую попытку. Второй шаг (`/verify`) здесь же:
 * без него страница входа по телефону обрывалась бы на «код отправлен»,
 * хотя API умеет оба шага с стори 1.4.
 *
 * Запросы идут через BFF (`/api/proxy/...`), а не напрямую в API: cookie
 * должны ставиться на той же origin, где открыта страница (AC-03).
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

/** Коды отказов API, переведённые в текст. Неизвестный код — общая фраза. */
const ERRORS: Record<string, string> = {
  invalid_phone: "Проверьте номер: нужен российский мобильный.",
  rate_limited: "Слишком много попыток. Подождите немного.",
  code_invalid: "Код не подошёл. Проверьте цифры.",
  code_expired: "Код истёк. Запросите новый.",
  phone_taken: "Этот номер уже привязан к другому аккаунту.",
  user_blocked: "Доступ к аккаунту ограничен. Напишите в поддержку.",
  storage_unavailable: "Сервис временно недоступен. Попробуйте позже.",
};

const DEFAULT_ERROR = "Не удалось выполнить запрос. Попробуйте ещё раз.";

type Step = "phone" | "code";

export function PhoneLoginForm() {
  const router = useRouter();
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<Step>("phone");
  const [error, setError] = useState<string | null>(null);
  const [isPending, setPending] = useState(false);

  /** Разбирает отказ API и выставляет текст ошибки. */
  async function fail(response: Response): Promise<void> {
    let code: string | undefined;
    try {
      const payload = (await response.json()) as { code?: string };
      code = payload.code;
    } catch {
      // Тело не JSON (502 от BFF, HTML от прокси) — общая фраза.
    }
    setError((code && ERRORS[code]) || DEFAULT_ERROR);
  }

  async function requestCode(): Promise<void> {
    setError(null);
    setPending(true);
    try {
      const response = await fetch("/api/proxy/auth/phone/request", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ phone }),
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        await fail(response);
        return;
      }
      setStep("code");
    } catch {
      setError(DEFAULT_ERROR);
    } finally {
      setPending(false);
    }
  }

  async function verifyCode(): Promise<void> {
    setError(null);
    setPending(true);
    try {
      const response = await fetch("/api/proxy/auth/phone/verify", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ phone, code }),
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        await fail(response);
        return;
      }
      // Сессия выданa cookie; layout кабинета перечитает её через `/auth/me`.
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
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        void (step === "phone" ? requestCode() : verifyCode());
      }}
    >
      <label className="flex flex-col gap-2 text-sm text-[#1F1B17]">
        Телефон
        <input
          type="tel"
          name="phone"
          required
          autoComplete="tel"
          placeholder="+7 900 000-00-00"
          value={phone}
          onChange={(event) => setPhone(event.target.value)}
          // После отправки кода номер не редактируется: код выдан именно на него.
          readOnly={step === "code"}
          className="min-h-11 rounded-md border border-[#8A7A66] px-3 py-2 text-base read-only:opacity-70"
        />
      </label>

      {step === "code" && (
        <label className="flex flex-col gap-2 text-sm text-[#1F1B17]">
          Код из SMS
          <input
            type="text"
            name="code"
            required
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            className="min-h-11 rounded-md border border-[#8A7A66] px-3 py-2 text-base"
          />
        </label>
      )}

      <button
        type="submit"
        disabled={isPending}
        className="min-h-11 rounded-md bg-[#7A3E12] px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#7A3E12] disabled:opacity-60"
      >
        {isPending ? "Отправляем…" : step === "phone" ? "Получить код по SMS" : "Войти"}
      </button>

      {step === "code" && (
        <button
          type="button"
          onClick={() => {
            setStep("phone");
            setCode("");
            setError(null);
          }}
          className="text-sm text-[#5A5249] underline underline-offset-4"
        >
          Изменить номер
        </button>
      )}

      {error !== null && (
        <p role="alert" className="rounded-md bg-[#FBE9E6] px-4 py-3 text-sm text-[#A0281C]">
          {error}
        </p>
      )}
    </form>
  );
}
