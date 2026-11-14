/**
 * Рамки аудитории в форме создания карточки (стори 12.6, AC-2, Task 7.1–7.2).
 *
 * **Проверяется то, чего не хватало.** CR 12.6 §4 замерил: `grep` по `src`
 * давал 0 совпадений по `gender_filter`/`age_min` — автор не мог задать
 * рамки через интерфейс, фича была недостижима без прямого HTTP-вызова.
 * Здесь три вещи, каждая из которых ломается независимо:
 *
 * - **задаются** — значения доезжают до payload (без этого поля висят в форме
 *   и не уходят на сервер: «нарисовано, но не работает»);
 * - **очищаются** — пустое поле не отправляется, иначе снятая рамка
 *   вернулась бы как `undefined`-в-число на сервере (класс 12.2 `age`);
 * - **перевёрнутая пара** даёт inline-ошибку ДО сети — иначе человек узнаёт
 *   о `422 age_range_invalid` после отправки, хотя видит обе цифры сразу.
 *
 * Мягкий фильтр (FR81, ADR-19): рамки никого не отсекают, карточка видна
 * всем, поэтому все три поля необязательны и пустая форма валидна.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CardCreateForm } from "@/components/cards/card-create-form";

const PLACES = [
  {
    uuid: "11111111-1111-1111-1111-111111111111",
    name: "Кофейня",
    address_text: "пр. Ленина, 1",
    category: "cafe",
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * Заполняет обязательные поля, чтобы форма дошла до проверки рамок.
 * Рамки — единственное, что различается между тестами ниже.
 */
async function fillRequired() {
  // Стори 13.2 (AC-7, FR130): поля «Что» больше нет — заголовок собирает
  // сервер из активности и места. Заполняются только существующие поля.
  await userEvent.type(screen.getByLabelText("Когда"), "2030-01-01T18:00");
  await userEvent.selectOptions(screen.getByLabelText("Где"), PLACES[0].uuid);
  await userEvent.click(screen.getByRole("button", { name: "Компания" }));
  await userEvent.selectOptions(screen.getByLabelText("Чем займётесь"), "cinema");
}

/**
 * Вызовы `fetch`, которые относятся к СОЗДАНИЮ карточки.
 *
 * С H-1 (`PlacePicker` грузит центр карты) форма на монтировании шлёт
 * `/api/proxy/address/city`. Прежние ассерты `toHaveBeenCalledTimes(1)` и
 * `mock.calls[0]` считали ВСЕ запросы и краснели на верном коде: нулевым
 * вызовом шёл бы город, а не карточка. Фильтр по адресу делает тест
 * устойчивым к любым новым фоновым запросам формы.
 */
function cardCalls(fetchMock: ReturnType<typeof vi.fn>): unknown[][] {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes("/api/proxy/cards"));
}

