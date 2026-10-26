/**
 * Метрики пилота — презентация (стори 10.5, Task 7.2; AC-01, AC-03, AC-04, AC-05).
 *
 * **Компонент без состояния и без запросов.** Данные приходят готовым
 * `PilotMetricsRead` при серверном рендере (D-10.5-10): нет `fetch`, нет
 * `useEffect`, нет мигания «загружается → провал». Именно поэтому компонент
 * проверяется `renderToStaticMarkup` — без сети и без БД.
 *
 * **Пороги НЕ дублируются (TD-10.5-4).** `threshold` приходит в каждой
 * метрике из ответа API. Своя константа порога здесь разошлась бы с сервером
 * при первой правке, и кабинет показывал бы «провал» там, где сервер считает
 * «норму». Форматирование порога — отдельная функция ровно потому, что это
 * ПРЕДСТАВЛЕНИЕ числа, а не его значение.
 *
 * **`null` — это «нет данных», а НЕ «провалено»** (AC-05.3). Три состояния
 * метрики различаются текстом, а не только цветом (§10: цвет никогда не
 * единственный носитель смысла): «выполнено» / «ниже порога» / «нет данных».
 * Слить `null` с провалом значит объявить пилот провалившимся на пустой базе.
 */

import type { ReactNode } from "react";

/** Метрика в ответе API (`MetricRead`). */
export type MetricView = {
  code: string;
  value: number | null;
  numerator: number;
  denominator: number;
  threshold: number;
  passed: boolean | null;
};

/** Критерий отказа в ответе API (`FailureCriteriaRead`). */
export type FailureCriteriaView = {
  triggered: boolean;
  failed_count: number;
  window_days: number;
  insufficient_window: boolean;
  failed_metrics: string[];
};

/** Полный ответ кабинета (`PilotMetricsRead`). */
export type PilotMetricsView = {
  metrics: MetricView[];
  failure_criteria: FailureCriteriaView;
  warnings: string[];
  pilot_day: number | null;
  window_days: number;
};

/**
 * Подписи метрик — ТОЛЬКО в UI.
 *
 * `code` из API — машинный (`nfr1`…`nfr4`). Русский текст живёт здесь, иначе
 * API стал бы владельцем формулировок, а их правка требовала бы выкатки бэкенда.
 */
const METRIC_LABELS: Record<string, { title: string; unit: "share" | "per_card"; hint: string }> = {
  nfr1: {
    title: "Собранные встречи",
    unit: "share",
    hint: "Доля карточек, на которые записался хотя бы один человек",
  },
  nfr2: {
    title: "Участников на встречу",
    unit: "per_card",
    hint: "Среднее число занятых мест на одну карточку",
  },
  nfr3: {
    title: "Подтверждённая явка",
    unit: "share",
    hint: "Доля встреч, где приход подтвердили все участники",
  },
  nfr4: {
    title: "Повторные авторы",
    unit: "share",
    hint: "Доля авторов, создавших две встречи и больше",
  },
};

/** Тексты предупреждений NFR6 по машинному коду. Неизвестный код показывается как есть. */
const WARNING_TEXT: Record<string, string> = {
  nfr1_below_10_percent:
    "На 14-й день доля собранных встреч ниже 10%. Пилот не блокируется — это сигнал, а не приговор.",
};

const PERCENT = new Intl.NumberFormat("ru-RU", {
  style: "percent",
  maximumFractionDigits: 0,
});

const PER_CARD = new Intl.NumberFormat("ru-RU", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 2,
});

/**
 * Значение по единице измерения метрики.
 *
 * Доли — проценты, NFR2 — «участников на карточку» (число, не процент):
 * один формат для обеих единиц показал бы «150% участников на встречу».
 */
function formatValue(metric: MetricView, value: number): string {
  const definition = METRIC_LABELS[metric.code];
  return definition?.unit === "per_card" ? PER_CARD.format(value) : PERCENT.format(value);
}

function formatThreshold(metric: MetricView): string {
  return formatValue(metric, metric.threshold);
}

/**
 * Подпись состояния — текст, а не только цвет.
 *
 * `null` отделён от `false` намеренно (AC-05.3): «нет данных» и «ниже порога»
 * требуют разных действий читателя — первый смотрит, почему нет данных,
 * второй разбирается с продуктом.
 */
function statusLabel(metric: MetricView): string {
  if (metric.passed === null) return "нет данных";
  return metric.passed ? "выполнено" : "ниже порога";
}

