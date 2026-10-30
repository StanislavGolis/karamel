/**
 * Окно метрик пилота: разбор `searchParams` (стори 10.5, P-4; AC-01, AC-04.2).
 *
 * **Почему unit-тест, а не проверка страницы.** Дефект P-4 был не в
 * отрисовке: окно жёстко фиксировалось 14 днями, поэтому `insufficient_window`
 * не мог стать `false` НИ ПРИ КАКОМ вводе, и ветка AC-04.4 «Провалено N из 4
 * за M дней» была недостижима. Проверять здесь нечего, кроме границ окна.
 *
 * **Границы по обе стороны (§18.11):** 41 день → критерий ещё не применим,
 * 42 → применим; `to` ровно на 365 днях от `from` → легально, 366 → откат;
 * невалидный день `2026-02-30` → дефолт с подсказкой.
 */

import { describe, expect, it } from "vitest";

import {
  DEFAULT_WINDOW_DAYS,
  MAX_WINDOW_DAYS,
  MIN_PERIOD_MONTHS,
  monthsAgo,
  resolvePeriod,
} from "@/lib/metrics-period";

/** Фиксированное «сегодня» — иначе окно по умолчанию плывёт по календарю. */
const NOW = new Date("2026-05-15T12:34:56.000Z");

describe("resolvePeriod", () => {
  it("по умолчанию отдаёт 14 дней назад от сегодняшней UTC-полуночи", () => {
    // AC-01: дефолт — окно по NFR6 «14-й день».
    const period = resolvePeriod({}, NOW);

    expect(period.windowDays).toBe(DEFAULT_WINDOW_DAYS);
    expect(period.to).toBe("2026-05-15T00:00:00.000Z");
    expect(period.from).toBe("2026-05-01T00:00:00.000Z");
    expect(period.ignoredParams).toBe(false);
  });

  it("окно 42 дня делает критерий отказа применимым", () => {
    // AC-04.2: ровно 42 дня — порог, при котором `insufficient_window` = false.
    const period = resolvePeriod({ days: "42" }, NOW);

    expect(period.windowDays).toBe(42);
    expect(period.from).toBe("2026-04-03T00:00:00.000Z");
    expect(period.to).toBe("2026-05-15T00:00:00.000Z");
  });

  it("окно 41 день оставляет критерий неприменимым", () => {
    // AC-04.2, вторая сторона: 41 день < 42 — «ещё нельзя судить».
    const period = resolvePeriod({ days: "41" }, NOW);

    expect(period.windowDays).toBe(41);
    expect(period.ignoredParams).toBe(false);
  });

  it("явные from/to переопределяют пресет и дают выбранное окно", () => {
    // AC-01: «метрики на любую дату» — границы приходят снаружи.
    const period = resolvePeriod({ from: "2026-01-01", to: "2026-03-01", days: "14" }, NOW);

    expect(period.from).toBe("2026-01-01T00:00:00.000Z");
    expect(period.to).toBe("2026-03-01T00:00:00.000Z");
    expect(period.windowDays).toBe(59);
    expect(period.fromDate).toBe("2026-01-01");
    expect(period.toDate).toBe("2026-03-01");
  });

  it("перевёрнутое окно откатывается на дефолт с подсказкой", () => {
    // AC-01.3: `from < to` — рубеж на сервере; здесь только подсказка.
    const period = resolvePeriod({ from: "2026-03-01", to: "2026-01-01" }, NOW);

    expect(period.windowDays).toBe(DEFAULT_WINDOW_DAYS);
    expect(period.ignoredParams).toBe(true);
  });

  it("окно нулевой длины откатывается на дефолт с подсказкой", () => {
    // AC-01.3: `from == to` — 422 на сервере, дефолт в UI.
    const period = resolvePeriod({ from: "2026-01-01", to: "2026-01-01" }, NOW);

    expect(period.windowDays).toBe(DEFAULT_WINDOW_DAYS);
    expect(period.ignoredParams).toBe(true);
  });

  it("ровно 365 дней легально, 366 — откат", () => {
    // AC-01.4: `MAX_WINDOW_DAYS` — `>`, не `>=`.
    //
    // Стори 11.7 (AC-8): 365 дней ЦЕЛИКОМ В ПРОШЛОМ больше невозможны —
    // начало окна раньше шести месяцев клампится, и окно молча укорачивается
    // (проверено выше). Граница `>` не отменена, поэтому проверяется окном,
    // которое начинается внутри срока хранения, а `to` уходит в будущее:
    // `format: date-time` будущее не запрещает, а `MAX_WINDOW_DAYS` — про
    // ДЛИНУ независимо от того, где лежит окно.
    const from = "2026-05-01";
    const allowed = resolvePeriod({ from, to: "2027-05-01" }, NOW);
    expect(allowed.windowDays).toBe(MAX_WINDOW_DAYS);
    expect(allowed.ignoredParams).toBe(false);

    const rejected = resolvePeriod({ from, to: "2027-05-02" }, NOW);
    expect(rejected.windowDays).toBe(DEFAULT_WINDOW_DAYS);
    expect(rejected.ignoredParams).toBe(true);
  });

  it("несуществующий день откатывается на дефолт с подсказкой", () => {
    // `2026-02-30` в `Date` превращается в 2 марта — молчаливый перенос даты
    // дал бы окно, которого администратор не выбирал.
    const period = resolvePeriod({ from: "2026-02-30", to: "2026-05-15" }, NOW);

    expect(period.from).toBe("2026-05-01T00:00:00.000Z");
    expect(period.ignoredParams).toBe(true);
  });

  it("задан только один из двух параметров — откат с подсказкой", () => {
    // Полуокно не определено: без `to` граница «сегодня», без `from` — 14 дней;
    // угадывать за администратора значило бы показать не то окно.
    const onlyFrom = resolvePeriod({ from: "2026-01-01" }, NOW);
    expect(onlyFrom.windowDays).toBe(DEFAULT_WINDOW_DAYS);
    expect(onlyFrom.ignoredParams).toBe(true);

    const onlyTo = resolvePeriod({ to: "2026-01-01" }, NOW);
    expect(onlyTo.windowDays).toBe(DEFAULT_WINDOW_DAYS);
    expect(onlyTo.ignoredParams).toBe(true);
  });

  it("нечисловой или выходящий за предел пресет откатывается на дефолт", () => {
    // Подсказка важна: молчаливая подмена 14 днями выглядела бы как «выбрал 90».
    for (const days of ["abc", "0", "366", "-5"]) {
      const period = resolvePeriod({ days }, NOW);
      expect(period.windowDays, days).toBe(DEFAULT_WINDOW_DAYS);
      expect(period.ignoredParams, days).toBe(true);
    }
  });
});

