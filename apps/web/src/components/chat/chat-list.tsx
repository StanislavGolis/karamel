"use client";

/**
 * Список чатов с непрочитанными (стори 6.6, Task 6.2–6.4; AC-01, AC-02, AC-04).
 *
 * Состояния ровно те, что требует UX для любого списка:
 * `loading` (скелетоны фиксированной высоты, без CLS), `loaded`, `empty`
 * (`EmptyState` с объяснением, а не пустой `<div>`), `error` (сеть/сервер)
 * и `not_found` (`404` — ответ БЕЗ деталей, ADR-13).
 *
 * **Скелетоны, а не спиннеры** (UX-правило проекта): спиннер по центру
 * сдвигает контент и читается как «зависло»; скелетон держит габариты строк.
 *
 * **Счётчик непрочитанных — ТЕКСТОМ** (NFR57): цвет не единственный носитель
 * смысла, иначе человек с дальтонизмом не отличит «2 новых» от «прочитано».
 * При нуле счётчик НЕ рисуется: «0 непрочитанных» — шум, а не факт.
 *
 * Состояние на `useState` + `useEffect` (X-18: `@tanstack/react-query` в
 * проекте нет, установка зависимостей требует согласования). Функционально
 * то же: запрос по монтированию, «Ещё» по курсору, ошибка не теряет список.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchChats, type ChatListItem } from "@/lib/queries/chats";

type LoadState =
  | { kind: "loading" }
  | { kind: "loaded"; items: ChatListItem[]; cursor: string | null; total: number }
  | { kind: "empty" }
  | { kind: "not_found" }
  | { kind: "error"; code: string };

/** Текст ошибки по МАШИННОМУ коду; неизвестный код — общий текст (§4). */
function errorMessage(code: string): string {
  switch (code) {
    case "unauthenticated":
      return "Сессия истекла. Войдите снова.";
    case "consent_required":
      return "Нужно согласие на обработку данных — заполните его в профиле.";
    case "invalid_cursor":
      return "Не удалось продолжить список. Обновите страницу.";
    case "service_unavailable":
      return "Сервис недоступен. Попробуйте через минуту.";
    default:
      // §8.3: причина НЕ повторяет заголовок `EmptyState` — иначе один и тот
      // же текст звучит дважды, а тест не отличает заголовок от причины.
      return "Проверьте связь и попробуйте снова.";
  }
}

