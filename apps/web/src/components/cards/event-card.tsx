/**
 * Карточка события в ленте (UX §814, `EventCard`; стори 4.5, Task 7.3; AC-09).
 *
 * **Один компонент на все статусы** (`published`/`full`/`cancelled`/`own`):
 * UX §893 прямо запрещает отдельные компоненты на статус — иначе через три
 * стори в проекте окажется четыре карточки с разной геометрией, и лента
 * начнёт «дёргаться» при смене статуса у той же встречи.
 *
 * **Доступность (NFR57):** вся карточка — одна ссылка с `aria-label` вида
 * «Кино, суббота 19:00, Кино Прага, свободно 2 места». Статус и остаток мест
 * передаются **текстом**, не только цветом: цвет — украшение, а не носитель
 * смысла. Тап-зоны ≥44px (`min-h-11`).
 *
 * Анатомия по UX: время (18px/600) → место + категория (приглушённая) →
 * статус набора + срок (14px) → аватар + имя, возраст → описание (≤3 строки) →
 * действие (48px).
 */

import Link from "next/link";

import { CountdownBadge } from "@/components/cards/countdown-badge";
import { SeatsIndicator } from "@/components/cards/seats-indicator";

/** Форма карточки из `GET /api/v1/cards` (§7.2 архитектуры). */
export type FeedCard = {
  uuid: string;
  title: string;
  description: string | null;
  category: string;
  starts_at: string;
  expires_at: string;
  // AC-04 стори 5б.1 (NFR14): серверный якорь отсчёта окна решения.
  server_time: string;
  capacity: number;
  filled_count: number;
  status: "draft" | "published" | "full" | "closed" | "finished" | "cancelled";
  verification_status: string;
  // X-36 (стори 5а.4, AC-08): место может быть СКРЫТО модератором
  // (`places.is_active=false`) — тогда `null`, и карточка остаётся доступной
  // по прямой ссылке (заявки и участники живы). В ленту такие карточки
  // не попадают вовсе, поэтому `null` встречается на детальном экране.
  place: { uuid: string; name: string; address_text: string } | null;
  author: { uuid: string; name: string | null; age: number | null };
  chat_uuid: string | null;
  /**
   * AC-09 стори 5б.2 (D-5b.2-7, FR104): зритель в составе компании.
   *
   * Признак ПРО ЗРИТЕЛЯ, не про карточку: `filled_count` говорит «сколько
   * мест занято», а не «занял ли одно из них я». Считает сервер (в ленте —
   * одним пакетным запросом), клиент только ветвится по флагу.
   *
   * Необязательное поле в типе, хотя сервер отдаёт его всегда: снимки
   * ответов и тестовые данные до 5б.2 поля не имеют, и обязательность
   * сломала бы их без пользы — `false` здесь безопасное умолчание
   * («наблюдатель»).
   */
  viewer_participates?: boolean;
};

const WEEKDAYS = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"] as const;
const WEEKDAYS_FULL = [
  "воскресенье",
  "понедельник",
  "вторник",
  "среду",
  "четверг",
  "пятницу",
  "субботу",
] as const;

/** «Сб, 19:00» — крупная строка карточки. */
export function formatStartsAt(startsAt: string): string {
  const date = new Date(startsAt);
  if (Number.isNaN(date.getTime())) return "";
  const day = WEEKDAYS[date.getDay()];
  const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  return `${day[0].toUpperCase()}${day.slice(1)}, ${time}`;
}

/** «в субботу в 19:00» — для `aria-label`. */
export function formatStartsAtAccessible(startsAt: string): string {
  const date = new Date(startsAt);
  if (Number.isNaN(date.getTime())) return "";
  const day = WEEKDAYS_FULL[date.getDay()];
  const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  return `в ${day} в ${time}`;
}

/**
 * Текст статуса для карточки. Пустая строка — статус не показывается.
 *
 * `full` и `closed` различимы намеренно (модель 4.1): «Компания собрана» и
 * «Набор закрыт» — разные факты, и участник по-разному решает, идти ли.
 */
export function statusLabel(card: FeedCard): string {
  switch (card.status) {
    case "full":
      return "Компания собрана";
    case "closed":
      return "Набор закрыт автором";
    case "cancelled":
      return "Встреча отменена автором";
    case "finished":
      return "Встреча прошла";
    case "draft":
      return "Черновик — виден только вам";
    default:
      return "";
  }
}