describe("monthsAgo", () => {
  /**
   * Зеркало серверной `_months_ago` (стори 11.7, Task 4).
   * `setUTCMonth` сам переносит день через границу месяца (`31 февраля` → 3 марта),
   * поэтому кламп проверяется отдельно — иначе граница окна уехала бы на сутки.
   */
  it("кламп 31-го числа до последнего дня месяца", () => {
    // 2026-08-31 − 6 мес: февраля 31 не существует, последний день — 28.
    expect(monthsAgo(new Date("2026-08-31T00:00:00.000Z"), 6).toISOString()).toBe(
      "2026-02-28T00:00:00.000Z",
    );
    expect(monthsAgo(new Date("2026-03-31T00:00:00.000Z"), 1).toISOString()).toBe(
      "2026-02-28T00:00:00.000Z",
    );
  });

  it("високосный февраль", () => {
    // 2024 — високосный: 29 февраля существует, клампа до 28 быть не должно.
    expect(monthsAgo(new Date("2024-08-31T00:00:00.000Z"), 6).toISOString()).toBe(
      "2024-02-29T00:00:00.000Z",
    );
  });

  it("существующий день не клампится и время суток сохраняется", () => {
    expect(monthsAgo(new Date("2026-05-30T12:34:56.000Z"), 1).toISOString()).toBe(
      "2026-04-30T12:34:56.000Z",
    );
  });

  it("переход через год", () => {
    expect(monthsAgo(new Date("2026-01-31T00:00:00.000Z"), 1).toISOString()).toBe(
      "2025-12-31T00:00:00.000Z",
    );
  });
});

