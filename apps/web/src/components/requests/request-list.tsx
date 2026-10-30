/**
 * Список заявившихся для автора (UX §863–865; стори 5а.4, Task 9.3; AC-01, AC-03, AC-05).
 *
 * **Серверный компонент**: список нужен до отрисовки (гард авторства и `404`
 * решает API, а не клиент), и клиентская загрузка показала бы пустой экран
 * с последующим «дёрганьем» — на списке, где автор выбирает человека, мигание
 * читается как нестабильность.
 *
 * **Четыре состояния одной функцией** (UX §895: компонент списка без
 * `EmptyState` не проходит ревью):
 * - данные есть → список строк;
 * - пусто → `EmptyState` с причиной (не пустой `<div>`);
 * - `404` → карточка недоступна (ADR-13: не автор и несуществующая
 *   неразличимы, и подсказывать причину значило бы раскрывать существование);
 * - сбой сети/сервера → «повторить», а не пустота: пустой список и сбой
 *   выглядят одинаково, если их не различить, и автор решил бы, что заявок нет.
 *
 * **Сноска про порядок** (UX §865): «Порядок — по времени подачи» объясняет,
 * почему первым идёт тот, кто подал раньше. Без неё порядок читается как
 * случайный или как «лучшие сверху» — второе прямо запрещено UX §863.
 */

import { EmptyState } from "@/components/ui/empty-state";
import { RequestListRow } from "@/components/requests/request-list-row";

/** Форма ответа `GET /api/v1/cards/{uuid}/requests` (§7.2). */
export type CardRequestsPerson = {
  uuid: string;
  name: string | null;
  age: number | null;
  photo_url: string | null;
  photo_expires_in: number | null;
};

export type CardRequestsItem = {
  uuid: string;
  submitted_at: string;
  message: string;
  person: CardRequestsPerson;
  /**
   * Стори 12.6 (AC-3, Task 7.4): проходит ли заявитель рамки карточки.
   *
   * `true`/`false` — рамки заданы и вердикт известен; `null` — рамок нет
   * («проверять нечего»). Поле приходит ТОЛЬКО автору: в `GET /my/requests`
   * его нет (AC-4), и заявитель не узнаёт, что его отсеяли по возрасту.
   *
   * Обязательное, без умолчания: умолчание `true` сделало бы «вне рамок» и
   * «забыли заполнить» неотличимыми.
   */
  within_bounds: boolean | null;
};

export type CardRequestsResponse = {
  items: CardRequestsItem[];
  total: number;
  cursor: string | null;
  mode: "requests" | "open_set";
  selection_available: boolean;
};

/** Состояние загрузки списка: данные, «нет доступа», «сервис недоступен». */
export type RequestListState =
  | { kind: "ok"; data: CardRequestsResponse }
  | { kind: "not_found" }
  | { kind: "unavailable" };

const API_TIMEOUT_MS = 5_000;

/**
 * Загружает список заявившихся для автора.
 *
 * `404` отделён от прочих ошибок намеренно: для не-автора и для
 * несуществующей карточки ответ один и тот же (ADR-13), и показывать
 * «повторите» на `404` значило бы предлагать действие, которое не поможет.
 */
export async function loadCardRequests(
  cardUuid: string,
  cookieHeader: string | null,
): Promise<RequestListState> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(
      `${apiUrl}/api/v1/cards/${encodeURIComponent(cardUuid)}/requests`,
      {
        headers: cookieHeader ? { cookie: cookieHeader } : {},
        cache: "no-store",
        signal: AbortSignal.timeout(API_TIMEOUT_MS),
      },
    );
    if (response.status === 404) return { kind: "not_found" };
    if (!response.ok) return { kind: "unavailable" };
    return { kind: "ok", data: (await response.json()) as CardRequestsResponse };
  } catch {
    return { kind: "unavailable" };
  }
}

type RequestListProps = {
  state: RequestListState;
  /**
   * AC-10 стори 5б.2: состояние карточки — кнопка «Выбрать» активна только
   * при `published`.
   *
   * Передаётся СПИСКОМ, а не читается строкой: список остаётся серверным
   * (D-5b.2-8), и второй `GET` карточки ради одного поля был бы лишним
   * запросом на самый частый экран автора.
   */
  cardStatus: string;
};

export function RequestList({ state, cardStatus }: RequestListProps) {
  if (state.kind === "not_found") {
    return (
      <EmptyState
        title="Список недоступен"
        reason="Заявившихся видит только автор встречи."
        actionKind="quiet"
      />
    );
  }

  if (state.kind === "unavailable") {
    return (
      <EmptyState
        title="Не удалось загрузить заявившихся"
        reason="Проверьте связь и обновите страницу."
        actionKind="quiet"
      />
    );
  }

  const { items, selection_available: selectionAvailable, mode } = state.data;

  if (items.length === 0) {
    return (
      <EmptyState
        title={
          mode === "open_set" ? "Пока никто не присоединился" : "Пока никто не заявился"
        }
        reason={
          mode === "open_set"
            ? "Как только кто-то присоединится, он появится здесь."
            : "Заявки появятся здесь — вы увидите профиль и сообщение."
        }
        actionKind="quiet"
      />
    );
  }

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold tracking-tight text-ink">
        {mode === "open_set" ? "Кто идёт" : "Заявились"}
        <span className="ml-2 text-sm font-normal text-ink-secondary">{state.data.total}</span>
      </h2>
      <ul className="flex flex-col gap-3">
        {items.map((item) => (
          <RequestListRow
            key={item.uuid}
            requestUuid={item.uuid}
            person={item.person}
            message={item.message}
            selectionAvailable={selectionAvailable && mode === "requests"}
            cardPublished={cardStatus === "published"}
            withinBounds={item.within_bounds}
          />
        ))}
      </ul>
      {/* UX §865: порядок объясняется, иначе он читается как «лучшие сверху». */}
      <p className="text-sm text-ink-secondary">Порядок — по времени подачи.</p>
    </section>
  );
}
