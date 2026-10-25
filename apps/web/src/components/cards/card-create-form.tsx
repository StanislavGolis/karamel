"use client";

/**
 * Создание карточки события (Task 7; AC-09, AC-01…AC-03).
 *
 * Пять полей на одном экране, без визарда (UX `Поверхность 2а`): «Что»,
 * «Когда», «Где», «Сколько человек», «Описание». Поле «Окно решения»
 * появляется ТОЛЬКО при вместимости 1: в открытом наборе окно не запрашивается
 * (FR70), значение приходит из конфига (OQ-1: 48 ч).
 *
 * **Отклонение от стори (Task 7.4):** `react-hook-form` и `@tanstack/react-query`
 * в проекте не установлены, а установка зависимостей по AGENTS.md требует
 * согласования. Поэтому состояние — `useState` + `zod` (zod в проекте есть),
 * как в `components/profile/profile-photos.tsx` (2.5). Функционально то же:
 * валидация до отправки, значения не теряются при ошибке сети.
 *
 * Запросы идут через BFF (`/api/proxy/...`): cookie ставится на той же origin,
 * где открыта страница (D-10 стори 2.3).
 */

import { useState } from "react";
import { z } from "zod";

/** Состояния экрана по UX `2а`. `rejected` — 4.3, `limit-reached` — 4.4. */
type FormState =
  | { kind: "draft-new" }
  | { kind: "submitting" }
  | { kind: "verification-pending"; uuid: string }
  | { kind: "error"; message: string; field?: FieldName };

/** Поля, к которым привязываются inline-ошибки. */
type FieldName = "title" | "starts_at" | "place_uuid" | "capacity" | "description";

/**
 * Место из справочника — то, что отдаёт `GET /api/v1/places` (3.3).
 *
 * CR 4.2, патч 1: `category` обязателен. `EventCardCreate` требует его
 * (FR69), а в форме отдельного поля категории нет — UX `2а` его не показывает.
 * Категория берётся из выбранного места: справочник уже типизирован
 * (`PlaceCategory`), и это единственный источник без нового поля ввода.
 */
export type PlaceOption = {
  uuid: string;
  name: string;
  address_text: string;
  category: string;
};

/** Коды API → текст. Клиент ветвится по `code`, а не по тексту ответа (§4). */
const ERRORS: Record<string, { message: string; field?: FieldName }> = {
  PLACE_NOT_AVAILABLE: {
    message: "Это место недоступно. Выберите другое из справочника.",
    field: "place_uuid",
  },
  STARTS_AT_IN_PAST: {
    message: "Нельзя создать встречу в прошлом.",
    field: "starts_at",
  },
  decision_window_required: {
    message: "Укажите, за сколько времени вы выберете человека.",
    field: "capacity",
  },
  unauthenticated: { message: "Сессия истекла. Войдите снова." },
  request_too_large: { message: "Слишком длинное описание.", field: "description" },
};

/**
 * Схема формы: правила совпадают с `EventCardCreate` на бэкенде.
 *
 * `zod` не проверяет «в прошлом» — это серверное правило (AC-03): часы клиента
 * врут, и решение принимает `now()` сервера.
 */
const formSchema = z.object({
  title: z.string().trim().min(1, "Название не может быть пустым").max(100, "Слишком длинное"),
  starts_at: z.string().min(1, "Укажите дату и время"),
  place_uuid: z.string().min(1, "Выберите место из справочника"),
  capacity: z.number().int().min(1).max(6),
  description: z.string().max(2000, "Слишком длинное описание").optional(),
  decision_window_minutes: z.number().int().min(15).max(10080).optional(),
});

type FormValues = z.infer<typeof formSchema>;

type Props = {
  /** Места для селекта — загружаются серверным компонентом (FR98). */
  places: PlaceOption[];
};

