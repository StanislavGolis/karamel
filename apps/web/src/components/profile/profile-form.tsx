"use client";

/**
 * Форма анкеты: имя, город, возраст, текст (Task 10.1–10.6; AC-01, AC-04..AC-07).
 *
 * Клиентский компонент: API принимает JSON, а обычная HTML-форма отправила бы
 * `application/x-www-form-urlencoded` — FastAPI ответил бы `422` на каждую
 * попытку. Отправка идёт через BFF (`/api/proxy/profiles…`), чтобы cookie
 * ставились на той же origin, где открыта страница (D-10 стори 2.3).
 *
 * AC-07: кнопка недоступна, пока не заполнены обязательные поля. Проверка
 * на клиенте — удобство, а не защита: API отвечает `422 city_required`
 * и остальными доменными кодами даже при обходе формы.
 *
 * D-1 стори 2.3: создание и правка — разные методы. Наличие сохранённой
 * анкеты определяет `initial`: `null` → `POST` (`201`), иначе `PATCH` (`200`).
 * Клиент не должен сам решать «это создание или правка»: `POST` по готовой
 * анкете даёт `409`, и пользователь увидел бы ошибку вместо сохранения.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

/** Коды отказов API, переведённые в текст (паттерн `consent-form`). */
const ERRORS: Record<string, string> = {
  city_required: "Укажите город — без него анкету не найти в вашем городе.",
  field_required: "Поле не может быть пустым.",
  // CR 2.3, патч 12: сбой GPS — это не «не удалось сохранить анкету».
  // Пользователь должен понять, что нужно разрешить доступ к геолокации
  // или ввести город вручную, а не искать проблему в тексте анкеты.
  invalid_coordinates:
    "Не удалось определить местоположение. Разрешите доступ к геолокации или введите город вручную.",
  name_too_long: "Имя длиннее 50 символов. Сократите, пожалуйста.",
  city_too_long: "Название города длиннее 100 символов. Проверьте написание.",
  bio_too_long: "Текст длиннее 500 символов. Сократите, пожалуйста.",
  invalid_age: "Возраст должен быть от 18 до 99 лет.",
  invalid_request: "Не удалось прочитать данные формы. Проверьте поля и попробуйте снова.",
  // Фикс OOM (2026-10-22): отказ гарда размера тела. Для анкеты недостижим
  // (легальное тело — единицы КБ), но код существует и должен читаться
  // человеком, а не превращаться в «не удалось сохранить».
  request_too_large: "Данные слишком большие. Сократите текст анкеты и попробуйте снова.",
  profile_exists: "Анкета уже создана. Обновите страницу, чтобы её отредактировать.",
  profile_not_found: "Анкета ещё не создана. Заполните поля и сохраните.",
  gender_required: "Не выбран пол. Вернитесь на шаг подтверждения.",
  consent_required: "Нужно согласие на обработку персональных данных.",
  user_blocked: "Доступ к аккаунту ограничен. Напишите в поддержку.",
  unauthenticated: "Сессия истекла. Войдите снова.",
  session_expired: "Сессия истекла. Войдите снова.",
};

/**
 * Коды, после которых оставаться на форме бессмысленно: нужен повторный вход.
 *
 * CR 2.3, патч 10: `consent_required` здесь БОЛЬШЕ НЕТ. Согласие — не истёкшая
 * сессия: пользователь вошёл, но не подтвердил 18+ и ПД. Отправка его на
 * `/login?error=consent_required` показывала экран входа, где текст ошибки
 * из `ERRORS` недостижим, и войти повторно он тоже не мог без согласия.
 */
const REAUTH_CODES = new Set(["unauthenticated", "session_expired"]);

/** Коды, требующие шага согласия: ведут на `/consent`, а не на вход. */
const CONSENT_CODES = new Set(["consent_required"]);

const DEFAULT_ERROR = "Не удалось сохранить анкету. Попробуйте ещё раз.";

/** Пределы совпадают со схемой API (Task 2.3), чтобы клиент не отправлял заведомый отказ. */
const LIMITS = { name: 50, city: 100, bio: 500, ageMin: 18, ageMax: 99 } as const;

/** Ответ `GET /profiles/me` в объёме, нужном форме (без `id`/`user_id` — их нет в API). */
export type ProfileValues = {
  name: string;
  city: string;
  age: string;
  bio: string;
};

const EMPTY: ProfileValues = { name: "", city: "", age: "", bio: "" };

/**
 * Совместимость: `initial` может быть `null` (анкеты нет), частичным
 * (черновик из 2.2) или полным. `undefined`/`null` поля → пустая строка:
 * `value={null}` React превратил бы в неконтролируемое поле.
 */
function toValues(initial: Partial<ProfileValues> | null): ProfileValues {
  if (initial === null) return EMPTY;
  return {
    name: initial.name ?? "",
    city: initial.city ?? "",
    age: initial.age ?? "",
    bio: initial.bio ?? "",
  };
}

type FieldName = keyof ProfileValues;

