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

import { useEffect, useState } from "react";
import { z } from "zod";
// Стори 12.6 (AC-2, Task 7.2): правило пары границ — из общего модуля,
// чтобы форма и лента не разошлись (CR 12.6 H-6).
import { ageRangeFields, withAgeRange } from "@/lib/audience-bounds";
// Стори 13.1 (AC-1, AC-2, AC-4): выбор места с картой OSM через наш прокси.
import { PlacePicker } from "@/components/cards/place-picker";

/** Состояния экрана по UX `2а`. `rejected` — 4.3, `limit-reached` — 4.4. */
type FormState =
  | { kind: "draft-new" }
  | { kind: "submitting" }
  | { kind: "verification-pending"; uuid: string }
  | { kind: "error"; message: string; field?: FieldName };

/** Поля, к которым привязываются inline-ошибки. */
type FieldName =
  | "title"
  | "starts_at"
  | "place_uuid"
  | "capacity"
  | "description"
  // Стори 12.4 (AC-3): тип и активность — отдельные обязательные поля.
  | "kind"
  | "activity"
  // Стори 12.6 (AC-2, Task 7.2): пара возрастных рамок проверяется вместе.
  | "age_min"
  | "age_max";

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
/**
 * Горизонт встречи на клиенте — 72 часа (стори 12.5, AC-1, FR116).
 *
 * **Зеркало серверного `CARD_MAX_HORIZON_HOURS`** (`event_card_service.py:95`).
 * Одна константа на сторону: литерал `72` в двух местах разъехался бы молча
 * (прецедент `D-10.4-1`, `W-11.7-c`).
 *
 * Это **подсказка браузеру** для `max` у `datetime-local`, а НЕ правило:
 * правило живёт на сервере (`STARTS_AT_TOO_FAR`), потому что часы клиента врут
 * и их можно перевести назад. Браузерная подсказка экономит один круг
 * «отправил → получил 422», но никогда его не заменяет.
 */
export const CARD_MAX_HORIZON_HOURS = 72;

/**
 * `max` для `datetime-local`: `момент + 72 ч` в формате «YYYY-MM-DDTHH:mm».
 *
 * Формат именно локальный (без `Z` и без секунд): `datetime-local` не понимает
 * ISO-8601 с зоной и молча игнорирует такое значение — подсказка исчезла бы,
 * не сломав ни одного теста. Секунды отброшены: шаг поля — минуты.
 */
