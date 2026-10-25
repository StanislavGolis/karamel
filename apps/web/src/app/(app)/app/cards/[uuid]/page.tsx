/**
 * Детальный экран карточки события (стори 4.5, Task 5.2, 7.3; AC-02, AC-06).
 *
 * Серверный компонент: карточку (и её `404` для чужого сообщества или чужого
 * черновика — ADR-13) нужно знать до отрисовки. Гарды дублируют layout
 * кабинета намеренно (паттерн `app/profile/page.tsx`): прямой рендер страницы
 * без layout не должен открывать карточку без сессии или без согласия.
 *
 * `instant = false`: чтение cookie и запрос в API блокируют рендер — иначе
 * `cacheComponents` отдал бы разметку до проверки сессии.
 *
 * `404` от API показывается как «Карточка недоступна», а не как ошибка сети:
 * для участника чужая карточка и несуществующая неразличимы (ADR-13), и
 * подсказывать «возможно, она в другом городе» значит раскрывать её
 * существование.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { CardActions } from "@/components/cards/card-actions";
import { EventCard, type FeedCard } from "@/components/cards/event-card";
import { JoinActions } from "@/components/cards/join-actions";
import { ParticipantsList, loadCardParticipants } from "@/components/cards/participants-list";
import { RequestList, loadCardRequests } from "@/components/requests/request-list";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = {
  robots: { index: false, follow: false },
  title: "Встреча — Карамель",
};

export const instant = false;

const API_TIMEOUT_MS = 5_000;

/** Состояние загрузки: карточка, «нет доступа», «сервис недоступен». */
type CardState =
  | { kind: "ok"; card: FeedCard }
  | { kind: "not_found" }
  | { kind: "unavailable" };

async function loadCard(uuid: string, cookieHeader: string | null): Promise<CardState> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(`${apiUrl}/api/v1/cards/${encodeURIComponent(uuid)}`, {
      headers: cookieHeader ? { cookie: cookieHeader } : {},
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
    // ADR-13: `404` — это «нет доступа», а не сбой. Остальные не-`2xx`
    // (включая `403 consent_required`) — тоже «недоступно», но по другой
    // причине, поэтому состояния разделены.
    if (response.status === 404) return { kind: "not_found" };
    if (!response.ok) return { kind: "unavailable" };
    return { kind: "ok", card: (await response.json()) as FeedCard };
  } catch {
    return { kind: "unavailable" };
  }
}

