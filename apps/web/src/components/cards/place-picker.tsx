"use client";

/**
 * Выбор места: справочник города + поиск по карте OSM через прокси (стори 13.1).
 *
 * Satisfies: AC-1, AC-2, AC-4, AC-5, AC-8, AC-9, AC-11.
 *
 * **Почему поиск идёт на наш сервер, а не в Nominatim напрямую (AC-2).**
 * Правило OSM требует `User-Agent` с контактом; из браузера он ушёл бы с UA
 * браузера, и за нарушение блокировали бы наш IP. Плюс лимит 1 req/s осмыслен
 * только там, где видны ВСЕ запросы. Поэтому единственный исходящий адрес
 * клиента — `/api/proxy/address/search`.
 *
 * **Почему Leaflet грузится динамически внутри эффекта (AC-1).** `leaflet`
 * обращается к `window` при импорте: статический импорт уронил бы серверный
 * рендер шага 2. `next/dynamic` + `ssr: false` в проекте не применялся ни разу,
 * а `cacheComponents: true` (`next.config.ts:7`) в 11.2 ломал `export const
 * dynamic`; проверять гипотезу на живом экране дороже, чем загрузить модуль
 * в `useEffect`, который на сервере не выполняется вообще.
 *
 * **Координаты (AC-5).** `lat`/`lng` живут только в состоянии этого компонента
 * и в ответе `/address/search`. В `places` уходят `name`/`address_text`.
 */

import { useEffect, useRef, useState } from "react";

/** Место из справочника — контракт `GET /api/v1/places` (3.3). */
export type PlaceOption = {
  uuid: string;
  name: string;
  address_text: string;
  category: string;
};

/** AC-5: элемент ответа `/address/search`. `lng`, а не `lon` — см. сервер. */
export type AddressSuggestion = {
  name: string;
  address_text: string;
  display_name: string;
  category_hint: string;
  lat: number;
  lng: number;
};

/** AC-10: сервер требует ≥2 символов, клиент не шлёт заведомо короткое. */
export const MIN_QUERY_LENGTH = 2;

/** AC-4, Task 6.2: дебаунс 400 мс — иначе каждый символ был бы запросом. */
export const SEARCH_DEBOUNCE_MS = 400;

/** Текст деградации — `ux-design-v2:913`, дословно. */
export const GEO_UNAVAILABLE_TEXT =
  "Поиск по карте сейчас не работает. Введите адрес вручную — карточку можно создать.";

export const NOTHING_FOUND_TEXT =
  "Ничего не нашли. Проверьте название или введите адрес вручную.";

/** Решение 3: скрытое место — реактивации нет, автор выбирает другое. */
export const PLACE_HIDDEN_TEXT = "Место скрыто, выберите другое";

type SearchState =
  | { kind: "idle" }
  | { kind: "searching" }
  | { kind: "results"; items: AddressSuggestion[] }
  | { kind: "empty" }
  | { kind: "unavailable" }
  | { kind: "failed"; message: string };

/**
 * Миникарта (AC-1, AC-8; H-1 CR 2026-10-31).
 *
 * **Карта видна СРАЗУ, до выбора места.** Первая редакция рисовала её только
 * при `chosen !== null` — то есть после клика по подсказке. AC-1 требует карту
 * «при открытии шага 2», и разница не косметическая: без карты автор не видит
 * город и не понимает, где искать; атрибуция «© OpenStreetMap» тоже
 * отсутствовала до выбора, хотя лицензия ODbL требует её всегда, пока тайлы
 * на экране.
 *
 * **Маркер необязателен.** До выбора `marker = null` — карта центрована по
 * городу автора (`GET /address/city` → `viewbox`). После выбора ставится точка.
 *
 * `role="img"` + `aria-label`: карта — визуальный образ, и человек с экранным
 * диктором должен знать, что она показывает (NFR57 — статус текстом).
 */
