/**
 * Клиент заявок на карточку режима «заявки» (стори 11.5; AC-2, AC-5, AC-8).
 *
 * **Почему отдельный тест на две функции.** `submitJoinRequest` и
 * `cancelJoinRequest` — единственные точки выхода к серверу для FR76/FR82, и
 * ошибка в пути или методе (`/join` вместо `/requests`, `POST` вместо
 * `DELETE`) не проявится ни в тестах компонентов (они мокают эти функции),
 * ни в `tsc`. Проверяются ровно три вещи: URL, метод, тело.
 *
 * **Почему мок `fetch`, а не MSW.** В проекте `fetch` мокают напрямую
 * (`chat-screen.test.tsx`); поднимать обработчик сервера ради двух вызовов
 * значило бы добавить зависимость ради проверки строки.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { cancelJoinRequest, submitJoinRequest } from "@/lib/queries/cards";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

const UUID = "22222222-2222-2222-2222-222222222222";
const REQUEST_UUID = "33333333-3333-3333-3333-333333333333";

describe("submitJoinRequest", () => {
  it("шлёт POST на /requests с сообщением в теле (AC-2)", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 201 }));

    const result = await submitJoinRequest(UUID, "Привет");

    expect(result).toEqual({ ok: true, data: null });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/proxy/cards/${UUID}/requests`);
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ message: "Привет" }));
    // Без `content-type` BFF получит строку вместо JSON и ответит 422.
    expect((init.headers as Record<string, string>)["content-type"]).toBe("application/json");
  });

  it("доменный код отказа доходит до экрана как есть (AC-4)", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ code: "JOIN_REQUEST_LIMIT_REACHED" }), { status: 409 }),
    );

    const result = await submitJoinRequest(UUID, "Привет");

    expect(result).toEqual({ ok: false, code: "JOIN_REQUEST_LIMIT_REACHED" });
  });

  it("сетевой сбой даёт network_error, а не исключение (AC-8)", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const result = await submitJoinRequest(UUID, "Привет");

    expect(result).toEqual({ ok: false, code: "network_error" });
  });

  it("ответ без кода отдаёт http_<status>, а не пустую строку", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 502 }));

    const result = await submitJoinRequest(UUID, "Привет");

    expect(result).toEqual({ ok: false, code: "http_502" });
  });
});

describe("cancelJoinRequest", () => {
  it("шлёт DELETE на /requests/{uuid} и не разбирает пустое тело (AC-5)", async () => {
    // `204` с телом нельзя: `Response.json()` на пустой строке бросает.
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    const result = await cancelJoinRequest(REQUEST_UUID);

    expect(result).toEqual({ ok: true, data: null });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/proxy/requests/${REQUEST_UUID}`);
    expect(init.method).toBe("DELETE");
  });

  it("отмена выбранной заявки возвращает машинный код (AC-5)", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ code: "REQUEST_SELECTED" }), { status: 409 }),
    );

    const result = await cancelJoinRequest(REQUEST_UUID);

    expect(result).toEqual({ ok: false, code: "REQUEST_SELECTED" });
  });

  it("сетевой сбой даёт network_error (AC-8)", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const result = await cancelJoinRequest(REQUEST_UUID);

    expect(result).toEqual({ ok: false, code: "network_error" });
  });
});
