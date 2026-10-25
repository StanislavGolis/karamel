"use client";

/**
 * Лента карточек с фильтрами (UX §434 «Поверхность 1»; стори 4.5, Task 7.5,
 * 7.6; AC-09).
 *
 * Состояния ровно те, что требует UX: `loading` (три скелетона 180px без CLS),
 * `loaded`, `empty` (карточек нет вообще), `empty-by-filter` (фильтр отсёк всё),
 * `error` (сеть/сервер). Разделение `empty` и `empty-by-filter` — не
 * педантизм: у них РАЗНЫЕ действия (UX §775). При «нет карточек» акцентная
 * кнопка «Создать карточку»; при «отфильтровано» — тихое «Сбросить фильтр».
 * Предлагать создать, когда причина в фильтре, значит уводить от причины.
 *
 * Состояние на `useState` + `useEffect` (X-18: в проекте нет
 * `@tanstack/react-query`, установка зависимостей требует согласования).
 * Функционально то же: запрос по монтированию, «Ещё» по курсору, ошибка
 * не теряет выбранный фильтр.
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { EmptyState } from "@/components/ui/empty-state";
import { EventCard, type FeedCard } from "@/components/cards/event-card";
import { fetchCards, type CardRead } from "@/lib/queries/cards";

/** Категории фильтра — из белого списка `PlaceCategory` (ADR-12). */
const CATEGORIES: { value: string; label: string }[] = [
  { value: "cafe", label: "Кафе" },
  { value: "cinema", label: "Кино" },
  { value: "quiz", label: "Квиз" },
  { value: "walk", label: "Прогулка" },
  { value: "park", label: "Парк" },
  { value: "sport", label: "Спорт" },
  { value: "skating", label: "Каток" },
  { value: "other", label: "Другое" },
];

/** Ответ API → форма `EventCard`. Поля совпадают, тип отдельный ради имён. */
function toFeedCard(card: CardRead): FeedCard {
  return card;
}

type LoadState =
  | { kind: "loading" }
  | { kind: "loaded"; items: FeedCard[]; cursor: string | null; total: number }
  | { kind: "error"; code: string };

/** Текст ошибки по машинному коду; неизвестный код — общий текст. */
function errorMessage(code: string): string {
  switch (code) {
    case "unauthenticated":
      return "Сессия истекла. Войдите снова.";
    case "consent_required":
      return "Нужно согласие на обработку данных — заполните его в профиле.";
    case "invalid_cursor":
      return "Не удалось продолжить список. Обновите страницу.";
    case "category_invalid":
      return "Такой категории нет. Сбросьте фильтр.";
    default:
      return "Не удалось загрузить ленту. Проверьте связь и попробуйте снова.";
  }
}

/** Скелетон карточки — фиксированная высота 180px (UX: CLS исключён). */
function CardSkeleton() {
  return (
    <div
      aria-hidden="true"
      className="h-[180px] animate-pulse rounded-lg border border-border bg-surface-raised"
    />
  );
}