describe("CardCreateForm — рамки аудитории (AC-2, Task 7.1–7.2)", () => {
  it("рамки заданы — уходят в payload как `gender_filter`/`age_min`/`age_max`", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 201,
      ok: true,
      json: async () => ({ uuid: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", status: "draft" }),
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<CardCreateForm places={PLACES} />);

    await fillRequired();
    await userEvent.selectOptions(screen.getByLabelText("Пол"), "female");
    // `type="number"` + `Number(event.target.value)` — границы уходят числами,
    // не строками: сервер ждёт `int`, и `"25"` дал бы `422`.
    await userEvent.type(screen.getByLabelText("Возраст от"), "25");
    await userEvent.type(screen.getByLabelText("до"), "35");

    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));

    expect(cardCalls(fetchMock)).toHaveLength(1);
    const body = JSON.parse(
      (cardCalls(fetchMock)[0]?.[1] as { body: string }).body,
    ) as Record<string, unknown>;
    expect(body.gender_filter).toBe("female");
    expect(body.age_min).toBe(25);
    expect(body.age_max).toBe(35);
    // Стори 13.2 (AC-7, CR 13.2 H-5, Task 3b.3): в теле НЕТ ключа `title`.
    //
    // **Почему явный ассерт, а не косвенный.** До CR возврат поля в форму
    // ловился падением рендера (таймаут поиска элемента «Что»), и сообщение
    // говорило о таймауте, а не о том, что на сервер ушёл `title`. Явная
    // проверка тела даёт точную диагностику ПРИЧИНЫ: сервер отвергает
    // присланный заголовок `422 title_not_accepted` (AC-7), значит форма
    // обязана его не отправлять.
    //
    // Краснеет, если вернуть `title` в zod-схему и `values`: ключ появится
    // в теле, и создание карточки в продукте сломается.
    expect(body).not.toHaveProperty("title");
  });

  it("рамки очищены — в payload их НЕТ (снятая рамка не возвращается)", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 201,
      ok: true,
      json: async () => ({ uuid: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", status: "draft" }),
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<CardCreateForm places={PLACES} />);

    await fillRequired();
    // Задали и сняли: поле прошло через `undefined` и вернулось в пустое.
    await userEvent.type(screen.getByLabelText("Возраст от"), "30");
    await userEvent.clear(screen.getByLabelText("Возраст от"));

    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));

    expect(cardCalls(fetchMock)).toHaveLength(1);
    const body = JSON.parse(
      (cardCalls(fetchMock)[0]?.[1] as { body: string }).body,
    ) as Record<string, unknown>;
    // `undefined` в `JSON.stringify` не попадает в объект вовсе — поэтому
    // проверяется отсутствие ключа, а не его значение: `null` сервер понял бы
    // как «рамка есть, но пустая».
    expect(body).not.toHaveProperty("age_min");
    expect(body).not.toHaveProperty("gender_filter");
  });

  it("перевёрнутая пара — inline-ошибка ДО сети, запрос не уходит", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<CardCreateForm places={PLACES} />);

    await fillRequired();
    await userEvent.type(screen.getByLabelText("Возраст от"), "40");
    await userEvent.type(screen.getByLabelText("до"), "30");

    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));

    // Текст — человеческий, не техническое `age_range_invalid` (UX §1052).
    expect(await screen.findByText("Нижняя граница больше верхней")).toBeInTheDocument();
    // Человек видит ошибку на своих же цифрах — гонять его за `422` незачем.
    // С H-1 форма грузит центр карты на монтировании: проверяем, что не ушёл
    // именно ЗАПРОС КАРТОЧКИ (валидация отбила его до сети).
    expect(cardCalls(fetchMock)).toEqual([]);
  });

  it("прямая пара проходит — пара к предыдущему (иначе `<=` не отличить от запрета)", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 201,
      ok: true,
      json: async () => ({ uuid: "cccccccc-cccc-cccc-cccc-cccccccccccc", status: "draft" }),
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<CardCreateForm places={PLACES} />);

    await fillRequired();
    await userEvent.type(screen.getByLabelText("Возраст от"), "25");
    await userEvent.type(screen.getByLabelText("до"), "35");

    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));

    expect(screen.queryByText("Нижняя граница больше верхней")).toBeNull();
    expect(cardCalls(fetchMock)).toHaveLength(1);
  });

  it("полуоткрытая пара «от 30» валидна (AC-1a): вторая граница не обязательна", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 201,
      ok: true,
      json: async () => ({ uuid: "dddddddd-dddd-dddd-dddd-dddddddddddd", status: "draft" }),
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<CardCreateForm places={PLACES} />);

    await fillRequired();
    await userEvent.type(screen.getByLabelText("Возраст от"), "30");

    await userEvent.click(screen.getByRole("button", { name: /Опубликовать/ }));

    expect(screen.queryByText("Нижняя граница больше верхней")).toBeNull();
    expect(cardCalls(fetchMock)).toHaveLength(1);
  });
});
