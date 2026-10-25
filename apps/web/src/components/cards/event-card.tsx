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

import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
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
  /**
   * Карточка из ленты. Необязательна ради состояния `loading` (§8.3):
   * скелетон обязан занимать те же 176px, поэтому он рисуется тем же
   * компонентом, а не отдельной заглушкой в ленте.
   */
  card?: FeedCard;
  /** `true` — карточка автора («Ваша карточка» + «Открыть»). */
  isOwn?: boolean;
  /** §8.3: `loading` — скелетон 176px в габаритах готовой карточки. */
  loading?: boolean;
  /**
   * §8.3: `offline` — полоса `warning` сверху.
   *
   * Чтение не блокируется (§8.4 п.5): показан последний известный кэш,
   * ограничивается только запись — это делает `JoinRequestSheet`.
   */
  offline?: boolean;
  /** §8.3: `stale` — «Обновляем…», кнопка остаётся активной. */
  stale?: boolean;
};

/**
 * Тон статуса по §2.3.
 *
 * **Жёсткое правило §2.3: `danger` не применяется ни к одному штатному
 * состоянию карточки.** Красный — сбой системы. Поэтому отменённая встреча —
 * `muted`, а не `danger`: встреча отменена автором, это факт домена, а не сбой.
 *
 * `null` — статус не показывается (карточка в наборе, статус не нужен).
 */
export function statusTone(
  status: FeedCard["status"],
): "success" | "neutral" | "muted" | null {
  switch (status) {
    case "published":
      return "success";
    case "full":
      return "neutral";
    case "cancelled":
      return "muted";
    case "closed":
    case "finished":
    case "draft":
      return "neutral";
    default:
      return null;
  }
}

/**
 * Скелетон карточки (§8.3, §8.4 п.1).
 *
 * 176px — те же габариты, что у готовой карточки: иначе при появлении данных
 * лента дёргается и NFR8 (CLS) проваливается. `Skeleton kind="card"` уже
 * задаёт `h-44` (=176px), поэтому высота не дублируется числом.
 */
export function EventCardSkeleton() {
  return <Skeleton kind="card" className="rounded-lg border border-border" />;
}

export function EventCard({ card, isOwn = false, loading = false, offline = false, stale = false }: EventCardProps) {
  // §8.3: `loading` — скелетон. Пустая карточка без флага — тоже скелетон:
  // рисовать «полукарточку» из `undefined` хуже, чем показать заглушку.
  if (loading || !card) return <EventCardSkeleton />;

  const status = statusLabel(card);
  const tone = statusTone(card.status);
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
    <article className="overflow-hidden rounded-lg border border-border bg-surface">
      {/* §8.3: `offline` — полоса `warning` сверху. Чтение не блокируется
          (§8.4 п.5): человек видит последний известный кэш и понимает, что он
          мог устареть. Запись блокирует `JoinRequestSheet`. */}
      {offline ? (
        <p className="bg-warning-tint px-4 py-2 text-sm font-medium text-warning">
          Нет связи. Показаны последние известные данные.
        </p>
      ) : null}

      <Link
        href={`/app/cards/${card.uuid}`}
        aria-label={label}
        className="flex min-h-11 flex-col gap-2 p-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
      >
        <header className="flex items-start justify-between gap-3">
          <p className="text-lg font-semibold tracking-tight text-ink">
            {formatStartsAt(card.starts_at)}
          </p>
          <span className="shrink-0 text-sm font-medium text-ink-secondary">
            {free > 0 ? (
              <SeatsIndicator capacity={card.capacity} filledCount={card.filled_count} />
            ) : (
              "Компания собрана"
            )}
          </span>
        </header>

        <p className="text-base text-ink-secondary">
          {/* X-36: место скрыто — показываем явную пометку, а не пустоту.
              Молчаливое «· cafe» без названия читалось бы как дефект вёрстки. */}
          {card.place ? `${card.place.name} · ${card.category}` : "Место скрыто"}
        </p>

        {/* §2.3: статус — `Badge` с тоном по смыслу. Текст обязателен:
            цвет не носитель смысла (NFR57). */}
        {status && tone ? <Badge variant={tone}>{status}</Badge> : null}

        <p className="text-base font-medium text-ink">{card.title}</p>

        {card.status === "published" && free > 0 ? (
          <CountdownBadge expiresAt={card.expires_at} serverTime={card.server_time} />
        ) : null}

        {card.description ? (
          <p className="line-clamp-3 text-sm leading-relaxed text-ink-secondary">
            {card.description}
          </p>
        ) : null}

        <footer className="flex items-center gap-2 pt-1">
          <span
            aria-hidden="true"
            className="flex h-10 w-10 items-center justify-center rounded-full bg-surface-raised text-sm font-medium text-ink-secondary"
          >
            {(card.author.name ?? "?").slice(0, 1).toUpperCase()}
          </span>
          <span className="text-sm text-ink-secondary">
            {card.author.name ?? "Участник"}
            {card.author.age ? `, ${card.author.age}` : ""}
          </span>
        </footer>
      </Link>

      {/* §8.3: `stale` — «Обновляем…», кнопка активна. Скрывать действие на
          время тихой перезагрузки значило бы отбирать возможность, которая
          ещё работает. */}
      {stale ? (
        <p role="status" className="px-4 pb-2 text-sm text-ink-muted">
          Обновляем…
        </p>
      ) : null}

      {isOwn ? (
        <div className="border-t border-border-subtle px-4 py-3">
          <span className="text-sm font-medium text-ink-secondary">Ваша карточка</span>
        </div>
      ) : action ? (
        <div className="border-t border-border-subtle px-4 py-3">
          {/* Действие — `Button`, но как ссылка: переход на карточку, а не
              запрос. `asChild`-поведения у `Button` нет, поэтому ссылка
              стилизуется классами варианта, а не оборачивается в кнопку. */}
          <Link
            href={`/app/cards/${card.uuid}`}
            className="inline-flex min-h-11 items-center rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-ink transition-colors duration-fast ease-out hover:bg-accent-hover active:bg-accent-pressed focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
          >
            {action}
          </Link>
        </div>
      ) : null}
    </article>
  );
}
