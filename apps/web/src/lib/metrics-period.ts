/**
 * Окно метрик пилота: разбор `searchParams` и сборка ISO-границ (стори 10.5, P-4).
 *
 * **Зачем отдельный модуль.** До правки окно было жёстко 14 дней внутри
 * `page.tsx`, поэтому критерий отказа (`insufficient_window`) не мог стать
 * `false` ни при каком действии администратора: сервер честно отвечал «окно
 * короче 6 недель», и ветка AC-04.4 «Провалено N из 4 за M дней» в UI была
 * недостижима. AC-01 требует метрики на ЛЮБУЮ дату — значит границы приходят
 * снаружи, а не из `new Date()`.
 *
 * **Почему чистая функция, а не чтение `searchParams` в компоненте.** Правило
 * проекта: то, что нельзя проверить, нельзя обещать. Разбор строки проверяется
 * unit-тестом без рендера и без сети (`resolvePeriod({...})`), а серверный
 * компонент остаётся тонким слоем между `searchParams` и этой функцией.
 *
 * **Почему `date`, а не `datetime`.** Администратор выбирает ДЕНЬ, а не секунду:
 * поле `datetime-local` требует ещё и времени, и человек вводил бы `00:00`
 * вручную. Границы дня достраиваются до UTC-полуночи — той же формы, что
 * отдаёт `toISOString()`, и её принимает контракт `format: date-time`.
 */

/** Верхняя граница окна в днях — копия серверного `MAX_WINDOW_DAYS` (AC-01.4). */
export const MAX_WINDOW_DAYS = 365;

/** Окно по умолчанию: NFR6 говорит про «14-й день», значит и смотрят его. */
export const DEFAULT_WINDOW_DAYS = 14;

/** Пресеты окна. 42 дня — минимальное окно, в котором критерий отказа осмыслен. */
export const WINDOW_PRESETS = [14, 42, 90] as const;

const MS_PER_DAY = 24 * 60 * 60 * 1000;

/** `YYYY-MM-DD` — единственная форма, которую отдаёт `<input type="date">`. */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/** Окно метрик: границы в ISO UTC и длина в днях. */
export type MetricsPeriod = {
  /** Нижняя граница, `YYYY-MM-DDTHH:mm:ss.sssZ`. */
  from: string;
  /** Верхняя граница, `YYYY-MM-DDTHH:mm:ss.sssZ`. */
  to: string;
  /** Нижняя граница для `<input type="date">`, `YYYY-MM-DD`. */
  fromDate: string;
  /** Верхняя граница для `<input type="date">`, `YYYY-MM-DD`. */
  toDate: string;
  /** Длина окна в днях — то, что покажет подпись «за M дней». */
  windowDays: number;
  /** `true`, если хотя бы один параметр был отвергнут (показать подсказку). */
  ignoredParams: boolean;
};

/** UTC-полночь дня; `null` — строка не является календарной датой. */
function parseDateOnly(value: string | undefined): Date | null {
  if (value === undefined || !DATE_ONLY.test(value)) return null;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  // `2026-02-30` разбирается в `2026-03-02`: сверка обратной сборкой отбивает
  // несуществующие дни, которые `Date` молча переносит на следующий месяц.
  if (parsed.toISOString().slice(0, 10) !== value) return null;
  return parsed;
}

/**
 * Окно метрик по параметрам адреса.
 *
 * Приоритет: явные `from` и `to` → окно по ним; иначе пресет `days`; иначе
 * `DEFAULT_WINDOW_DAYS` назад от «сегодня». Невалидный ввод не роняет страницу
 * (`422` показал бы «не удалось загрузить метрики» — ложный сбой сервиса),
 * а откатывается на окно по умолчанию и поднимает `ignoredParams`.
 *
 * @param params — `searchParams` страницы
 * @param now — текущий момент; параметр нужен тесту, чтобы окно не «плыло»
 */
export function resolvePeriod(
  params: { from?: string; to?: string; days?: string },
  now: Date = new Date(),
): MetricsPeriod {
  let ignored = false;

  const from = parseDateOnly(params.from);
  const to = parseDateOnly(params.to);

  if (from !== null && to !== null) {
    // Серверное правило `from < to` (AC-01.3): пустое окно — ошибка запроса.
    // Здесь оно дублируется как ПОДСКАЗКА, рубеж остаётся на сервере.
    if (from.getTime() < to.getTime()) {
      const windowDays = Math.round((to.getTime() - from.getTime()) / MS_PER_DAY);
      if (windowDays <= MAX_WINDOW_DAYS) {
        return build(from, to, windowDays, ignored);
      }
    }
    ignored = true;
  } else if (params.from !== undefined || params.to !== undefined) {
    // Один из двух параметров задан — окно не определено, откат на дефолт.
    ignored = true;
  }

  const requestedDays = Number.parseInt(params.days ?? "", 10);
  const days =
    Number.isFinite(requestedDays) && requestedDays >= 1 && requestedDays <= MAX_WINDOW_DAYS
      ? requestedDays
      : DEFAULT_WINDOW_DAYS;
  if (params.days !== undefined && days !== requestedDays) ignored = true;

  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const start = new Date(end.getTime() - days * MS_PER_DAY);
  return build(start, end, days, ignored);
}

function build(from: Date, to: Date, windowDays: number, ignoredParams: boolean): MetricsPeriod {
  return {
    from: from.toISOString(),
    to: to.toISOString(),
    fromDate: from.toISOString().slice(0, 10),
    toDate: to.toISOString().slice(0, 10),
    windowDays,
    ignoredParams,
  };
}
