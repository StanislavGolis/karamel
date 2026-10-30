/**
 * Загрузчик карточки модератора: разбор ответов API (стори 12.10; AC-7).
 *
 * **Почему отдельная функция, а не код внутри компонента** (`W-12.7-l`):
 * состояния `200`/`403`/`404`/`5xx`/сеть различаются ДЕЙСТВИЕМ читателя, и
 * проверять их через рендер значит связывать утверждение о разборе ответа с
 * разметкой. Функция тестируется с подменённым `fetch`, рендер — отдельно.
 *
 * **`403` ≠ `404` ≠ `unavailable`** (P-26, AC-7). Слить их в одно состояние
 * значило бы отправить модератора обновлять страницу там, где дело в его
 * роли или в опечатке в адресе.
 */

import { describe, expect, it, vi } from "vitest";

import { loadUserCard } from "@/app/(mod)/mod/users/[uuid]/page";

const UUID = "11111111-2222-3333-4444-555555555555";

/** Ответ карточки в объёме контракта 8.5 + 11.4 + 12.10. */
function cardBody(overrides: Record<string, unknown> = {}) {
  return {
    profile: {
      uuid: UUID,
      name: "Анна",
      city: "Томск",
      age: 28,
      gender: "female",
      status: "active",
      consent_version: "1.2",
      photo_url: "https://storage.test/photos/a/1.jpg?expires=900",
      photo_expires_in: 900,
    },
    reports: { filed: [], against: [], filed_total: 0, against_total: 2 },
    sanctions: { items: [], total: 0 },
    attendance: { items: [], total: 12 },
    ...overrides,
  };
}

function stubFetch(response: Response | Error): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      if (response instanceof Error) throw response;
      return response;
    }),
  );
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("loadUserCard — разбор ответов API (AC-7)", () => {
  it("200 → профиль и три числа сводки", async () => {
    stubFetch(jsonResponse(200, cardBody()));
    try {
      const state = await loadUserCard("session=abc", UUID);
      expect(state.kind).toBe("ok");
      if (state.kind !== "ok") return;
      expect(state.data.profile.name).toBe("Анна");
      expect(state.data.profile.age).toBe(28);
      expect(state.data.profile.consent_version).toBe("1.2");
      expect(state.data.profile.photo_url).toBe(
        "https://storage.test/photos/a/1.jpg?expires=900",
      );
      expect(state.data.summary).toEqual({
        reports_against_total: 2,
        sanctions_total: 0,
        attendance_total: 12,
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("403 → forbidden, а не unavailable", async () => {
    stubFetch(jsonResponse(403, { code: "forbidden" }));
    try {
      const state = await loadUserCard("session=abc", UUID);
      expect(state.kind).toBe("forbidden");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("404 → notFound, а не forbidden", async () => {
    stubFetch(jsonResponse(404, { code: "user_not_found" }));
    try {
      const state = await loadUserCard("session=abc", UUID);
      expect(state.kind).toBe("notFound");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("401 → unauthorized, а не unavailable (CR M-6, P-26)", async () => {
    stubFetch(jsonResponse(401, { code: "unauthorized" }));
    try {
      const state = await loadUserCard("session=abc", UUID);
      // Действие читателя другое: «войти заново», а не «перезагрузить».
      expect(state.kind).toBe("unauthorized");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("422 → notFound: битый uuid, а не сбой сервиса (CR M-6)", async () => {
    stubFetch(jsonResponse(422, { detail: [] }));
    try {
      const state = await loadUserCard("session=abc", UUID);
      expect(state.kind).toBe("notFound");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("429 → unavailable: совет «попробовать снова» верен для лимита (CR M-6)", async () => {
    stubFetch(jsonResponse(429, { code: "rate_limited" }));
    try {
      const state = await loadUserCard("session=abc", UUID);
      expect(state.kind).toBe("unavailable");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("500 → unavailable", async () => {
    stubFetch(jsonResponse(500, { code: "internal" }));
    try {
      const state = await loadUserCard("session=abc", UUID);
      expect(state.kind).toBe("unavailable");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("сеть упала → unavailable, а не исключение", async () => {
    stubFetch(new TypeError("fetch failed"));
    try {
      const state = await loadUserCard("session=abc", UUID);
      expect(state.kind).toBe("unavailable");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("200 без профиля → unavailable (сбой контракта, не пустая анкета)", async () => {
    stubFetch(jsonResponse(200, { reports: { against_total: 0 } }));
    try {
      const state = await loadUserCard("session=abc", UUID);
      expect(state.kind).toBe("unavailable");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("ходит именно в /api/v1/moderation/users/, не в /profiles/", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, cardBody()));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await loadUserCard("session=abc", UUID);
      const firstCall = fetchMock.mock.calls[0] as unknown[] | undefined;
      expect(firstCall).toBeDefined();
      const url = String(firstCall?.[0]);
      expect(url).toContain("/api/v1/moderation/users/");
      expect(url).not.toContain("/profiles/");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