export function ProfileForm({ initial }: { initial: Partial<ProfileValues> | null }) {
  const router = useRouter();
  const [values, setValues] = useState<ProfileValues>(() => toValues(initial));
  const [error, setError] = useState<string | null>(null);
  const [invalidField, setInvalidField] = useState<FieldName | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [isPending, setPending] = useState(false);
  const [isLocating, setLocating] = useState(false);

  const isEdit = initial !== null;

  // AC-01, AC-07: все четыре поля обязательны (CR 2.3, Решение 2).
  // Раньше проверялись только `city` и `bio`: пустое имя и пустой возраст
  // уходили на сервер (`age: Number("")` → `0`), и пользователь получал
  // `422 invalid_request` без указания, какое поле исправить.
  const canSubmit =
    values.name.trim().length > 0 &&
    values.city.trim().length > 0 &&
    values.bio.trim().length > 0 &&
    values.age.trim().length > 0 &&
    !isPending;

  function update(field: FieldName, value: string): void {
    setValues((current) => ({ ...current, [field]: value }));
    // Ошибка относилась к предыдущему значению: после правки поля она
    // вводила бы в заблуждение (тот же приём, что в `consent-form`).
    if (invalidField === field) setInvalidField(null);
    if (error !== null) setError(null);
    if (status !== null) setStatus(null);
  }

  /** Разбирает отказ API: `{code}` → текст. Неизвестный код — общая фраза. */
  async function fail(response: Response): Promise<void> {
    let code: string | undefined;
    try {
      const payload = (await response.json()) as { code?: string };
      code = payload.code;
    } catch {
      // Тело не JSON (502 от BFF, HTML от прокси) — общая фраза.
    }

    if (code !== undefined && REAUTH_CODES.has(code)) {
      router.replace(`/login?error=${code}`);
      return;
    }

    // CR 2.3, патч 10: согласие — отдельный шаг, а не повторный вход.
    if (code !== undefined && CONSENT_CODES.has(code)) {
      router.replace("/consent");
      return;
    }

    // Какое поле подсветить, решается по коду: доменный код называет поле.
    if (code === "city_required" || code === "city_too_long") setInvalidField("city");
    else if (code === "name_too_long") setInvalidField("name");
    else if (code === "bio_too_long") setInvalidField("bio");
    else if (code === "invalid_age") setInvalidField("age");
    else setInvalidField(null);

    setError((code && ERRORS[code]) || DEFAULT_ERROR);
  }

  async function submit(): Promise<void> {
    if (!canSubmit) return;
    setError(null);
    setStatus(null);
    setPending(true);
    try {
      // CR 2.3, Решение 2: возраст проверяется до отправки. Раньше пустое поле
      // уходило как `Number("")` → `0`, сервер отвечал `422 invalid_age`
      // (или `invalid_request`), и пользователь не понимал, что не так.
      // Теперь пустой и нечисловой возраст — отказ на клиенте с подсветкой поля.
      const parsedAge = Number(values.age);
      if (values.age.trim().length === 0 || !Number.isFinite(parsedAge)) {
        setInvalidField("age");
        setError("Укажите возраст числом.");
        return;
      }

      // AC-02, AC-03: тело — только поля анкеты. `gender`/`status` API
      // отклоняет (`extra="forbid"`), поэтому они не отправляются вовсе.
      const body: Record<string, string | number> = {
        name: values.name.trim(),
        city: values.city.trim(),
        age: parsedAge,
        bio: values.bio.trim(),
      };

      const response = await fetch(
        isEdit ? "/api/proxy/profiles/me" : "/api/proxy/profiles",
        {
          method: isEdit ? "PATCH" : "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
          credentials: "same-origin",
          cache: "no-store",
        },
      );

      if (!response.ok) {
        await fail(response);
        return;
      }

      setStatus("Анкета сохранена.");
      // `refresh` перерисовывает серверный компонент: он заново получит анкету
      // и переключит форму в режим правки без перезагрузки страницы.
      router.refresh();
    } catch {
      setError(DEFAULT_ERROR);
    } finally {
      setPending(false);
    }
  }

  /**
   * AC-06: «Определить город» — геолокация запрашивается ТОЛЬКО по нажатию.
   *
   * Браузер спрашивает разрешение при первом вызове `getCurrentPosition`,
   * поэтому автозапрос при загрузке страницы был бы неожиданным
   * и нарушал бы ожидание пользователя (UX: явное действие — явный запрос).
   * Координаты уходят на сервер и не сохраняются в браузере: в ответ
   * приходит только название города (FR59).
   */
  async function locate(): Promise<void> {
    if (typeof navigator === "undefined" || navigator.geolocation === undefined) {
      setError("Браузер не поддерживает геолокацию. Введите город вручную.");
      return;
    }
    setError(null);
    setStatus(null);
    setLocating(true);

    const position = await new Promise<GeolocationPosition | null>((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (value) => resolve(value),
        // Отказ в доступе и таймаут — не ошибка приложения: пользователь
        // вводит город вручную, поэтому молча возвращаем `null`.
        () => resolve(null),
        { enableHighAccuracy: false, timeout: 10_000, maximumAge: 300_000 },
      );
    });

    if (position === null) {
      setLocating(false);
      setError("Не удалось определить местоположение. Введите город вручную.");
      return;
    }

    try {
      const response = await fetch("/api/proxy/profiles/locate-city", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          accuracy: position.coords.accuracy,
        }),
        credentials: "same-origin",
        cache: "no-store",
      });

      if (!response.ok) {
        await fail(response);
        return;
      }

      const payload = (await response.json()) as { city?: string | null };
      if (typeof payload.city === "string" && payload.city.length > 0) {
        update("city", payload.city);
        return;
      }
      // AC-06: вне пилота — не ошибка, а честное «не определился».
      setError("Томск не определился, введите город вручную.");
      setInvalidField("city");
    } catch {
      setError("Не удалось определить город. Введите его вручную.");
    } finally {
      setLocating(false);
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
      <Field
        id="name"
        label="Имя"
        value={values.name}
        maxLength={LIMITS.name}
        invalid={invalidField === "name"}
        hint="Так вас увидят в карточке."
        onChange={(value) => update("name", value)}
      />

      <div className="flex flex-col gap-2">
        <Field
          id="city"
          label="Город"
          value={values.city}
          maxLength={LIMITS.city}
          required
          invalid={invalidField === "city"}
          hint="По городу вас находят. Без города анкета не сохраняется."
          onChange={(value) => update("city", value)}
        />
        <button
          type="button"
          onClick={() => void locate()}
          disabled={isLocating || isPending}
          className="min-h-11 self-start rounded-md border border-border-input px-4 py-2 text-sm font-medium text-ink transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:opacity-60"
        >
          {isLocating ? "Определяем…" : "Определить город"}
        </button>
      </div>

      <Field
        id="age"
        label="Возраст"
        value={values.age}
        type="number"
        min={LIMITS.ageMin}
        max={LIMITS.ageMax}
        invalid={invalidField === "age"}
        hint={`От ${LIMITS.ageMin} до ${LIMITS.ageMax} лет.`}
        onChange={(value) => update("age", value)}
      />

      <div className="flex flex-col gap-2">
        <label htmlFor="bio" className="text-base font-medium text-ink">
          О себе <span aria-hidden="true">*</span>
        </label>
        <textarea
          id="bio"
          value={values.bio}
          maxLength={LIMITS.bio}
          required
          rows={5}
          aria-invalid={invalidField === "bio"}
          aria-describedby="bio-hint"
          onChange={(event) => update("bio", event.target.value)}
          className="min-h-11 rounded-md border border-border-input bg-surface-raised px-4 py-2 text-base text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
        />
        <p id="bio-hint" className="text-sm text-ink-secondary">
          До {LIMITS.bio} символов. Осталось: {LIMITS.bio - values.bio.length}.
        </p>
      </div>

      {error !== null && (
        <p role="alert" className="rounded-md bg-danger-tint px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {status !== null && (
        <p role="status" className="rounded-md bg-surface px-4 py-3 text-sm text-ink">
          {status}
        </p>
      )}

      {/* §8.3 «формы»: `aria-busy` при отправке. Текст кнопки НЕ меняется на
          «Сохраняем…» — §6.4 п.3: смена текста двигает кнопку по ширине. */}
      <button
        type="submit"
        disabled={!canSubmit}
        aria-busy={isPending || undefined}
        className="min-h-11 rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-ink transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:opacity-60"
      >
        {isEdit ? "Сохранить изменения" : "Сохранить анкету"}
      </button>
    </form>
  );
}

