/**
 * Метрики пилота (стори 10.5, Task 7.1; AC-01, AC-03, AC-04, AC-05).
 *
 * **Серверный компонент, без клиентского состояния** (D-10.5-10): данные
 * приходят из `GET /api/v1/metrics` при рендере. Нет `fetch` в браузере, нет
 * `useEffect`, нет мигания «загружается → провал» и нет второго пути
 * получения тех же чисел.
 *
 * **Гарда НЕ дублируется.** `(mod)/mod/layout.tsx` уже проверил сессию и роль
 * (`redirect("/login")` без сессии, `redirect("/")` для не-модератора).
 * Вторая проверка здесь завела бы третье представление одного правила — ровно
 * тот дефект, из-за которого в 1.6 аккаунт попадал в цикл редиректов.
 *
 * **Почему `instant = false`.** Окно метрик по умолчанию — 14 дней, и оно
 * считается от текущего момента: без серверного рендера страница показывала бы
 * данные не того окна, которое видит администратор.
 *
 * **Ошибки различаются.** `403` — не «сервис недоступен», а «нет прав»: это
 * разные действия читателя, и сливать их в один текст значит отправить
 * модератора перезагружать страницу вместо проверки роли.
 */

import { cookies } from "next/headers";

import { MetricsPeriodForm } from "@/components/mod/metrics-period-form";
import { PilotMetrics, type PilotMetricsView } from "@/components/mod/pilot-metrics";
import { resolvePeriod, type MetricsPeriod } from "@/lib/metrics-period";

export const metadata = { robots: { index: false, follow: false } };

/** Состояние нужно до отрисовки: без него неизвестно, что показывать. */
export const instant = false;

const API_TIMEOUT_MS = 5_000;

type MetricsState =
  | { kind: "ok"; data: PilotMetricsView }
  | { kind: "forbidden" }
  | { kind: "unavailable" };

/**
 * Читает метрики через API на выбранное окно.
 *
 * Форма ответа проверяется по обязательным полям: серверное расширение не
 * должно ломать отрисовку, а ответ без `metrics` — не «пустой кабинет»,
 * а сбой контракта.
 */
async function loadMetrics(cookieHeader: string, period: MetricsPeriod): Promise<MetricsState> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
  const query = `?from=${encodeURIComponent(period.from)}&to=${encodeURIComponent(period.to)}`;

  try {
    const response = await fetch(`${apiUrl}/api/v1/metrics${query}`, {
      headers: { cookie: cookieHeader },
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });

    // 403 — отказ по роли, а не сбой сервиса: разные действия читателя.
    if (response.status === 403) return { kind: "forbidden" };
    if (!response.ok) return { kind: "unavailable" };

    const payload = (await response.json()) as Partial<PilotMetricsView>;
    if (!Array.isArray(payload.metrics) || payload.metrics.length === 0) {
      return { kind: "unavailable" };
    }
    if (typeof payload.failure_criteria !== "object" || payload.failure_criteria === null) {
      return { kind: "unavailable" };
    }

    return {
      kind: "ok",
      data: {
        metrics: payload.metrics,
        failure_criteria: payload.failure_criteria,
        warnings: Array.isArray(payload.warnings) ? payload.warnings : [],
        pilot_day: typeof payload.pilot_day === "number" ? payload.pilot_day : null,
        window_days: typeof payload.window_days === "number" ? payload.window_days : 0,
      },
    };
  } catch {
    return { kind: "unavailable" };
  }
}

export default async function PilotMetricsPage({
  searchParams,
}: {
  searchParams: Promise<{ from?: string; to?: string; days?: string }>;
}) {
  const jar = await cookies();
  const cookieHeader = jar
    .getAll()
    .map(({ name, value }) => `${name}=${value}`)
    .join("; ");

  // P-4: окно приходит из адреса, а не из жёсткой константы — иначе критерий
  // отказа AC-04.4 не мог сработать ни при каком вводе администратора.
  const period = resolvePeriod(await searchParams);

  const state = await loadMetrics(cookieHeader, period);

  if (state.kind === "forbidden") {
    return (
      <main className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Метрики пилота</h1>
        <p className="text-base">
          Недостаточно прав: метрики пилота доступны только модератору.
        </p>
      </main>
    );
  }

  if (state.kind === "unavailable") {
    return (
      <main className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Метрики пилота</h1>
        <p className="text-base leading-relaxed">
          Не удалось загрузить метрики. Обновите страницу через минуту — выходить
          из аккаунта не нужно.
        </p>
      </main>
    );
  }

  return (
    <main className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold tracking-tight">Метрики пилота</h1>
      {/* P-4: окно выбирается здесь; без этого критерий отказа AC-04.4 недостижим. */}
      <MetricsPeriodForm period={period} />
      <PilotMetrics data={state.data} />
    </main>
  );
}
