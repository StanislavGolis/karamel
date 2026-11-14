/**
 * Выбор места: карта OSM через прокси (стори 13.1).
 *
 * Satisfies: AC-2, AC-4, AC-5, AC-9.
 *
 * **Почему проверяется URL, а не факт `fetch`.** AC-2 требует, чтобы клиент
 * НЕ ходил в Nominatim напрямую: `User-Agent` с контактом из браузера не
 * уйдёт, и за нарушение правила OSM заблокировали бы наш IP. Тест «fetch был
 * вызван» остался бы зелёным при прямом запросе к `nominatim.openstreetmap.org`
 * — он и был бы релей с чужого адреса.
 *
 * **Почему `503` проверяется вместе с сохранением текста.** AC-4 требует
 * деградации, а не отказа: человек должен увидеть свой запрос в поле ручного
 * ввода и создать карточку. Тест только на текст ошибки пропустил бы
 * очистку поля — форма формально «сообщила», а работать с ней уже нельзя.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  GEO_UNAVAILABLE_TEXT,
  PLACE_HIDDEN_TEXT,
  PlacePicker,
  centerFromViewbox,
  type AddressSuggestion,
  type PlaceOption,
} from "@/components/cards/place-picker";

const PLACES: PlaceOption[] = [
  {
    uuid: "11111111-1111-1111-1111-111111111111",
    name: "Кофейня",
    address_text: "пр. Ленина, 1",
    category: "cafe",
  },
];

const SUGGESTION: AddressSuggestion = {
  name: "Кинотеатр «Прага»",
  address_text: "ул. Ленина, 12",
  display_name: "Кинотеатр «Прага», улица Ленина, 12, Томск, Россия",
  category_hint: "cinema",
  lat: 56.4977,
  lng: 84.9481,
};

/**
 * Роутинг по URL: с H-1 клиент шлёт ДВА вида запросов (`/address/city` при
 * монтировании и `/address/search` при вводе). Последовательные
 * `mockResolvedValueOnce` привязывали ответ к ПОРЯДКУ вызовов: стоило
 * добавить один запрос — и тест начал бы отдавать результаты поиска
 * городу, а тест «скрытое место» получал бы `200 []` вместо подсказки.
 * Роутинг по адресу делает тест устойчивым к порядку и к новым запросам.
 */
function routedFetch(handlers: {
  city?: Response;
  search?: Response | (() => Response);
  place?: Response;
}): ReturnType<typeof vi.fn> {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/address/city')) {
      return handlers.city ?? jsonResponse(200, { viewbox: "84.80,56.60,85.20,56.30" });
    }
    if (url.includes('/address/search')) {
      const response = handlers.search ?? jsonResponse(200, []);
      return typeof response === "function" ? response() : response;
    }
    if (url.includes('/address/place')) {
      return handlers.place ?? jsonResponse(200, { place_uuid: "abc", created: true });
    }
    throw new Error(`неожиданный запрос: ${url}`);
  });
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PlacePicker: координаты и прокси (AC-2, AC-5)", () => {
  it("единственный исходящий адрес — наш прокси, координаты остаются в состоянии", async () => {
    // Карта грузится динамически и в jsdom не нужна: проверяем запросы.
    const fetchMock = routedFetch({ search: jsonResponse(200, [SUGGESTION]) });
    vi.stubGlobal("fetch", fetchMock);
    const onChange = vi.fn();

    render(<PlacePicker places={PLACES} value="" onChange={onChange} />);
    await userEvent.type(screen.getByLabelText("Поиск места на карте"), "Прага");

    const item = await screen.findByText(SUGGESTION.name, {}, { timeout: 3000 });
    expect(item).toBeInTheDocument();

    // AC-2: адрес — наш BFF. Прямой запрос к OSM означал бы релей с UA браузера.
    // С H-1 клиент шлёт и `/address/city` (центр карты). Здесь важно не
    // «сколько всего запросов», а что ПОИСК не ходит в OSM напрямую:
    // прямой запрос к `nominatim.openstreetmap.org` пошёл бы с UA браузера
    // и стоил бы нам блокировки IP (AC-2).
    const allUrls = fetchMock.mock.calls.map((call) => String(call[0]));
    const searchUrls = allUrls.filter((url) => url.includes("/address/search"));
    expect(searchUrls.length).toBeGreaterThan(0);
    for (const url of searchUrls) {
      expect(url.startsWith("/api/proxy/address/search")).toBe(true);
    }
    for (const url of allUrls) {
      expect(url).not.toContain("nominatim.openstreetmap.org");
    }

    await userEvent.click(screen.getByRole("button", { name: new RegExp(SUGGESTION.name) }));

    // AC-5: в место уходят только имя и адрес. Координаты — в состоянии
    // компонента и в ответе `/address/search`, не в `places`.
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const chosen = onChange.mock.calls.at(-1)?.[0] as PlaceOption;
    expect(Object.keys(chosen).sort()).toEqual(["address_text", "category", "name", "uuid"]);
    expect(chosen).not.toHaveProperty("lat");
    expect(chosen).not.toHaveProperty("lng");
  });

  it("короткий запрос не уходит в сеть", async () => {
    const fetchMock = routedFetch({});
    vi.stubGlobal("fetch", fetchMock);

    render(<PlacePicker places={PLACES} value="" onChange={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Поиск места на карте"), "П");

    // Один символ — не ПОИСКОВЫЙ запрос: сервер ответил бы
    // `422 QUERY_TOO_SHORT`, и каждый символ в поле давал бы отказ.
    // Запрос центра карты (`/address/city`) законен и к длине ввода
    // отношения не имеет — иначе тест краснел бы на верном коде.
    await new Promise((resolve) => setTimeout(resolve, 600));
    const searchCalls = fetchMock.mock.calls
      .map((call) => String(call[0]))
      .filter((url) => url.includes("/address/search"));
    expect(searchCalls).toEqual([]);
  });
});