function MiniMap({ center, marker }: { center: [number, number]; marker: [number, number] | null }) {
  const container = useRef<HTMLDivElement | null>(null);
  const instance = useRef<import("leaflet").Map | null>(null);
  const pin = useRef<import("leaflet").Marker | null>(null);
  const [lat, lng] = center;

  useEffect(() => {
    let cancelled = false;

    void (async () => {
      // AC-1: модуль грузится только в браузере — серверный рендер шага 2
      // не содержит Leaflet вовсе (P-25: гард по выводу сборки).
      const leaflet = (await import("leaflet")).default;
      if (cancelled || container.current === null || instance.current !== null) return;
      instance.current = leaflet.map(container.current, { scrollWheelZoom: false }).setView([lat, lng], 13);
      // AC-8: тайлы OSM и обязательная атрибуция — лицензия ODbL, не декор.
      leaflet
        .tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
          attribution: "© OpenStreetMap contributors",
          maxZoom: 19,
        })
        .addTo(instance.current);
    })();

    return () => {
      cancelled = true;
      instance.current?.remove();
      instance.current = null;
      pin.current = null;
    };
    // `lat`/`lng` НЕ в зависимостях: карта создаётся один раз, дальше
    // двигается `setView` во втором эффекте. Пересоздание на каждый центр
    // роняло бы тайлы и сбрасывало зум при каждом поиске.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (instance.current === null) return;
    void (async () => {
      const leaflet = (await import("leaflet")).default;
      if (instance.current === null) return;
      instance.current.setView([lat, lng], instance.current.getZoom() || 13);
      if (marker === null) {
        pin.current?.remove();
        pin.current = null;
        return;
      }
      if (pin.current === null) {
        pin.current = leaflet.marker(marker).addTo(instance.current);
      } else {
        pin.current.setLatLng(marker);
      }
    })();
  }, [lat, lng, marker]);

  return (
    <div
      ref={container}
      role="img"
      aria-label={
        marker === null
          ? `Карта города: ${lat.toFixed(4)}, ${lng.toFixed(4)}`
          : `Карта места: ${marker[0].toFixed(4)}, ${marker[1].toFixed(4)}`
      }
      className="h-40 w-full rounded-md border border-[color:var(--border-input)]"
    />
  );
}

/**
 * H-1: центр карты из `viewbox` города — `<lng_min>,<lat_max>,<lng_max>,<lat_min>`.
 *
 * Порядок задан форматом Nominatim, а не нашим `pilot_city_bbox` (там
 * `lat_min,lat_max,lng_min,lng_max`): перепутанные оси дали бы карту
 * «в Атлантике» без единой ошибки. Разбор строкой, а не полями `lat`/`lng`,
 * потому что `GET /address/city` отдаёт рамку — координат объекта в ней нет.
 *
 * `null` → центр мира: города без рамки существуют (`W-13.1-f`), и врать
 * центром Томска нельзя.
 */
export function centerFromViewbox(viewbox: string | null): [number, number] {
  if (viewbox === null) return [20, 0];
  const parts = viewbox.split(",").map((part) => Number.parseFloat(part.trim()));
  if (parts.length !== 4 || parts.some((value) => !Number.isFinite(value))) return [20, 0];
  const [lngMin, latMax, lngMax, latMin] = parts;
  return [(latMin + latMax) / 2, (lngMin + lngMax) / 2];
}

type Props = {
  /** Справочник города — виден ВСЕГДА, даже когда карта недоступна (AC-4). */
  places: PlaceOption[];
  value: string;
  onChange: (place: PlaceOption | null) => void;
  error?: string;
  /**
   * Метка поля места. По умолчанию «Где» — так поле называлось до 13.1
   * (`<Field label="Где">`), и тесты формы ищут именно эту метку.
   *
   * **Почему метка живёт здесь, а не в `<Field>`.** `Field` оборачивает
   * содержимое в `<label>`, а внутри выбора места теперь НЕСКОЛЬКО контролов
   * (поиск, ручной ввод, справочник). Обёртка `label` связала бы метку со
   * всеми сразу: `getByLabelText("Где")` стал бы неоднозначным, а скринридер
   * прочитал бы «Где» трижды на разных полях. Здесь `label` привязан к
   * `id` ровно одного элемента — справочника, который и есть выбор места.
   */
  label?: string;
};

/** id единственного поля, к которому привязана метка (см. `label`). */
const PLACE_SELECT_ID = "place-uuid";

