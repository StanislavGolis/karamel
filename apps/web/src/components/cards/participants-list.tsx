/**
 * Состав компании (UX `EventCard` §2б; стори 6.3, Task 5.1; AC-01, AC-03).
 *
 * **Серверный компонент**: состав нужен до отрисовки, а `404` для постороннего
 * решает API (ADR-13), не клиент. Клиентская загрузка показала бы сначала
 * пустоту, потом список — на экране, где человек проверяет «с кем он идёт»,
 * мигание читается как нестабильность.
 *
 * **Три состояния одной функцией** (UX §873: любой список обязан отрисовать
 * `EmptyState`, а не пустой `<div>`):
 * - данные есть → аватар + «Имя, возраст», автор помечен «Автор»;
 * - `404` → блок не рисуется ВООБЩЕ: посторонний не должен узнать, что состав
 *   существует, а «нет доступа» с подсказкой раскрыло бы карточку (ADR-13);
 * - сбой сети → «не удалось загрузить» с просьбой обновить: пустой список и
 *   сбой выглядят одинаково, если их не различить.
 *
 * **Никаких рейтингов и счётчиков встреч** (UX §877): этих полей нет и в
 * контракте API, и в компоненте нет даже типа под них — то, чего нет, нельзя
 * случайно отрисовать.
 */

import { PersonMini } from "@/components/ui/person-mini";
import { EmptyState } from "@/components/ui/empty-state";

/** Один участник в ответе `GET /api/v1/cards/{uuid}/participants`. */
export type CardParticipant = {
  uuid: string;
  name: string | null;
  age: number | null;
  is_author: boolean;
  photo_url: string | null;
  photo_expires_in: number | null;
};

export type CardParticipantsResponse = {
  card: { uuid: string };
  items: CardParticipant[];
  total: number;
};

/** Состояние загрузки состава: данные, «нет доступа», «сервис недоступен». */
export type ParticipantsState =
  | { kind: "ok"; data: CardParticipantsResponse }
  | { kind: "not_found" }
  | { kind: "unavailable" };

const API_TIMEOUT_MS = 5_000;

/**
 * Загружает состав компании.
 *
 * `404` отделён от прочих ошибок намеренно: для постороннего и для
 * несуществующей карточки ответ один (ADR-13), и показывать «повторите» на
 * `404` значило бы предлагать действие, которое не поможет.
 */
export async function loadCardParticipants(
  cardUuid: string,
  cookieHeader: string | null,
): Promise<ParticipantsState> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(
      `${apiUrl}/api/v1/cards/${encodeURIComponent(cardUuid)}/participants`,
      {
        headers: cookieHeader ? { cookie: cookieHeader } : {},
        cache: "no-store",
        signal: AbortSignal.timeout(API_TIMEOUT_MS),
      },
    );
    if (response.status === 404) return { kind: "not_found" };
    if (!response.ok) return { kind: "unavailable" };
    return { kind: "ok", data: (await response.json()) as CardParticipantsResponse };
  } catch {
    return { kind: "unavailable" };
  }
}

type ParticipantsListProps = {
  state: ParticipantsState;
};

export function ParticipantsList({ state }: ParticipantsListProps) {
  // AC-02 (ADR-13): «нет доступа» не рисует НИЧЕГО. Placeholder с текстом
  // «состав скрыт» подтвердил бы существование компании постороннему.
  if (state.kind === "not_found") return null;

  if (state.kind === "unavailable") {
    return (
      <EmptyState
        title="Не удалось загрузить компанию"
        reason="Проверьте связь и обновите страницу."
        actionKind="quiet"
      />
    );
  }

  const { items, total } = state.data;

  return (
    <section className="flex flex-col gap-3">
      {/* AC-03, NFR57: статус — всегда ТЕКСТОМ («Компания собрана»), а не
          только цветом или иконкой. `total` показывается числом: компания
          2–6 человек — продуктовый факт (FR85), а не догадка клиента. */}
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold tracking-tight">Компания собрана</h2>
        <span className="text-sm text-ink-secondary">{total} человек</span>
      </div>
      <ul className="flex flex-col gap-3">
        {items.map((member) => (
          <li key={member.uuid} className="flex items-center justify-between gap-3">
            <PersonMini
              name={member.name}
              age={member.age}
              photoUrl={member.photo_url}
            />
            {member.is_author ? (
              <span className="shrink-0 rounded-md border border-border px-2 py-1 text-xs font-medium text-ink-secondary">
                Автор
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
