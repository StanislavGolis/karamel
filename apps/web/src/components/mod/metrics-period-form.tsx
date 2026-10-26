/**
 * Выбор окна метрик пилота (стори 10.5, P-4; AC-01, AC-04.4).
 *
 * **Зачем форма, если страница серверная.** До правки окно жёстко равнялось
 * 14 дням (`new Date(to.getTime() - 14 * 24 * 60 * 60 * 1000)`), и сервер
 * честно отвечал `insufficient_window: true`. Значит ветка AC-04.4 «Провалено
 * N из 4 за M дней» (`pilot-metrics.tsx`) не могла отрисоваться НИ ПРИ КАКОМ
 * действии администратора, а AC-01 «метрики на любую дату» в UI не выполнялся.
 * Форма — единственный вход, который делает это окно управляемым.
 *
 * **Почему GET-форма, а не клиентский `fetch`.** Данные приходят при серверном
 * рендере (D-10.5-10): отправка формы меняет адрес, страница перерисовывается
 * на сервере, и второго пути получения тех же чисел не появляется. Плюс окно
 * становится ссылкой, которую можно переслать другому модератору.
 *
 * **Почему `type="date"`, а не `datetime-local`.** Администратор выбирает день;
 * время в UTC-полуночь достраивает `resolvePeriod`. Ручной ввод `00:00` —
 * лишний шаг ради точности, которая метрикам не нужна.
 *
 * **Пресеты 14 / 42 / 90.** 42 дня — ровно то окно, в котором критерий отказа
 * вообще имеет смысл (AC-04.2); 14 — день предупреждения NFR6; 90 — «квартал».
 * Пресет ссылкой, а не кнопкой: работает без JS и не требует обработчика.
 */

import type { MetricsPeriod } from "@/lib/metrics-period";
import { WINDOW_PRESETS } from "@/lib/metrics-period";

/** Подписи пресетов: месяц/полтора месяца читаются хуже, чем число дней. */
const PRESET_LABELS: Record<number, string> = {
  14: "14 дней",
  42: "42 дня",
  90: "90 дней",
};

export type MetricsPeriodFormProps = {
  /** Текущее окно — из `resolvePeriod` на сервере. */
  period: MetricsPeriod;
};

export function MetricsPeriodForm({ period }: MetricsPeriodFormProps) {
  return (
    <section
      aria-labelledby="metrics-period"
      className="flex flex-col gap-3 rounded border border-border bg-surface-raised p-4"
    >
      <div className="flex flex-col gap-1">
        <h2 id="metrics-period" className="text-lg font-semibold text-ink">
          Период
        </h2>
        {/* AC-04.4: длина окна видна числом — «за M дней» относится к нему. */}
        <p className="text-sm text-ink-secondary">
          Сейчас показано {period.windowDays} дн. — с {period.fromDate} по {period.toDate} (UTC).
        </p>
      </div>

      {/* Пресеты — ссылки: работают без JS и остаются закладкой. */}
      <ul className="flex flex-wrap gap-2">
        {WINDOW_PRESETS.map((days) => {
          const isActive = period.windowDays === days;
          return (
            <li key={days}>
              <a
                href={`/mod/metrics?days=${days}`}
                aria-current={isActive ? "true" : undefined}
                className={`inline-flex min-h-11 items-center rounded-md border px-4 text-sm underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring ${
                  isActive
                    ? "border-accent bg-accent text-accent-ink"
                    : "border-border-input bg-surface-raised text-ink-secondary hover:bg-surface"
                }`}
              >
                {PRESET_LABELS[days] ?? `${days} дней`}
              </a>
            </li>
          );
        })}
      </ul>

      {/* Произвольное окно: `from < to` проверяет сервер, здесь только подсказка. */}
      <form method="get" className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm text-ink">
          С
          <input
            type="date"
            name="from"
            defaultValue={period.fromDate}
            required
            className="min-h-12 rounded-md border border-border-input bg-surface px-4 text-base text-ink outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm text-ink">
          По
          <input
            type="date"
            name="to"
            defaultValue={period.toDate}
            required
            className="min-h-12 rounded-md border border-border-input bg-surface px-4 text-base text-ink outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
          />
        </label>
        <button
          type="submit"
          className="inline-flex min-h-11 items-center rounded-md border border-border-input bg-surface-raised px-4 text-sm font-medium text-ink hover:bg-surface focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
        >
          Показать
        </button>
      </form>

      {/* Отвергнутый параметр не молчит: иначе «выбрал 90» и «14» выглядят одинаково. */}
      {period.ignoredParams && (
        <p role="status" className="text-sm text-ink-secondary">
          Часть параметров периода не распознана — показано окно по умолчанию (14 дней).
          Проверьте даты: начало должно быть раньше конца, окно — не длиннее года.
        </p>
      )}
    </section>
  );
}
