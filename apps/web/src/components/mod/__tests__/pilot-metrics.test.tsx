/**
 * Метрики пилота: четыре состояния презентации (стори 10.5, Task 7.7; AC-03, AC-04, AC-05).
 *
 * **Почему `renderToStaticMarkup`, а не `@testing-library/react`.** Компонент
 * серверный и без состояния (D-10.5-10): у него нет `useEffect`, нет запросов
 * и нет событий. Проверять «клики» здесь нечего, а строковый рендер позволяет
 * утверждать про ТЕКСТ ответа — то, что увидит модератор.
 *
 * **Четыре состояния — из AC, а не из кода:**
 * 1. всё в норме (AC-01.5) — «выполнено»;
 * 2. провал (AC-01.5) — «ниже порога» + критерий отказа (AC-04.4);
 * 3. пусто (AC-05.3) — «нет данных», и НЕ «провалено»;
 * 4. предупреждение NFR6 (AC-03.2) — текст предупреждения в разметке.
 *
 * **Границы проверяются по обе стороны** (§18.11): `passed: true` и
 * `passed: false` в одном тесте, `pilot_day: null` и число, окно 42 и 41.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  PilotMetrics,
  type FailureCriteriaView,
  type MetricView,
  type PilotMetricsView,
} from "@/components/mod/pilot-metrics";

function metric(
  code: string,
  value: number | null,
  passed: boolean | null,
  threshold = 0.3,
): MetricView {
  return {
    code,
    value,
    numerator: value === null ? 0 : 1,
    denominator: value === null ? 0 : 2,
    threshold,
    passed,
  };
}

function criteria(overrides: Partial<FailureCriteriaView> = {}): FailureCriteriaView {
  return {
    triggered: false,
    failed_count: 0,
    window_days: 14,
    insufficient_window: true,
    failed_metrics: [],
    ...overrides,
  };
}

function data(overrides: Partial<PilotMetricsView> = {}): PilotMetricsView {
  return {
    metrics: [
      metric("nfr1", 0.5, true),
      metric("nfr2", 2.0, true, 1.5),
      metric("nfr3", 0.8, true, 0.6),
      metric("nfr4", 0.4, true, 0.25),
    ],
    failure_criteria: criteria(),
    warnings: [],
    pilot_day: 10,
    window_days: 14,
    ...overrides,
  };
}

function render(props: PilotMetricsView): string {
  return renderToStaticMarkup(<PilotMetrics data={props} />);
}

describe("PilotMetrics", () => {
  it("показывает все четыре метрики со статусом «выполнено»", () => {
    const html = render(data());

    // AC-01.1: все четыре присутствуют всегда.
    expect(html).toContain("Собранные встречи");
    expect(html).toContain("Участников на встречу");
    expect(html).toContain("Подтверждённая явка");
    expect(html).toContain("Повторные авторы");
    // AC-01.5: `passed: true` → «выполнено», а не «ниже порога».
    expect(html).toContain("выполнено");
    expect(html).not.toContain("ниже порога");
    // AC-01.2, TD-10.5-4: порог приходит из ответа.
    // `Intl.NumberFormat` вставляет неразрывный пробел перед `%` —
    // проверяется число, а не типографский знак.
    expect(html).toMatch(/порог 30\s*%/u);
  });

  it("показывает провал и критерий отказа", () => {
    // AC-04.1: два провала и окно 42 дня → сработавший критерий.
    const html = render(
      data({
        metrics: [
          metric("nfr1", 0.1, false),
          metric("nfr2", 1.0, false, 1.5),
          metric("nfr3", 0.8, true, 0.6),
          metric("nfr4", 0.4, true, 0.25),
        ],
        failure_criteria: criteria({
          triggered: true,
          failed_count: 2,
          window_days: 42,
          insufficient_window: false,
          failed_metrics: ["nfr1", "nfr2"],
        }),
        window_days: 42,
      }),
    );

    expect(html).toContain("ниже порога");
    // AC-04.4: «провалено N из 4 за M дней».
    expect(html).toContain("Провалено 2 из 4 за 42 дн.");
    expect(html).toContain("критерий отказа сработал");
    expect(html).toContain("Ниже порога: Собранные встречи, Участников на встречу");
  });

  it("показывает «нет данных» вместо «провалено» при null", () => {
    // AC-05.3: `value: null` → «нет данных», НЕ «ниже порога».
    const html = render(
      data({
        metrics: [
          metric("nfr1", null, null),
          metric("nfr2", null, null, 1.5),
          metric("nfr3", null, null, 0.6),
          metric("nfr4", null, null, 0.25),
        ],
        failure_criteria: criteria({ failed_count: 0 }),
        // AC-03.4: дата старта не задана — `null`, не `0`.
        pilot_day: null,
      }),
    );

    expect(html).toContain("нет данных");
    expect(html).not.toContain("ниже порога");
    // AC-05.2: числитель и знаменатель объясняют `null`.
    expect(html).toContain("0 из 0");
    // AC-03.4: дата старта не задана.
    expect(html).toContain("Дата старта пилота не задана");
  });

  it("показывает предупреждение NFR6 и не блокирует пилот", () => {
    // AC-03.2: предупреждение выводится текстом, а не только цветом.
    const html = render(
      data({
        warnings: ["nfr1_below_10_percent"],
        pilot_day: 14,
      }),
    );

    expect(html).toContain("Внимание");
    expect(html).toContain("ниже 10%");
    expect(html).toContain("Пилот не блокируется");
  });

  it("неизвестный код предупреждения показывается как есть", () => {
    // NFR60: серверное расширение не должно ломать отрисовку.
    const html = render(data({ warnings: ["something_new"] }));
    expect(html).toContain("something_new");
  });

  it("показывает «окно короче 6 недель» вместо «всё хорошо»", () => {
    // AC-04.2, D-10.5-9: короткое окно — отдельное утверждение.
    const html = render(
      data({ failure_criteria: criteria({ insufficient_window: true, window_days: 41 }) }),
    );

    expect(html).toContain("Окно короче 6 недель (41 дн.)");
    expect(html).toContain("не означает, что метрики в норме");
  });
});