export function CardCreateForm({ places }: Props) {
  const [state, setState] = useState<FormState>({ kind: "draft-new" });
  // Значения держим отдельно от состояния экрана: при ошибке сети ввод
  // не теряется (UX `2а`, требование AC-09).
  const [values, setValues] = useState<FormValues>({
    title: "",
    starts_at: "",
    place_uuid: "",
    capacity: 2,
  });
  const [errors, setErrors] = useState<Partial<Record<FieldName, string>>>({});

  const isRequestMode = values.capacity === 1;
  const busy = state.kind === "submitting";

  function update<K extends keyof FormValues>(key: K, value: FormValues[K]) {
    setValues((current) => {
      const next = { ...current, [key]: value };
      // CR 4.2, патч 2: при переходе в режим заявок окно должно попасть
      // в запрос. Селект показывает «1 час» по умолчанию, но
      // `decision_window_minutes` оставался `undefined`, пока пользователь
      // не тронет поле — и сервер отвечал `422 decision_window_required`
      // на визуально заполненной форме.
      if (key === "capacity" && value === 1 && next.decision_window_minutes === undefined) {
        next.decision_window_minutes = 60;
      }
      // Обратный переход: окно открытого набора не запрашивается (FR70),
      // но оставлять его в payload можно — сервер примет явное значение.
      return next;
    });
    // Сбрасываем ошибку поля при правке: пользователь уже исправляет,
    // красная рамка на верном значении путает.
    setErrors((current) => ({ ...current, [key]: undefined }));
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = formSchema.safeParse(values);
    if (!parsed.success) {
      const next: Partial<Record<FieldName, string>> = {};
      for (const issue of parsed.error.issues) {
        const field = issue.path[0] as FieldName;
        next[field] = issue.message;
      }
      setErrors(next);
      return;
    }

    setState({ kind: "submitting" });
    setErrors({});

    let response: Response;
    try {
      // CR 4.2, патч 1: `category` берётся из выбранного места — иначе
      // запрос отвергался бы `422` (поле обязательно в `EventCardCreate`).
      const selectedPlace = places.find((place) => place.uuid === parsed.data.place_uuid);
      response = await fetch("/api/proxy/cards", {
        method: "POST",
        headers: { "content-type": "application/json" },
        // `starts_at` уходит ISO-8601 с зоной: datetime-local даёт локальное
        // время без зоны, а сервер требует зону (ADR-16) и отклонит наивное.
        body: JSON.stringify({
          ...parsed.data,
          category: selectedPlace?.category ?? "other",
          starts_at: new Date(parsed.data.starts_at).toISOString(),
        }),
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch {
      setState({ kind: "error", message: "Не удалось сохранить. Попробуйте снова." });
      return;
    }

    if (response.status === 201) {
      const body = (await response.json()) as { uuid: string; status: string };
      if (body.status === "published") {
        setState({ kind: "verification-pending", uuid: body.uuid });
      } else {
        // AC-01, D-4.2-5: новая карточка — `draft` с `pending`, пока проверка
        // контента (4.3) не реализована. Показываем это текстом (NFR57).
        setState({ kind: "verification-pending", uuid: body.uuid });
      }
      return;
    }

    const payload = (await response.json().catch(() => ({}))) as { code?: string };
    const known = payload.code ? ERRORS[payload.code] : undefined;
    setState({
      kind: "error",
      message: known?.message ?? "Не удалось сохранить. Попробуйте снова.",
      field: known?.field,
    });
  }

  if (state.kind === "verification-pending") {
    return (
      <div className="rounded-lg border border-[color:var(--border)] p-6">
        {/* NFR57: статус текстом, не только цветом. */}
        <p className="text-base">Проверяем текст. Обычно это занимает несколько минут.</p>
        <p className="mt-2 text-sm text-[color:var(--ink-secondary)]">
          Карточка пока видна только вам. Когда проверка закончится, мы пришлём уведомление.
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-6">
      {places.length === 0 ? (
        // FR98: свободного ввода адреса нет, поэтому пустой справочник —
        // не «пустой select», а объяснение, что делать дальше.
        <p className="rounded-lg border border-[color:var(--border)] p-4 text-sm">
          В справочнике пока нет мест. Попросите администратора сообщества добавить место —
          без него карточку создать нельзя.
        </p>
      ) : null}

      <Field label="Что" error={errors.title}>
        <input
          value={values.title}
          onChange={(event) => update("title", event.target.value)}
          maxLength={100}
          className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
          placeholder="Настольные игры в „Праге“"
        />
      </Field>

      <Field label="Когда" error={errors.starts_at}>
        <input
          type="datetime-local"
          value={values.starts_at}
          onChange={(event) => update("starts_at", event.target.value)}
          className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
        />
      </Field>

      <Field label="Где" error={errors.place_uuid}>
        {/* FR98, NFR34: только справочник — свободного ввода адреса нет. */}
        <select
          value={values.place_uuid}
          onChange={(event) => update("place_uuid", event.target.value)}
          className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
        >
          <option value="">Выберите место</option>
          {places.map((place) => (
            <option key={place.uuid} value={place.uuid}>
              {place.name} — {place.address_text}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Сколько человек" error={errors.capacity}>
        {/* FR70: выбор «1» включает режим заявок, 2–6 — открытый набор. */}
        <div className="flex gap-2" role="group" aria-label="Сколько человек">
          {[1, 2, 3, 4, 5, 6].map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={values.capacity === value}
              onClick={() => update("capacity", value)}
              className={`h-11 w-11 rounded-md border ${values.capacity === value ? "border-[color:var(--accent)] font-semibold" : "border-[color:var(--border-input)]"}`}
            >
              {value}
            </button>
          ))}
        </div>
      </Field>

      {isRequestMode ? (
        <Field label="Окно решения" error={undefined}>
          {/* FR70/FR79: в режиме заявок окно обязательно и запрашивается. */}
          <select
            value={values.decision_window_minutes ?? 60}
            onChange={(event) => update("decision_window_minutes", Number(event.target.value))}
            className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
          >
            <option value={60}>1 час</option>
            <option value={360}>6 часов</option>
            <option value={1440}>24 часа</option>
            <option value={2880}>48 часов</option>
          </select>
        </Field>
      ) : (
        <p className="text-sm text-[color:var(--ink-secondary)]">
          Присоединяются сразу, без выбора — окно решения не нужно.
        </p>
      )}

      <Field label="Описание" error={errors.description}>
        <textarea
          value={values.description ?? ""}
          onChange={(event) => update("description", event.target.value)}
          rows={4}
          maxLength={2000}
          className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
          placeholder="Что возьмёте, во что играете, с кем хотите познакомиться"
        />
      </Field>

      {state.kind === "error" ? (
        <p role="alert" className="text-sm text-[color:var(--danger)]">
          {state.message}
        </p>
      ) : null}

      <button
        type="submit"
        disabled={busy || places.length === 0}
        className="h-11 rounded-md bg-[color:var(--accent)] px-6 font-semibold text-[color:var(--accent-ink)] disabled:opacity-60"
      >
        {busy ? "Публикуем…" : "Опубликовать"}
      </button>
    </form>
  );
}

function Field({
  label,
  error,
  children,
}: {
  label: string;
  error: string | undefined;
  children: React.ReactNode;
}) {
  // Ошибка — inline под полем, а не тост (UX `2а`, AC-09): тост исчезает,
  // и пользователь не понимает, какое поле править.
  return (
    <label className="flex flex-col gap-2 text-base">
      <span className="font-medium">{label}</span>
      {children}
      {error ? (
        <span role="alert" className="text-sm text-[color:var(--danger)]">
          {error}
        </span>
      ) : null}
    </label>
  );
}