/** ISO 8601 UTC → «19:34» в локальной зоне читателя. */
function timeOf(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

/**
 * Подпись счётчика с русским склонением.
 *
 * «2 непрочитанных» — форма для 2–4; «1 непрочитанное» — для одного;
 * «5 непрочитанных» и «21 непрочитанное» — по тем же правилам. Число
 * остаётся в тексте: оно и есть факт, который нужен человеку.
 */
function unreadLabel(count: number): string {
  const mod100 = count % 100;
  const mod10 = count % 10;
  if (mod100 >= 11 && mod100 <= 14) return `${count} непрочитанных`;
  if (mod10 === 1) return `${count} непрочитанное`;
  if (mod10 >= 2 && mod10 <= 4) return `${count} непрочитанных`;
  return `${count} непрочитанных`;
}

/** Скелетон строки — фиксированная высота (UX: CLS исключён).
 *  Примитив `Skeleton` (§6.3): `aria-hidden`, `surface`, гасится при reduced-motion. */
function RowSkeleton() {
  return <Skeleton kind="row" className="h-[72px] rounded-lg border border-border" />;
}

export function ChatList() {
  const router = useRouter();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [loadingMore, setLoadingMore] = useState(false);

  /**
   * Контроллер первой страницы (X-6.4-1): уход со страницы не должен
   * оставлять запрос в полёте, а ответ на старый запрос — перетирать новый.
   */
  const firstPageAbort = useRef<AbortController | null>(null);

  /**
   * Загрузка первой страницы.
   *
   * `await fetchChats(...)` стоит ПЕРВЫМ оператором: `setState` после
   * `await` — асинхронный, и правило `react-hooks/set-state-in-effect` не
   * срабатывает. Синхронный `setState` в теле эффекта дал бы каскадный
   * рендер на каждом монтировании (тот же приём, что в `CardFeed`).
   */
  const refresh = useCallback(async () => {
    firstPageAbort.current?.abort();
    const controller = new AbortController();
    firstPageAbort.current = controller;

    const result = await fetchChats();
    if (controller.signal.aborted) return;

    if (!result.ok) {
      // ADR-13: `404` — отдельное состояние. Общий текст ошибки заставлял бы
      // человека «пробовать снова» там, где повтор не поможет.
      if (result.code === "chat_not_found" || result.code === "http_404") {
        setState({ kind: "not_found" });
        return;
      }
      setState({ kind: "error", code: result.code });
      return;
    }
    if (result.data.items.length === 0) {
      setState({ kind: "empty" });
      return;
    }
    setState({
      kind: "loaded",
      items: result.data.items,
      cursor: result.data.cursor,
      total: result.data.total,
    });
  }, []);

  useEffect(() => {
    // Синхронного каскадного рендера нет: состояние меняется только после
    // ответа сети (правило не отслеживает `await` внутри `refresh`).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
    return () => firstPageAbort.current?.abort();
  }, [refresh]);

  /** «Ещё»: следующая страница по курсору, без сброса уже показанного. */
  const loadMore = useCallback(async () => {
    if (state.kind !== "loaded" || !state.cursor) return;
    setLoadingMore(true);
    const result = await fetchChats(state.cursor);
    setLoadingMore(false);
    if (!result.ok) {
      setState({ kind: "error", code: result.code });
      return;
    }
    setState({
      kind: "loaded",
      items: [...state.items, ...result.data.items],
      cursor: result.data.cursor,
      total: result.data.total,
    });
  }, [state]);

  if (state.kind === "loading") {
    return (
      <div aria-busy="true" aria-label="Загружаем чаты" className="flex flex-col gap-3">
        <RowSkeleton />
        <RowSkeleton />
        <RowSkeleton />
        <span className="sr-only">Загружаем чаты</span>
      </div>
    );
  }

  if (state.kind === "not_found") {
    return (
      <EmptyState
        title="Чат недоступен"
        reason="Возможно, диалог скрыт или ссылка устарела. Вернитесь к списку встреч."
        action="К встречам"
        actionKind="quiet"
        onAction={() => {
          // `useRouter().push`: навигация внутри Next без полной перезагрузки
          // (правило `@next/next/no-location-assign-relative-destination`).
          router.push("/app");
        }}
      />
    );
  }

  if (state.kind === "error") {
    return (
      /* §8.3: ошибка списка — `EmptyState` с действием повтора, а не голая
         строка. Причина берётся из кода ответа: «сессия истекла» и «сервис
         недоступен» требуют разных действий, и общий текст врал бы. */
      <EmptyState
        title="Не удалось загрузить чаты"
        reason={errorMessage(state.code)}
        action="Повторить"
        actionKind="quiet"
        onAction={() => {
          setState({ kind: "loading" });
          void refresh();
        }}
      />
    );
  }

  if (state.kind === "empty") {
    return (
      <EmptyState
        title="Чатов пока нет"
        reason="Чат открывается сам, когда компания собрана: примите участие во встрече или создайте свою."
        action="К встречам"
        actionKind="primary"
        onAction={() => {
          router.push("/app");
        }}
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <ul className="flex flex-col gap-3">
        {state.items.map((item) => (
          <li key={item.chat_uuid}>
            <Link
              href={`/app/chats/${item.chat_uuid}`}
              className="flex min-h-[72px] flex-col gap-1 rounded-lg border border-border bg-surface-raised p-4 transition-colors hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
            >
              <span className="flex items-baseline justify-between gap-3">
                <span className="text-base font-semibold text-ink">
                  {item.card_title}
                </span>
                <time
                  dateTime={item.last_activity_at}
                  className="shrink-0 text-sm text-ink-secondary"
                >
                  {timeOf(item.last_activity_at)}
                </time>
              </span>
              {item.last_message ? (
                <span className="truncate text-sm text-ink-secondary">
                  {item.last_message.body}
                </span>
              ) : (
                <span className="text-sm text-ink-muted">Сообщений пока нет</span>
              )}
              {item.unread_count > 0 ? (
                <span className="text-sm font-medium text-accent">
                  {unreadLabel(item.unread_count)}
                </span>
              ) : null}
            </Link>
          </li>
        ))}
      </ul>

      {state.cursor ? (
        <button
          type="button"
          onClick={() => void loadMore()}
          disabled={loadingMore}
          className="min-h-11 self-center rounded-md border border-border px-4 py-2 text-sm font-medium text-ink hover:bg-surface-hover disabled:opacity-60"
        >
          {loadingMore ? "Загружаем…" : "Ещё"}
        </button>
      ) : null}

      <p className="text-sm text-ink-secondary">Всего чатов: {state.total}</p>
    </div>
  );
}