/**
 * Класс состояния: цвет + текст (никогда только цвет).
 *
 * Семантические токены дизайн-системы, литеральные цвета запрещены (гейт 1).
 */
function statusClass(metric: MetricView): string {
  if (metric.passed === null) return "text-ink-muted";
  return metric.passed ? "text-success" : "text-danger";
}

/** Иконка состояния: `✓` / `✗` / `—`. Дублирует текст, не заменяет его. */
function statusIcon(metric: MetricView): string {
  if (metric.passed === null) return "—";
  return metric.passed ? "✓" : "✗";
}

type Props = {
  data: PilotMetricsView;
};

export function PilotMetrics({ data }: Props): ReactNode {
  const criteria = data.failure_criteria;
  const failedTitles = criteria.failed_metrics
    .map((code) => METRIC_LABELS[code]?.title ?? code)
    .join(", ");

  return (
    <div className="flex flex-col gap-6">
      {/* AC-03.1: день пилота на дату `to`; `null` — дата старта не задана. */}
      <p className="text-sm text-ink-secondary">
        {data.pilot_day === null
          ? "Дата старта пилота не задана — день пилота не определяется."
          : `День пилота: ${data.pilot_day}. Окно: ${data.window_days} дн.`}
      </p>

      {/* AC-03.2, NFR6: предупреждение. Пилот не блокируется (AC-03.3). */}
      {data.warnings.length > 0 && (
        <section
          aria-labelledby="pilot-warnings"
          className="flex flex-col gap-2 rounded border border-border bg-warning-tint p-4"
        >
          <h2 id="pilot-warnings" className="text-base font-semibold text-warning">
            Внимание
          </h2>
          <ul className="flex flex-col gap-1">
            {data.warnings.map((code) => (
              <li key={code} className="text-sm leading-relaxed text-ink">
                {WARNING_TEXT[code] ?? code}
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* AC-01.1: все четыре метрики присутствуют всегда. */}
      <section aria-labelledby="pilot-metrics-heading" className="flex flex-col gap-4">
        <h2 id="pilot-metrics-heading" className="text-lg font-semibold text-ink">
          Метрики
        </h2>
        <ul className="flex flex-col gap-4">
          {data.metrics.map((metric) => {
            const definition = METRIC_LABELS[metric.code];
            return (
              <li
                key={metric.code}
                className="flex flex-col gap-1 rounded border border-border bg-surface-raised p-4"
              >
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-base font-medium text-ink">
                    {definition?.title ?? metric.code}
                  </span>
                  {/* AC-05.3: три состояния, `null` — «нет данных». */}
                  <span className={`text-sm ${statusClass(metric)}`}>
                    {statusIcon(metric)} {statusLabel(metric)}
                  </span>
                </div>

                {definition && (
                  <p className="text-sm leading-relaxed text-ink-secondary">{definition.hint}</p>
                )}

                <p className="text-sm text-ink">
                  {metric.value === null
                    ? "нет данных"
                    : formatValue(metric, metric.value)}
                  {" · порог "}
                  {formatThreshold(metric)}
                </p>

                {/* AC-05.2: числитель и знаменатель объясняют `null`. */}
                <p className="text-sm text-ink-muted">
                  {metric.numerator} из {metric.denominator}
                </p>
              </li>
            );
          })}
        </ul>
      </section>

      {/* AC-04.4: «провалено N из 4 за M дней» + перечень проваленных. */}
      <section aria-labelledby="pilot-failure" className="flex flex-col gap-2 rounded border border-border p-4">
        <h2 id="pilot-failure" className="text-lg font-semibold text-ink">
          Критерий отказа
        </h2>

        {criteria.insufficient_window ? (
          // AC-04.2, D-10.5-9: «ещё нельзя судить» — не «всё хорошо».
          <p className="text-sm leading-relaxed text-ink-secondary">
            Окно короче 6 недель ({criteria.window_days} дн.) — судить о критерии отказа
            рано. Это не означает, что метрики в норме.
          </p>
        ) : (
          <p className="text-sm leading-relaxed text-ink">
            Провалено {criteria.failed_count} из 4 за {criteria.window_days} дн.
            {criteria.triggered && " — критерий отказа сработал."}
          </p>
        )}

        {criteria.failed_metrics.length > 0 && (
          <p className="text-sm text-ink-secondary">Ниже порога: {failedTitles}.</p>
        )}
      </section>
    </div>
  );
}
