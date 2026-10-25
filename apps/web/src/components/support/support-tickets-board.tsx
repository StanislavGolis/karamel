"use client";

/**
 * Форма и список обращений в одном клиентском компоненте (стори 9.5; D-9.5-5).
 *
 * **Почему общий стейт, а не два независимых компонента.** `201` отдаёт полный
 * элемент (D-9.5-5), и клиент вставляет его в уже отрисованный список без
 * второго запроса. Если бы форма и список были раздельными, после отправки
 * пришлось бы перезапрашивать страницу — экран мигал бы, а обращение
 * появлялось бы с задержкой.
 *
 * **Листание (CR 9.5 MEDIUM-3).** Бэкенд отдаёт keyset-страницу
 * (`limit=20`, `next_cursor`, `total`), но экран долго грузил только первую
 * страницу: 21-е обращение становилось недостижимым. При лимите 5/ч это
 * достижимо за неделю, то есть дефект реальный, а не теоретический.
 *
 * Кнопка «Показать ещё» — а не бесконечная прокрутка: список короткий
 * (десятки строк), а прокрутка без явного действия прячет факт «есть ещё»
 * и мешает клавиатурной навигации.
 *
 * **`total` показывается** (LOW-3): без числа человек не понимает, почему
 * список обрезан. После `201` счётчик увеличивается локально — второй запрос
 * ради одного числа не нужен.
 *
 * Стейт ЗДЕСЬ допустим (project-context §11): это не копия серверного
 * состояния для синхронизации, а результаты уже подтверждённых сервером
 * запросов (страница + вставленная строка).
 */

import { useState } from "react";

import { SupportForm } from "@/components/support/support-form";
import { Button } from "@/components/ui/button";
import { SupportTicketList, type SupportTicketView } from "@/components/support/support-list";

/** Ответ API на страницу списка (`SupportTicketsResponse`). */
type TicketsPage = {
  items: SupportTicketView[];
  total: number;
  next_cursor: string | null;
};

const DEFAULT_ERROR = "Не удалось загрузить обращения. Попробуйте ещё раз.";

/**
 * Проверяет форму страницы (NFR60: неизвестное ИГНОРИРУЕТСЯ).
 *
 * `as TicketsPage` доверял бы серверу форму без проверки: изменение контракта
 * дало бы `undefined.map` в рендере вместо пустой страницы.
 */
function isTicketsPage(value: unknown): value is TicketsPage {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    Array.isArray(candidate.items) &&
    typeof candidate.total === "number" &&
    (candidate.next_cursor === null || typeof candidate.next_cursor === "string")
  );
}

export function SupportTicketsBoard({
  initial,
  initialTotal,
  initialCursor,
}: {
  initial: SupportTicketView[];
  initialTotal: number;
  initialCursor: string | null;
}) {
  const [items, setItems] = useState<SupportTicketView[]>(initial);
  const [total, setTotal] = useState(initialTotal);
  const [cursor, setCursor] = useState<string | null>(initialCursor);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * Догружает следующую страницу по курсору.
   *
   * `credentials: "same-origin"` — BFF живёт на той же origin, и без этого
   * cookie сессии не уехала бы в прокси при кросс-доменной сборке.
   *
   * При ошибке курсор НЕ сбрасывается: пользователь нажимает кнопку повторно,
   * и страница читается с того же места (иначе часть списка потерялась бы).
   */
  async function loadMore(): Promise<void> {
    if (pending || cursor === null) return;

    setPending(true);
    setError(null);

    try {
      const query = new URLSearchParams({ cursor });
      const response = await fetch(`/api/proxy/support/tickets?${query.toString()}`, {
        credentials: "same-origin",
        cache: "no-store",
      });

      if (!response.ok) {
        setError(DEFAULT_ERROR);
        return;
      }

      const page: unknown = await response.json();
      if (!isTicketsPage(page)) {
        setError(DEFAULT_ERROR);
        return;
      }

      // Курсор страницы, а не наш: сервер мог вернуть `null` (последняя
      // страница) — тогда кнопка исчезает сама.
      setItems((current) => [...current, ...page.items]);
      setTotal(page.total);
      setCursor(page.next_cursor);
    } catch {
      setError(DEFAULT_ERROR);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <SupportForm
        onCreated={(ticket) => {
          setItems((current) => [ticket, ...current]);
          // LOW-3: счётчик «всего» обязан учитывать созданное обращение,
          // иначе человек видит «всего 3» и четыре строки в списке.
          setTotal((current) => current + 1);
        }}
      />
      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-lg font-semibold tracking-tight">Мои обращения</h2>
          {total > 0 ? (
            <span className="text-sm text-ink-secondary">Всего: {total}</span>
          ) : null}
        </div>
        <SupportTicketList items={items} />
        {error ? (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        ) : null}
        {cursor !== null ? (
          /* Примитив `Button`: `secondary` — второе действие экрана, тап-зона 48,
             `loading` даёт `aria-busy` без смены текста (§6.4). */
          <Button
            type="button"
            variant="secondary"
            className="self-start"
            onClick={() => void loadMore()}
            disabled={pending}
            loading={pending}
          >
            Показать ещё
          </Button>
        ) : null}
      </section>
    </div>
  );
}
