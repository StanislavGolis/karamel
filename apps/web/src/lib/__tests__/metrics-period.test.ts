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
    const allowed = resolvePeriod({ from: "2025-05-15", to: "2026-05-15" }, NOW);
    expect(allowed.windowDays).toBe(MAX_WINDOW_DAYS);
    expect(allowed.ignoredParams).toBe(false);

    const rejected = resolvePeriod({ from: "2025-05-14", to: "2026-05-15" }, NOW);
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