export function PlacePicker({ places, value, onChange, error, label = "Где" }: Props) {
  const [query, setQuery] = useState("");
  const [state, setState] = useState<SearchState>({ kind: "idle" });
  const [chosen, setChosen] = useState<AddressSuggestion | null>(null);
  /**
   * H-1 CR 2026-10-31: центр карты — город автора, а не точка выбора.
   *
   * `null` до ответа `/address/city`: карта тогда покажет мир. Врать
   * координатами Томска нельзя — при втором городе карта открылась бы
   * в чужом месте, и автор искал бы своё заведение не там.
   */
  const [center, setCenter] = useState<[number, number] | null>(null);
  // AC-4: текст, введённый до отказа, не теряется — поля ручного ввода
  // заполняются им же, а не очищаются.
  const [manualName, setManualName] = useState("");
  const [manualAddress, setManualAddress] = useState("");
  const [manualOpen, setManualOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  // H-1: рамка города автора. Один запрос на монтирование: значение меняется
  // только при смене профиля, а не при каждом поиске.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch("/api/proxy/address/city", {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (cancelled || response.status !== 200) return;
        const body = (await response.json()) as { viewbox: string | null };
        setCenter(centerFromViewbox(body.viewbox));
      } catch {
        // Сеть недоступна — карта покажет мир, поиск всё равно работает.
        // Это не ошибка пользователя (AC-4: деградация, не отказ).
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const trimmed = query.trim();
    // Короткий запрос не уходит в сеть: сброс состояния сделан в `onChange`,
    // а не здесь. Синхронный `setState` в теле эффекта вызывает каскадный
    // рендер, и правило `react-hooks/set-state-in-effect` ловит это верно:
    // сброс — следствие ДЕЙСТВИЯ пользователя, а не синхронизации с внешней
    // системой. Внутри таймера `setState` уже асинхронный и правило не нарушает.
    if (trimmed.length < MIN_QUERY_LENGTH) {
      return;
    }

    // AC-10: сокращение запроса отменяет предыдущий — иначе ответ на прошлый
    // ввод перезаписал бы список результатов более нового.
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void (async () => {
        setState({ kind: "searching" });
        try {
          const response = await fetch(
            `/api/proxy/address/search?q=${encodeURIComponent(trimmed)}`,
            { signal: controller.signal, credentials: "same-origin", cache: "no-store" },
          );
          if (response.status === 200) {
            const items = (await response.json()) as AddressSuggestion[];
            setState(items.length === 0 ? { kind: "empty" } : { kind: "results", items });
            return;
          }
          if (response.status === 503) {
            // AC-4: наш собственный лимит и недоступность OSM — один статус.
            setState({ kind: "unavailable" });
            setManualOpen(true);
            setManualName(trimmed);
            return;
          }
          setState({
            kind: "failed",
            message: "Не удалось выполнить поиск. Введите адрес вручную.",
          });
          setManualOpen(true);
        } catch (error) {
          if ((error as Error).name === "AbortError") return;
          setState({
            kind: "failed",
            message: "Не удалось выполнить поиск. Введите адрес вручную.",
          });
          setManualOpen(true);
        }
      })();
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  /**
   * AC-9: автосоздание места по выбранному результату.
   *
   * Отдельный запрос обязателен: карточка ссылается на `place_uuid`, а поиск
   * отдаёт координаты и текст — места в справочнике ещё нет. `city_slug` не
   * отправляется: сервер берёт его из профиля автора (Task 3.4).
   */
  async function savePlace(
    input: { name: string; address_text: string; category_hint?: string },
  ): Promise<void> {
    setBusy(true);
    try {
      const response = await fetch("/api/proxy/address/place", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(input),
        credentials: "same-origin",
        cache: "no-store",
      });
      if (response.status === 200) {
        const body = (await response.json()) as { place_uuid: string };
        onChange({
          uuid: body.place_uuid,
          name: input.name,
          address_text: input.address_text,
          category: input.category_hint ?? "other",
        });
        setManualOpen(false);
        return;
      }
      const payload = (await response.json().catch(() => ({}))) as { code?: string };
      if (payload.code === "PLACE_HIDDEN") {
        // Решение 3: без `existing_uuid` — перебором имён нельзя узнать,
        // какие скрытые места существуют.
        setState({ kind: "failed", message: PLACE_HIDDEN_TEXT });
        return;
      }
      setState({ kind: "failed", message: "Не удалось сохранить место. Попробуйте снова." });
    } catch {
      setState({ kind: "failed", message: "Не удалось сохранить место. Попробуйте снова." });
    } finally {
      setBusy(false);
    }
  }

  function chooseSuggestion(suggestion: AddressSuggestion) {
    // AC-5: координаты остаются в состоянии компонента — на сервер (в `places`)
    // уходят только `name` и `address_text`.
    setChosen(suggestion);
    void savePlace({
      name: suggestion.name,
      address_text: suggestion.address_text,
      category_hint: suggestion.category_hint,
    });
  }

  return (
    <div className="flex flex-col gap-3">
      {/* AC-2: единственный исходящий адрес — наш прокси. */}
      <input
        type="search"
        value={query}
        onChange={(event) => {
          const next = event.target.value;
          setQuery(next);
          // Сброс — следствие действия пользователя, а не синхронизации
          // с внешней системой: держим его здесь, чтобы эффект оставался
          // чистым и не вызывал каскадный рендер.
          if (next.trim().length < MIN_QUERY_LENGTH) {
            setState({ kind: "idle" });
          }
        }}
        placeholder="Название или адрес — начните вводить"
        aria-label="Поиск места на карте"
        className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
      />

      {state.kind === "searching" ? (
        <p aria-busy="true" className="text-sm text-ink-secondary">
          Ищем…
        </p>
      ) : null}

      {state.kind === "results" ? (
        <ul className="flex flex-col gap-1">
          {state.items.map((item) => (
            <li key={`${item.lat},${item.lng},${item.name}`}>
              <button
                type="button"
                disabled={busy}
                onClick={() => chooseSuggestion(item)}
                className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2 text-left"
              >
                <span className="block font-medium">{item.name}</span>
                <span className="block text-sm text-ink-secondary">{item.display_name}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      {state.kind === "empty" ? (
        <p className="text-sm text-warning">{NOTHING_FOUND_TEXT}</p>
      ) : null}

      {state.kind === "unavailable" ? (
        // AC-4: отказ, не ошибка ввода — `warning`, действие доступно.
        <p role="alert" className="text-sm text-warning">
          {GEO_UNAVAILABLE_TEXT}
        </p>
      ) : null}

      {state.kind === "failed" ? (
        <p role="alert" className="text-sm text-danger">
          {state.message}
        </p>
      ) : null}

      {/* H-1 CR 2026-10-31 / AC-1: карта видна СРАЗУ, до выбора места.
          Центр — город автора; маркер появляется после выбора подсказки.
          Атрибуция «© OpenStreetMap contributors» живёт в тайлах и видна
          всегда, пока карта на экране (AC-8, лицензия ODbL). */}
      <MiniMap
        center={chosen !== null ? [chosen.lat, chosen.lng] : (center ?? [20, 0])}
        marker={chosen === null ? null : [chosen.lat, chosen.lng]}
      />

      {/* AC-4: ручной ввод доступен всегда — карточку можно создать даже
          без карты. Поля показываются по требованию, но не исчезают. */}
      <button
        type="button"
        onClick={() => setManualOpen((open) => !open)}
        className="self-start text-sm underline text-ink-secondary"
      >
        {manualOpen ? "Скрыть ручной ввод" : "Ввести адрес вручную"}
      </button>

      {manualOpen ? (
        <div className="flex flex-col gap-2 rounded-md border border-[color:var(--border-input)] p-3">
          <input
            type="text"
            value={manualName}
            onChange={(event) => setManualName(event.target.value)}
            placeholder="Название места"
            aria-label="Название места"
            className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
          />
          <input
            type="text"
            value={manualAddress}
            onChange={(event) => setManualAddress(event.target.value)}
            placeholder="Адрес"
            aria-label="Адрес места"
            className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
          />
          <button
            type="button"
            disabled={busy || manualName.trim() === "" || manualAddress.trim() === ""}
            onClick={() =>
              void savePlace({
                name: manualName.trim(),
                address_text: manualAddress.trim(),
              })
            }
            className="h-11 rounded-md bg-[color:var(--accent)] px-4 font-semibold text-[color:var(--accent-ink)] disabled:opacity-60"
          >
            Указать это место
          </button>
        </div>
      ) : null}

      {/* AC-4: справочник виден ВСЕГДА — даже когда поиск недоступен. */}
      <label htmlFor={PLACE_SELECT_ID} className="font-medium">
        {label}
      </label>
      <select
        id={PLACE_SELECT_ID}
        value={value}
        onChange={(event) => {
          const selected = places.find((place) => place.uuid === event.target.value) ?? null;
          onChange(selected);
        }}
        className="w-full rounded-md border border-[color:var(--border-input)] px-3 py-2"
      >
        <option value="">Или выберите из справочника</option>
        {places.map((place) => (
          <option key={place.uuid} value={place.uuid}>
            {place.name} — {place.address_text}
          </option>
        ))}
      </select>

      {error ? (
        <span role="alert" className="text-sm text-warning">
          {error}
        </span>
      ) : null}
    </div>
  );
}