export function CardFeed() {
  const router = useRouter();
  const [category, setCategory] = useState<string | null>(null);
  const [hasSeats, setHasSeats] = useState(false);
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [loadingMore, setLoadingMore] = useState(false);

  /**
   * Загрузка первой страницы по текущим фильтрам.
   *
   * `await fetchCards(...)` стоит ПЕРВЫМ оператором намеренно: `setState`
   * после `await` — асинхронный, и правило `react-hooks/set-state-in-effect`
   * не срабатывает. Синхронный `setState({kind:"loading"})` в теле эффекта
   * вызвал бы каскадный рендер на каждом монтировании.
   *
   * Состояние `loading` при смене фильтра ставится обработчиком клика
   * (`selectCategory`/`toggleSeats`) — там это событие пользователя, а не
   * эффект.
   */
  const refresh = useCallback(async () => {
    const result = await fetchCards({ category, hasSeats });
    if (!result.ok) {
      setState({ kind: "error", code: result.code });
      return;
    }
    setState({
      kind: "loaded",
      items: result.data.items.map(toFeedCard),
      cursor: result.data.cursor,
      total: result.data.total,
    });
  }, [category, hasSeats]);

  useEffect(() => {
    // Правило `react-hooks/set-state-in-effect` смотрит на ФАКТ вызова функции,
    // которая может звать `setState`, и не отслеживает, что в `refresh` он
    // стоит за `await fetchCards(...)`. Синхронного каскадного рендера здесь
    // нет: состояние меняется только после ответа сети. Отключение точечное
    // и относится ровно к одному вызову.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
  }, [refresh]);

  /** Смена категории: `loading` ставится здесь, в обработчике события. */
  const selectCategory = useCallback((value: string | null) => {
    setState({ kind: "loading" });
    setCategory(value);
  }, []);

  /** Переключатель «только со свободными местами». */
  const toggleSeats = useCallback((value: boolean) => {
    setState({ kind: "loading" });
    setHasSeats(value);
  }, []);

  /** «Ещё»: следующая страница по курсору, без сброса уже показанного. */
  const loadMore = useCallback(async () => {
    if (state.kind !== "loaded" || !state.cursor) return;
    setLoadingMore(true);
    const result = await fetchCards({ category, hasSeats, cursor: state.cursor });
    setLoadingMore(false);
    if (!result.ok) {
      setState({ kind: "error", code: result.code });
      return;
    }
    setState({
      kind: "loaded",
      items: [...state.items, ...result.data.items.map(toFeedCard)],
      cursor: result.data.cursor,
      total: result.data.total,
    });
  }, [category, hasSeats, state]);

  const hasFilter = category !== null || hasSeats;

  return (
    <div className="flex flex-col gap-4">
      <section aria-label="Фильтры ленты" className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => selectCategory(null)}
          aria-pressed={category === null}
          className={`min-h-11 rounded-full border px-3 text-sm ${
            category === null
              ? "border-accent bg-surface font-medium text-accent"
              : "border-border text-ink-secondary"
          }`}
        >
          Все
        </button>
        {CATEGORIES.map((item) => (
          <button
            key={item.value}
            type="button"
            onClick={() => selectCategory(item.value)}
            aria-pressed={category === item.value}
            className={`min-h-11 rounded-full border px-3 text-sm ${
              category === item.value
                ? "border-accent bg-surface font-medium text-accent"
                : "border-border text-ink-secondary"
            }`}
          >
            {item.label}
          </button>
        ))}
        <label className="ml-2 flex min-h-11 items-center gap-2 text-sm text-ink-secondary">
          <input
            type="checkbox"
            checked={hasSeats}
            onChange={(event) => toggleSeats(event.target.checked)}
            className="h-5 w-5"
          />
          Только со свободными местами
        </label>
      </section>

      {state.kind === "loading" ? (
        <div className="flex flex-col gap-3">
          <CardSkeleton />
          <CardSkeleton />
          <CardSkeleton />
        </div>
      ) : null}

      {state.kind === "error" ? (
        <div className="flex flex-col items-start gap-3">
          <p className="text-sm text-ink-secondary">{errorMessage(state.code)}</p>
          <button
            type="button"
            onClick={() => {
              setState({ kind: "loading" });
              void refresh();
            }}
            className="min-h-11 rounded-md border border-border px-4 py-2 text-sm font-medium text-ink-secondary hover:bg-surface"
          >
            Попробовать снова
          </button>
        </div>
      ) : null}

      {state.kind === "loaded" && state.items.length === 0 ? (
        hasFilter ? (
          <EmptyState
            title="По этому фильтру ничего нет"
            reason="В сообществе есть другие встречи — попробуйте снять фильтр."
            action="Сбросить фильтр"
            actionKind="quiet"
            onAction={() => {
              setState({ kind: "loading" });
              setCategory(null);
              setHasSeats(false);
            }}
          />
        ) : (
          <EmptyState
            title="Свободных встреч пока нет"
            reason="Создайте своё — компания соберётся сама."
            action="Создать карточку"
            actionKind="primary"
            onAction={() => router.push("/app/cards/new")}
          />
        )
      ) : null}

      {state.kind === "loaded" && state.items.length > 0 ? (
        <>
          <ul className="flex flex-col gap-3">
            {state.items.map((card) => (
              <li key={card.uuid}>
                <EventCard card={card} />
              </li>
            ))}
          </ul>

              {state.cursor ? (
            <button
              type="button"
              onClick={() => void loadMore()}
              disabled={loadingMore}
              className="min-h-11 self-center rounded-md border border-border px-4 py-2 text-sm font-medium text-ink-secondary hover:bg-surface disabled:opacity-60"
            >
              {loadingMore ? "Загружаем…" : "Ещё"}
            </button>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