describe("PlacePicker: деградация (AC-4)", () => {
  it("503 — текст сохранён, ручной ввод открыт, справочник виден", async () => {
    const fetchMock = routedFetch({ search: jsonResponse(503, { code: "GEO_UNAVAILABLE" }) });
    vi.stubGlobal("fetch", fetchMock);

    render(<PlacePicker places={PLACES} value="" onChange={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Поиск места на карте"), "Прага");

    expect(await screen.findByText(GEO_UNAVAILABLE_TEXT)).toBeInTheDocument();

    // AC-4: введённый текст не потерян — он в поле ручного ввода.
    const manualName = screen.getByLabelText("Название места") as HTMLInputElement;
    expect(manualName.value).toBe("Прага");

    // Справочник остаётся доступным: карточку можно создать без карты.
    expect(screen.getByLabelText("Где")).toBeInTheDocument();
  });

  it("пустой результат — подсказка про ручной ввод", async () => {
    vi.stubGlobal("fetch", routedFetch({ search: jsonResponse(200, []) }));

    render(<PlacePicker places={PLACES} value="" onChange={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Поиск места на карте"), "Прага");

    expect(
      await screen.findByText("Ничего не нашли. Проверьте название или введите адрес вручную."),
    ).toBeInTheDocument();
  });
});

describe("PlacePicker: скрытое место (AC-9, решение 3)", () => {
  it("422 PLACE_HIDDEN — текст без UUID скрытого места", async () => {
    const fetchMock = routedFetch({
      search: jsonResponse(200, [SUGGESTION]),
      place: jsonResponse(422, { code: "PLACE_HIDDEN" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const onChange = vi.fn();

    render(<PlacePicker places={PLACES} value="" onChange={onChange} />);
    await userEvent.type(screen.getByLabelText("Поиск места на карте"), "Прага");
    await userEvent.click(
      await screen.findByRole("button", { name: new RegExp(SUGGESTION.name) }),
    );

    expect(await screen.findByText(PLACE_HIDDEN_TEXT)).toBeInTheDocument();
    // Скрытое место не выбрано: карточка не должна уйти с чужим UUID.
    expect(onChange).not.toHaveBeenCalled();
  });
});


describe("PlacePicker: карта до выбора (AC-1, H-1 CR)", () => {
  it("карта присутствует в DOM ДО выбора места", async () => {
    // Краснеет, если вернуть `chosen !== null ? <MiniMap/> : null`: до выбора
    // карты нет, а AC-1 требует её «при открытии шага 2». Именно так дефект
    // H-1 и жил — ни один тест не смотрел на карту.
    vi.stubGlobal("fetch", routedFetch({}));

    render(<PlacePicker places={PLACES} value="" onChange={vi.fn()} />);

    const map = await screen.findByRole("img", { name: /Карта города/ });
    expect(map).toBeInTheDocument();
    expect(map.className).toContain("h-40");
  });

  it("центр карты — город автора из viewbox, а не мировая точка", async () => {
    // Краснеет, если `centerFromViewbox` перепутает оси: рамка Томска
    // даёт центр (56.45, 85.00). При перепутанных осях карта открылась бы
    // в Атлантике, и автор искал бы место не там.
    vi.stubGlobal("fetch", routedFetch({}));

    render(<PlacePicker places={PLACES} value="" onChange={vi.fn()} />);

    const map = await screen.findByRole("img", { name: /Карта города/ });
    expect(map.getAttribute("aria-label")).toContain("56.45");
    expect(map.getAttribute("aria-label")).toContain("85.00");
  });
});

describe("centerFromViewbox (AC-10, H-1)", () => {
  it("центр рамки — середина по обеим осям", () => {
    expect(centerFromViewbox("84.80,56.60,85.20,56.30")).toEqual([56.45, 85]);
  });

  it("нет рамки → мировой центр, а не координаты Томска", () => {
    // Краснеет, если подставлять «дефолтный город»: при втором городе карта
    // открывалась бы в чужом месте.
    expect(centerFromViewbox(null)).toEqual([20, 0]);
  });

  it("мусор в рамке → мировой центр, а не NaN", () => {
    // `NaN` в `setView` роняет Leaflet исключением, и карта исчезает молча.
    expect(centerFromViewbox("a,b,c,d")).toEqual([20, 0]);
    expect(centerFromViewbox("1,2,3")).toEqual([20, 0]);
  });
});