export default async function CardPage({ params }: { params: Promise<{ uuid: string }> }) {
  const { uuid } = await params;

  const jar = await cookies();
  const cookieHeader = jar
    .getAll()
    .map(({ name, value }) => `${name}=${value}`)
    .join("; ");
  const result = await loadSession(cookieHeader || null);

  if (result.kind === "unavailable") {
    return (
      <main className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Сервис недоступен</h1>
        <p className="text-base leading-relaxed">
          Не удалось загрузить данные. Обновите страницу через минуту.
        </p>
      </main>
    );
  }
  if (result.kind === "blocked") redirect("/login?error=user_blocked");
  if (result.kind === "unauthenticated") redirect("/login");
  if (!hasConsent(result.user)) redirect("/consent");

  const state = await loadCard(uuid, cookieHeader || null);

  if (state.kind === "not_found") {
    return (
      <main className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Карточка недоступна</h1>
        <p className="text-base leading-relaxed">
          Возможно, встреча отменена или ссылка неверна. Вернитесь к ленте — там
          есть другие встречи.
        </p>
        <a
          href="/app"
          className="min-h-11 self-start rounded-md border border-[#D8CFC4] px-4 py-2 text-sm font-medium text-[#3D3529] hover:bg-[#F2EDE6]"
        >
          К ленте
        </a>
      </main>
    );
  }

  if (state.kind === "unavailable") {
    return (
      <main className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Сервис недоступен</h1>
        <p className="text-base leading-relaxed">
          Не удалось загрузить карточку. Проверьте связь и обновите страницу.
        </p>
      </main>
    );
  }

  const card = state.card;
  // AC-02 экрана: `draft` бывает виден только автору — API уже это обеспечил,
  // но подпись «черновик» нужна, чтобы автор не принял его за опубликованный.
  const isOwnDraft = card.status === "draft";

  // AC-11 (стори 4.6): действия автора видны ТОЛЬКО автору. Признак —
  // `author.uuid` из карточки против `uuid` сессии: `author_id` (внутренний
  // bigint) наружу не выходит (D-2), а сравнивать имена или возраст нельзя —
  // они не уникальны и могут отсутствовать.
  //
  // `isOwn` компонента карточки остаётся про черновик: опубликованную свою
  // карточку автор видит как обычную, а управление живёт отдельным блоком.
  const isAuthor = result.user.uuid === card.author.uuid;

  return (
    <main className="flex flex-col gap-4">
      <a href="/app" className="min-h-11 self-start text-sm font-medium text-[#7A3E12]">
        ← К ленте
      </a>
      <EventCard card={card} isOwn={isOwnDraft} />
      {/* AC-09 стори 5б.2 (FR104, X-5a.6-2): действия участника — ТОЛЬКО
          не-автору. Автор не присоединяется к своей компании, и кнопка
          «Отменить участие» ему не нужна (он распускает карточку через
          `CardActions`).

          `joinable` считается из статуса карточки, а не из `filled_count`:
          окно решения ограничивает приём ЗАЯВОК (5б.1), и «Присоединиться»
          на истёкшем окне вело бы в гарантированный `409 CARD_NOT_ACCEPTING`.
          Сервер проверяет то же условие заново — клиентская проверка только
          убирает заведомо провальное действие. */}
      {!isAuthor
        ? (
            <JoinActions
              uuid={card.uuid}
              participates={card.viewer_participates ?? false}
              joinable={card.status === "published"}
            />
          )
        : null}
      {isAuthor ? <CardActions uuid={card.uuid} status={card.status} /> : null}
      {/* Стори 6.3 (AC-01, AC-03; D-6.3-6): состав компании — ТОЛЬКО своим и
          ТОЛЬКО у собранной компании. До сборки состава нет (FR30: «чат только
          у собранной компании»), а постороннему API всё равно ответит `404`
          (ADR-13). Скрытие блока — удобство, не защита: защита на сервере.

          `viewer_participates` — готовый признак 5б.2 (D-5b.2-7); второй
          способ «я в составе» на клиенте потребовал бы списка участников,
          которого до этого запроса нет. */}
      {/* Стори 6.4 (Task 6.3): вход в переписку — только у собранной компании
          и только у её участников. `chat_uuid` заполняется в момент сборки
          (контракт 5б.2/6.2); до этого ссылки нет, потому что чата нет.
          `viewer_participates` — тот же признак, что у состава (6.3). */}
      {card.chat_uuid !== null && (isAuthor || card.viewer_participates) ? (
        <a
          href={`/app/chats/${card.chat_uuid}`}
          className="min-h-11 self-start rounded-md bg-[#7A3E12] px-4 py-2 text-sm font-medium text-white hover:bg-[#5E2F0D]"
        >
          Открыть чат компании
        </a>
      ) : null}
      {card.status === "full" && (isAuthor || card.viewer_participates) ? (
        <ParticipantsList
          state={await loadCardParticipants(uuid, cookieHeader || null)}
        />
      ) : null}
      {/* Стори 5а.4 (AC-01, AC-02): список заявившихся — ТОЛЬКО автору.
          Запрос всё равно уходит под cookie автора; API вернёт `404` любому
          другому (ADR-13), поэтому здесь достаточно проверки `isAuthor`, а
          скрытие блока не является защитой — защита на сервере. */}
      {isAuthor ? (
        <RequestList
          state={await loadCardRequests(uuid, cookieHeader || null)}
          // AC-10 стори 5б.2: кнопка «Выбрать» активна только при `published`.
          cardStatus={card.status}
        />
      ) : null}
    </main>
  );
}
