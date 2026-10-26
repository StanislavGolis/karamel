/**
 * Выбор окна метрик пилота (стори 10.5, P-4; AC-01, AC-04.4).
 *
 * **Что доказывается.** Дефект P-4 был в недостижимости: страница фиксировала
 * окно 14 днями, поэтому `insufficient_window` всегда было `true`, а ветка
 * AC-04.4 «Провалено N из 4 за M дней» не отрисовывалась НИ ПРИ КАКОМ вводе.
 * Тест фиксирует три вещи: ссылка-пресет 42 меняет окно, форма отправляет
 * `from`/`to` в адрес, отвергнутый параметр не молчит.
 *
 * **Почему `renderToStaticMarkup`.** Компонент без состояния (D-10.5-10):
 * проверять события нечего, а строковый рендер позволяет утверждать про
 * фактическую разметку — то, что увидит и нажмёт администратор.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { MetricsPeriodForm } from "@/components/mod/metrics-period-form";
import { resolvePeriod } from "@/lib/metrics-period";

/** Фиксированное «сегодня», чтобы окно не зависело от календаря прогона. */
const NOW = new Date("2026-05-15T12:34:56.000Z");

function render(params: { from?: string; to?: string; days?: string }): string {
  return renderToStaticMarkup(<MetricsPeriodForm period={resolvePeriod(params, NOW)} />);
}

describe("MetricsPeriodForm", () => {
  it("даёт ссылку на окно 42 дня — то, в котором критерий отказа применим", () => {
    // AC-04.2/AC-04.4: 42 дня — минимальное окно, при котором сервер вернёт
    // `insufficient_window: false` и текст «Провалено N из 4 за M дней».
    const html = render({});

    expect(html).toContain('href="/mod/metrics?days=42"');
    expect(html).toContain("42 дня");
    expect(html).toContain('href="/mod/metrics?days=14"');
    expect(html).toContain('href="/mod/metrics?days=90"');
  });

  it("отправляет выбранные даты в адрес GET-формой", () => {
    // AC-01: «метрики на любую дату» — границы уходят в `searchParams`.
    const html = render({ from: "2026-01-01", to: "2026-03-01" });

    expect(html).toContain('method="get"');
    expect(html).toContain('name="from"');
    expect(html).toContain('name="to"');
    expect(html).toContain('value="2026-01-01"');
    expect(html).toContain('value="2026-03-01"');
  });

  it("показывает длину окна числом — она же попадает в «за M дней»", () => {
    // AC-04.4: «за M дней» — это `window_days` ответа, а не догадка UI.
    const html = render({ days: "42" });
    expect(html).toContain("Сейчас показано 42 дн.");

    const fourteen = render({});
    expect(fourteen).toContain("Сейчас показано 14 дн.");
  });

  it("помечает активный пресет для скринридера", () => {
    // §13.3: состояние — не только цветом.
    const html = render({ days: "42" });

    expect(html).toContain('aria-current="true"');
  });

  it("не молчит, когда параметр периода отвергнут", () => {
    // Иначе «выбрал 90» и «показано 14» выглядели бы одинаково.
    const html = render({ from: "2026-03-01", to: "2026-01-01" });

    expect(html).toContain("не распознана");
    expect(html).toContain("Сейчас показано 14 дн.");
  });

  it("молчит при валидном окне — подсказка не висит постоянно",
    () => {
      // Вторая сторона: иначе `role="status"` стал бы шумом на каждом экране.
      const html = render({ days: "42" });
      expect(html).not.toContain("не распознана");
    });
});
