/**
 * Анкета автора карточки (UX поверхность 7 §780-802; стори 12.7, AC-6).
 *
 * **Серверный компонент**: анкета нужна до отрисовки, а `404` (архив, чужой
 * город, черновик чужого автора) решает API (ADR-13), не клиент. Клиентская
 * загрузка показала бы сначала пустоту, потом данные — на экране, где человек
 * решает «идти ли на встречу», мигание читается как нестабильность.
 *
 * **Потребитель контракта** (P-26): без этого компонента маршрут
 * `GET /cards/{uuid}/author` был бы документом без читателя, и стори
 * считалась бы закрытой по факту наличия эндпоинта.
 *
 * **Три состояния одной функцией** (UX §873: любой блок обязан отрисовать
 * понятное состояние, а не пустой `<div>`):
 * - данные есть → фото (или инициалы), имя, вычисленный возраст, город, описание;
 * - `404` → `archived`: текст UX §919 «Эта встреча завершена. Анкета автора
 *   больше не показывается.» и «Открыть ленту». Это НЕ ошибка сети: архивная
 *   карточка открывается штатно (`GET /cards/{uuid}` → `200`, FR73), и
 *   пользователь должен отличать «анкета закрыта» от «сервис недоступен»;
 * - сбой сети → «не удалось загрузить» с просьбой обновить.
 *
 * **Чего здесь нет и не может быть** (UX §780-802, NFR39): даты рождения,
 * рейтинга, истории явок, числа встреч. `birth_date` не приходит из API
 * вовсе (замок 12.3), а рейтинга нет в контракте — то, чего нет, нельзя
 * случайно отрисовать.
 */

import { EmptyState } from "@/components/ui/empty-state";
import { PersonMini } from "@/components/ui/person-mini";

/** Ответ `GET /api/v1/cards/{uuid}/author`. */
export type CardAuthorProfile = {
  uuid: string;
  name: string | null;
  age: number | null;
  city: string | null;
  bio: string | null;
  photo_url: string | null;
  photo_expires_in: number | null;
};

/** Состояние загрузки анкеты: данные, «закрыта», «сервис недоступен». */
export type AuthorProfileState =
  | { kind: "ok"; data: CardAuthorProfile }
  | { kind: "archived" }
  | { kind: "unavailable" };

const API_TIMEOUT_MS = 5_000;

/**
 * Загружает анкету автора через карточку.
 *
 * `404` отделён от прочих ошибок намеренно: для архивной карточки, чужого
 * города и черновика чужого автора ответ ОДИН (ADR-13), и предлагать
 * «повторите» на `404` значило бы предлагать действие, которое не поможет.
 *
 * Запроса анкеты по голому `uuid` пользователя здесь нет и быть не может
 * (AC-6, ADR-22): единственный адрес — карточка.
 */
export async function loadCardAuthorProfile(
  cardUuid: string,
  cookieHeader: string | null,
): Promise<AuthorProfileState> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(
      `${apiUrl}/api/v1/cards/${encodeURIComponent(cardUuid)}/author`,
      {
        headers: cookieHeader ? { cookie: cookieHeader } : {},
        cache: "no-store",
        signal: AbortSignal.timeout(API_TIMEOUT_MS),
      },
    );
    if (response.status === 404) return { kind: "archived" };
    if (!response.ok) return { kind: "unavailable" };
    return { kind: "ok", data: (await response.json()) as CardAuthorProfile };
  } catch {
    return { kind: "unavailable" };
  }
}

type AuthorProfileProps = {
  state: AuthorProfileState;
};

export function AuthorProfile({ state }: AuthorProfileProps) {
  if (state.kind === "unavailable") {
    return (
      <EmptyState
        title="Не удалось загрузить анкету"
        reason="Проверьте связь и обновите страницу."
        actionKind="quiet"
      />
    );
  }

  // AC-6, UX §919: архив — понятное состояние, а не пустота. Текст дословно
  // из UX: пользователь должен узнать, ПОЧЕМУ анкеты нет, и что делать дальше.
  if (state.kind === "archived") {
    return (
      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold tracking-tight">Автор встречи</h2>
        <p className="text-base leading-relaxed">
          Эта встреча завершена. Анкета автора больше не показывается.
        </p>
        <a
          href="/app"
          className="min-h-11 self-start rounded-md border border-border px-4 py-2 text-sm font-medium text-ink hover:bg-surface-hover"
        >
          Открыть ленту
        </a>
      </section>
    );
  }

  const profile = state.data;

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold tracking-tight">Автор встречи</h2>
      <PersonMini name={profile.name} age={profile.age} photoUrl={profile.photo_url} />
      {/* AC-6, NULL §4 project-context: `null`-поля НЕ рисуют пустых строк.
          «Город:» с пустым значением читалось бы как сбой загрузки. */}
      {profile.city ? (
        <p className="text-sm text-ink-secondary">Город: {profile.city}</p>
      ) : null}
      {profile.bio ? (
        <p className="text-base leading-relaxed">{profile.bio}</p>
      ) : null}
    </section>
  );
}