/**
 * Подпись действия. `null` — действия нет (UX §814: у `full`/`cancelled` кнопки нет).
 *
 * «Подать заявку» при `capacity = 1` (режим заявок, FR70) и «Присоединиться»
 * при 2–6 — разные действия с разными последствиями; одно слово на оба случая
 * заставляло бы человека угадывать.
 */
export function actionLabel(card: FeedCard): string | null {
  if (card.status === "full" || card.status === "cancelled" || card.status === "finished") {
    return null;
  }
  if (card.status === "closed") return null;
  return card.capacity === 1 ? "Подать заявку" : "Присоединиться";
}

type EventCardProps = {
  card: FeedCard;
  /** `true` — карточка автора («Ваша карточка» + «Открыть»). */
  isOwn?: boolean;
};

export function EventCard({ card, isOwn = false }: EventCardProps) {
  const status = statusLabel(card);
  const action = actionLabel(card);
  const free = card.capacity - card.filled_count;

  // AC-09, UX §814: `aria-label` несёт время, место и остаток мест — то,
  // ради чего человек читает карточку. Без него скринридер объявил бы
  // «ссылка» и набор разрозненных слов.
  const label = [
    card.title,
    formatStartsAtAccessible(card.starts_at),
    // X-36: место скрыто модератором — в подписи его просто нет. `filter(Boolean)`
    // ниже убирает `null`, поэтому «, ,» в озвучке не появляется.
    card.place?.name,
    free > 0 ? `свободно ${free}` : "компания собрана",
  ]
    .filter(Boolean)
    .join(", ");

  return (
    <article className="rounded-lg border border-[#E4DCD1] bg-white">
      <Link
        href={`/app/cards/${card.uuid}`}
        aria-label={label}
        className="flex min-h-11 flex-col gap-2 p-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#7A3E12]"
      >
        <header className="flex items-start justify-between gap-3">
          <p className="text-lg font-semibold tracking-tight text-[#2B241C]">
            {formatStartsAt(card.starts_at)}
          </p>
              <span className="shrink-0 text-sm font-medium text-[#3D3529]">
            {free > 0 ? <SeatsIndicator capacity={card.capacity} filledCount={card.filled_count} /> : "Компания собрана"}
          </span>
        </header>

        <p className="text-base text-[#5A5249]">
          {/* X-36: место скрыто — показываем явную пометку, а не пустоту.
              Молчаливое «· cafe» без названия читалось бы как дефект вёрстки. */}
          {card.place ? `${card.place.name} · ${card.category}` : "Место скрыто"}
        </p>

          {status ? (
          <p className="text-sm font-medium text-[#5A5249]">{status}</p>
        ) : null}

        <p className="text-base font-medium text-[#2B241C]">{card.title}</p>

          {card.status === "published" && free > 0 ? (
          <CountdownBadge expiresAt={card.expires_at} serverTime={card.server_time} />
        ) : null}

          {card.description ? (
          <p className="line-clamp-3 text-sm leading-relaxed text-[#5A5249]">
            {card.description}
          </p>
        ) : null}

        <footer className="flex items-center gap-2 pt-1">
              <span
            aria-hidden="true"
            className="flex h-10 w-10 items-center justify-center rounded-full bg-[#F2EDE6] text-sm font-medium text-[#3D3529]"
          >
            {(card.author.name ?? "?").slice(0, 1).toUpperCase()}
          </span>
          <span className="text-sm text-[#5A5249]">
            {card.author.name ?? "Участник"}
            {card.author.age ? `, ${card.author.age}` : ""}
          </span>
        </footer>
      </Link>

      {isOwn ? (
        <div className="border-t border-[#F2EDE6] px-4 py-3">
          <span className="text-sm font-medium text-[#3D3529]">Ваша карточка</span>
        </div>
      ) : action ? (
        <div className="border-t border-[#F2EDE6] px-4 py-3">
              <Link
            href={`/app/cards/${card.uuid}`}
            className="inline-flex min-h-11 items-center rounded-md bg-[#7A3E12] px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#7A3E12]"
          >
            {action}
          </Link>
        </div>
      ) : null}
    </article>
  );
}