describe("resolvePeriod — нижняя граница окна (стори 11.7, AC-8)", () => {
  /**
   * AC-8: `from` старше шести месяцев сервер отвергает (`422 period_too_old`).
   * Страница обязана не показывать «сервис недоступен», а клампить начало
   * окна до границы и поднимать `ignoredParams`.
   */
  it("from старше шести месяцев клампится до границы, окно не откатывается", () => {
    const period = resolvePeriod({ from: "2025-01-01", to: "2026-05-15" }, NOW);

    // Не дефолтное окно: администратор увидел бы не тот период, что выбрал.
    expect(period.to).toBe("2026-05-15T00:00:00.000Z");
    expect(period.windowDays).toBeGreaterThan(0);
    expect(period.windowDays).toBeLessThan(MAX_WINDOW_DAYS);
    expect(period.ignoredParams).toBe(true);

    // Начало — не раньше границы (шесть месяцев назад плюс сутки запаса).
    const boundary = monthsAgo(NOW, MIN_PERIOD_MONTHS);
    expect(new Date(period.from).getTime()).toBeGreaterThan(boundary.getTime());
  });

  it("from внутри шести месяцев не трогается", () => {
    // 2026-05-01 при «сегодня» 2026-05-15 — внутри границы (2025-11-15).
    const period = resolvePeriod({ from: "2026-05-01", to: "2026-05-15" }, NOW);

    expect(period.from).toBe("2026-05-01T00:00:00.000Z");
    expect(period.ignoredParams).toBe(false);
  });

  it("дата на сутки позже границы всё равно клампится до границы+сутки", () => {
    // CR 11.7: без этого теста мутация «убрать сутки запаса в periodFloor»
    // ВЫЖИВАЛА — прежние проверки сравнивали `>=`/`>` и проходили при обоих
    // значениях. Утверждение должно быть ТОЧНЫМ.
    //
    // Вход страницы — только `YYYY-MM-DD` (`parseDateOnly`), поэтому различие
    // ловится не часами, а днём: при `NOW = 2026-05-15T12:34:56Z` граница
    // `2025-11-15T12:34:56Z`. Дата `2025-11-16` — уже ПОЗЖЕ границы, но
    // РАНЬШЕ границы с сутками запаса (`2025-11-16T12:34:56Z`).
    // С запасом → клампится ровно до `2025-11-16T12:34:56Z`.
    // Без запаса → осталась бы `2025-11-16T00:00:00Z`.
    const period = resolvePeriod({ from: "2025-11-16", to: "2026-05-15" }, NOW);

    expect(period.from).toBe("2025-11-16T12:34:56.000Z");
    expect(period.ignoredParams).toBe(true);
  });

  it("граница включает запас в сутки: ровно шесть месяцев назад не отвергается", () => {
    // Рендер и запрос разнесены во времени; без запаса from оказался бы
    // на микросекунды старше серверной границы и получал бы 422.
    const boundary = monthsAgo(NOW, MIN_PERIOD_MONTHS);
    const fromDate = boundary.toISOString().slice(0, 10);
    const period = resolvePeriod({ from: fromDate, to: "2026-05-15" }, NOW);

    expect(new Date(period.from).getTime()).toBeGreaterThanOrEqual(boundary.getTime());
  });
});