/**
 * Однострочное поле с меткой, подсказкой и `aria-invalid` (project-context §10).
 *
 * Вынесено, чтобы четыре поля не расходились в разметке доступности:
 * `aria-describedby` связывает поле с подсказкой, `aria-invalid` сообщает
 * скринридеру о конкретной ошибке, а не только красит рамку.
 */
function Field({
  id,
  label,
  value,
  onChange,
  hint,
  invalid = false,
  required = false,
  type = "text",
  maxLength,
  min,
  max,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint: string;
  invalid?: boolean;
  required?: boolean;
  type?: "text" | "number";
  maxLength?: number;
  min?: number;
  max?: number;
}) {
  return (
    <div className="flex flex-col gap-2">
      <label htmlFor={id} className="text-base font-medium text-ink">
        {label}
        {required && <span aria-hidden="true"> *</span>}
      </label>
      <input
        id={id}
        type={type}
        value={value}
        required={required}
        maxLength={maxLength}
        min={min}
        max={max}
        aria-invalid={invalid}
        aria-describedby={`${id}-hint`}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-11 rounded-md border border-border-input bg-surface-raised px-4 py-2 text-base text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
      />
      <p id={`${id}-hint`} className="text-sm text-ink-secondary">
        {hint}
      </p>
    </div>
  );
}