export function horizonLimitLocal(now: Date): string {
  const limit = new Date(now.getTime() + CARD_MAX_HORIZON_HOURS * 3_600_000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${limit.getFullYear()}-${pad(limit.getMonth() + 1)}-${pad(limit.getDate())}T${pad(limit.getHours())}:${pad(limit.getMinutes())}`;
}

const ERRORS: Record<string, { message: string; field?: FieldName }> = {
  PLACE_NOT_AVAILABLE: {
    message: "Это место недоступно. Выберите другое из справочника.",
    field: "place_uuid",
  },
  STARTS_AT_IN_PAST: {
    message: "Нельзя создать встречу в прошлом.",
    field: "starts_at",
  },
  // Стори 12.5 (AC-1, FR116): серверный горизонт 72 часа. Код верхним
  // регистром, как требует §4 `project-context`; эпик писал нижним
  // (`starts_at_too_far`) — расхождение зафиксировано долгом `W-12.5-b`.
  STARTS_AT_TOO_FAR: {
    message: "Встречу можно назначить не дальше трёх суток.",
    field: "starts_at",
  },
  decision_window_required: {
    message: "Укажите, за сколько времени вы выберете человека.",
    field: "capacity",
  },
  unauthenticated: { message: "Сессия истекла. Войдите снова." },
  request_too_large: { message: "Слишком длинное описание.", field: "description" },
  // Стори 12.4: машинные коды сервера для новых полей.
  kind_invalid: { message: "Выберите тип встречи.", field: "kind" },
  activity_invalid: { message: "Выберите занятие из списка.", field: "activity" },
  activity_other_reserved: { message: "Выберите занятие из списка.", field: "activity" },
};

/**
 * Схема формы: правила совпадают с `EventCardCreate` на бэкенде.
 *
 * `zod` не проверяет «в прошлом» — это серверное правило (AC-03): часы клиента
 * врут, и решение принимает `now()` сервера.
 */
const formSchema = withAgeRange(
  z.object({
  // Стори 13.2 (AC-7, FR130, ADR-26): поля `title` здесь НЕТ — заголовок
  // собирает сервер из активности и места. Сервер отвергает присланный
  // `title` (`422 title_not_accepted`), поэтому форма обязана его не
  // отправлять: иначе создание карточки перестало бы работать вовсе.
  starts_at: z.string().min(1, "Укажите дату и время"),
  place_uuid: z.string().min(1, "Выберите место из справочника"),
  capacity: z.number().int().min(1).max(6),
  // Стори 12.4 (AC-3, FR117/FR118): оба поля обязательны и НЕ выводятся из
  // `capacity`. `other` в списке нет — это легаси-значение БД.
  kind: z.enum(["date", "company"], { message: "Выберите тип встречи" }),
  activity: z.enum(
    ["coffee", "walk", "bar", "cinema", "boardgames", "sport"],
    { message: "Выберите занятие" },
  ),
  description: z.string().max(2000, "Слишком длинное описание").optional(),
  decision_window_minutes: z.number().int().min(15).max(10080).optional(),
  // Стори 12.6 (AC-2, Task 7.1–7.2): рамки аудитории. Мягкий фильтр —
  // карточка видна всем (FR81), поэтому все три поля необязательны.
  //
  // Пустые строки — «не задано»: `z.number()` их отклонил бы, а `undefined`
  // в `JSON.stringify` просто не попадает в payload, и сервер применит своё
  // «не задано». Это тот же приём, что у `description`.
  //
  // Сами границы берутся из `ageRangeFields`, а проверка пары — из
  // `withAgeRange`: правило одно на форму и на ленту (CR 12.6 H-6).
  gender_filter: z.enum(["female", "male"]).optional(),
  ...ageRangeFields,
  }),
);

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
    // Стори 13.2 (AC-7): `title` не вводится — сервер собирает его сам.
    starts_at: "",
    place_uuid: "",
    capacity: 2,
    // Пустые строки — «не выбрано»: `z.enum` их отклонит, и форма не уйдёт,
    // пока автор не укажет тип и занятие (AC-3). Не выводим из `capacity`.
    kind: "" as unknown as "date" | "company",
    activity: "" as unknown as "coffee",
    // Стори 12.6 (AC-2): рамки НЕ заданы по умолчанию — «ищу любого».
    // Дефолт «18–99» в форме дал бы карточку с рамками там, где автор
    // ничего не выбирал (UX §1048: показывать только заданное).
    gender_filter: undefined,
    age_min: undefined,
    age_max: undefined,
  });
  const [errors, setErrors] = useState<Partial<Record<FieldName, string>>>({});
  /**
   * Стори 13.1 (AC-9): категория выбранного места.
   *
   * **Почему отдельное состояние, а не поиск по `places`.** Место, созданное
   * из подсказки OSM, в массиве `places` ОТСУТСТВУЕТ: страница загрузила
   * справочник до того, как место появилось. `places.find(...)` вернул бы
   * `undefined`, и карточка ушла бы с `category: "other"` — то есть в ленте
   * вместо кинопросмотра оказалось бы «другое». `PlacePicker` отдаёт
   * категорию вместе с UUID, и она переживает отсутствие в справочнике.
   */
  const [chosenCategory, setChosenCategory] = useState<string | undefined>(undefined);
  /**
   * `max` для `datetime-local` — подсказка браузеру, не правило (AC-1).
   *
   * Считается в одноразовом `useEffect`, а не в рендере: `new Date()` на
   * сервере и в браузере даёт разные строки, и гидрация разошлась бы
   * предупреждением. Эффект без интервала — это НЕ тикающий счётчик (З-9):
   * значение ставится один раз и дальше не меняется.
   */
  const [maxStartsAt, setMaxStartsAt] = useState<string | undefined>(undefined);

  useEffect(() => {
    // Правило `react-hooks/set-state-in-effect` снято осознанно, а не для
    // зелени: значение ЗАВИСИТ от браузерных часов, и вычисление в рендере
    // разошлось бы с серверным (гидрация). Эффект здесь — синхронизация с
    // внешней системой (часы), ровно тот случай, для которого он и нужен;
    // альтернатива (`useSyncExternalStore` с `getServerSnapshot`) дала бы
    // тот же setState, только в обёртке. Долг `W-12.6-z-1` — переписать на
    // `useSyncExternalStore`, если правило понадобится включить обратно.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMaxStartsAt(horizonLimitLocal(new Date()));
  }, []);

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
      //
      // Стори 13.1 (AC-9): порядок источников — сначала `chosenCategory`
      // (его ставит `PlacePicker`, и он верен даже для места, созданного из
      // подсказки OSM), затем справочник. Обратный порядок терял бы
      // категорию нового места: в массиве `places` его ещё нет.
      const selectedPlace = places.find((place) => place.uuid === parsed.data.place_uuid);
      response = await fetch("/api/proxy/cards", {
        method: "POST",
        headers: { "content-type": "application/json" },
        // `starts_at` уходит ISO-8601 с зоной: datetime-local даёт локальное
        // время без зоны, а сервер требует зону (ADR-16) и отклонит наивное.
        body: JSON.stringify({
          ...parsed.data,
          category: chosenCategory ?? selectedPlace?.category ?? "other",
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

      {/* Стори 13.2 (AC-7, FR130): поля «Что» больше нет. Заголовок собирает
          сервер из выбранной активности и места («Настольные игры в баре
          „Прага“»), поэтому автор не должен ни придумывать его, ни
          редактировать. Плитки активности — стори 13.6. */}
      <Field label="Когда" error={errors.starts_at}>
        <input
          type="datetime-local"
          value={values.starts_at}
          onChange={(event) => update("starts_at", event.target.value)}
          // AC-1 стори 12.5: подсказка «не дальше трёх суток». Серверное
          // правило живёт в `create_card`/`update_card`; браузер лишь
          // подставляет границу в календарь. Удалить этот атрибут безопасно —
          // `422 STARTS_AT_TOO_FAR` всё равно придёт.
          max={maxStartsAt}
          className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
        />
      </Field>

      <div className="flex flex-col gap-2 text-base">
        {/* Стори 13.1 (AC-1, AC-2, AC-4, AC-9): выбор места — справочник
            города плюс поиск по карте OSM через наш прокси. Свободный ввод
            адреса здесь не «открытая форма»: он создаёт место в справочнике
            через `POST /api/v1/address/place`, дедуп — `uq_places_city_name`.
            `places` по-прежнему виден всегда, поэтому отказ карты (503) не
            блокирует создание карточки (AC-4). */}
        <PlacePicker
          places={places}
          value={values.place_uuid}
          // H-3 CR 2026-10-31: ошибка `place_uuid` ОБЯЗАНА доходить до поля.
          // `formSchema` требует место, `submit` пишет «Выберите место из
          // справочника» в `errors.place_uuid`. Пока здесь стоял `undefined`,
          // сообщение уходило в никуда: кнопка нажималась, форма молча не
          // отправлялась, и пользователь не понимал, чего не хватает.
          // Тот же путь у серверного `PLACE_NOT_AVAILABLE`
          // (`ERRORS[...].field = "place_uuid"`).
          error={errors.place_uuid}
          onChange={(place) => {
            // AC-5: уходит только UUID места. Координаты живут в состоянии
            // `PlacePicker` и в `places` не попадают.
            update("place_uuid", place?.uuid ?? "");
            // FR69: категория карточки берётся из места. Для места, созданного
            // по подсказке OSM, категорию принёс `category_hint` (вне enum →
            // `other` уже на сервере).
            setChosenCategory(place?.category ?? "other");
          }}
        />
      </div>

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

      <Field label="Тип встречи" error={errors.kind}>
        {/* AC-3 стори 12.4 (FR117): выбирается ЯВНО. Связи с `capacity` нет
            намеренно: карточка на одного с типом «компания» легальна (AC-4). */}
        <div className="flex gap-2" role="group" aria-label="Тип встречи">
          {([
            { value: "date", label: "Свидание" },
            { value: "company", label: "Компания" },
          ] as const).map((option) => (
            <button
              key={option.value}
              type="button"
              aria-pressed={values.kind === option.value}
              onClick={() => update("kind", option.value)}
              className={`h-11 rounded-md border px-4 ${values.kind === option.value ? "border-[color:var(--accent)] font-semibold" : "border-[color:var(--border-input)]"}`}
            >
              {option.label}
            </button>
          ))}
        </div>
      </Field>

      <Field label="Чем займётесь" error={errors.activity}>
        {/* AC-3 стори 12.4 (FR118): шесть значений белого списка. Легаси
            `other` в форме нет: сервер ответит `422 activity_other_reserved`. */}
        <select
          value={values.activity}
          onChange={(event) => update("activity", event.target.value as "coffee")}
          className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
        >
          <option value="">Выберите занятие</option>
          <option value="coffee">Кофе</option>
          <option value="walk">Прогулка</option>
          <option value="bar">Бар</option>
          <option value="cinema">Кино</option>
          <option value="boardgames">Настольные игры</option>
          <option value="sport">Спорт</option>
        </select>
      </Field>

      {/* Стори 12.6 (AC-2, Task 7.1): рамки аудитории. Мягкий фильтр —
          карточка видна всем, подать заявку может любой (FR81). Все три
          поля необязательны, и «не задано» — законный ответ: тогда в ленте
          рамки не показываются вовсе (UX §1048). */}
      <fieldset className="flex flex-col gap-3 rounded-md border border-[color:var(--border)] p-4">
        <legend className="px-1 text-base font-medium">Кого ищете (необязательно)</legend>
        <p className="text-sm text-[color:var(--ink-secondary)]">
          Карточку увидят все. Рамки подскажут, кого вы ждёте.
        </p>

        <Field label="Пол" error={undefined}>
          <select
            value={values.gender_filter ?? ""}
            onChange={(event) =>
              update(
                "gender_filter",
                (event.target.value || undefined) as "female" | "male" | undefined,
              )
            }
            className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
          >
            {/* «Любой» — именно отсутствие значения, а не третье значение
                перечисления: сервер ждёт `female`/`male`/NULL (CHECK `0038`). */}
            <option value="">Любой</option>
            <option value="female">Женщины</option>
            <option value="male">Мужчины</option>
          </select>
        </Field>

        <div className="flex gap-3">
          <Field label="Возраст от" error={errors.age_min}>
            <input
              type="number"
              inputMode="numeric"
              min={18}
              max={99}
              value={values.age_min ?? ""}
              onChange={(event) =>
                update(
                  "age_min",
                  event.target.value === "" ? undefined : Number(event.target.value),
                )
              }
              className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
            />
          </Field>
          <Field label="до" error={errors.age_max}>
            <input
              type="number"
              inputMode="numeric"
              min={18}
              max={99}
              value={values.age_max ?? ""}
              onChange={(event) =>
                update(
                  "age_max",
                  event.target.value === "" ? undefined : Number(event.target.value),
                )
              }
              className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
            />
          </Field>
        </div>
      </fieldset>

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
        // AC-4: сообщение пришло из ответа API (или сеть недоступна) —
        // действие не выполнено, тон `danger`. Не унифицировать с `Field`.
        <p role="alert" className="text-sm text-danger">
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
        // AC-3 стори 11.9: `Field` показывает только ошибки zod-схемы —
        // незавершённый ввод, который человек исправляет сам. Тон `warning`.
        // Отказы API живут в `state.kind === "error"` ниже и остаются `danger`.
        <span role="alert" className="text-sm text-warning">
          {error}
        </span>
      ) : null}
    </label>
  );
}
