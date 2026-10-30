/**
 * Анкета пользователя для модератора (стори 12.10; AC-1…AC-8, `W-11.4-a`).
 *
 * **Что здесь решается.** AC-6 стори 11.4 («видно, кто принял 1.1, а кто 1.2»)
 * был закрыт только в API: модератор не мог посмотреть анкету без `curl`.
 * Экран закрывает `W-11.4-a` — единственную открытую ВЫСОКУЮ находку 11.4.
 *
 * **Серверный компонент, без клиентского состояния** (приём `metrics/page.tsx`,
 * D-10.5-10): данные приходят из `GET /api/v1/moderation/users/{uuid}` при
 * рендере. Нет `fetch` в браузере, нет `useEffect`, нет второго пути получения
 * тех же данных.
 *
 * **Гарда НЕ дублируется.** `(mod)/mod/layout.tsx` уже проверил сессию и роль
 * (`redirect("/login")` без сессии, `redirect("/")` для не-модератора).
 * Вторая проверка здесь завела бы третье представление одного правила — ровно
 * тот дефект, из-за которого в 1.6 аккаунт попадал в цикл редиректов.
 *
 * **Почему `403` всё равно обрабатывается** (D-12.10-6): роль читается из БД
 * на каждом запросе, и её могут понизить между рендером layout и запросом
 * страницы. Тогда layout уже пропустил, а API отказал.
 *
 * **Почему `instant = false`.** Возраст и `photo_url` считаются в момент
 * запроса: без серверного рендера страница показывала бы возраст не того дня.
 *
 * **`birth_date` не отображается и не может** (AC-4, FR124, NFR66): поля нет
 * в контракте API (замок 12.3), в компоненте нет типа под него — то, чего
 * нет, нельзя случайно отрисовать.
 *
 * **Возраст НЕ пересчитывается** (D-12.10-4): второй расчёт — второй источник
 * истины, разойдётся с сервером на границе суток (`W-12.2-b`).
 */

import { cookies } from "next/headers";

import { PersonMini } from "@/components/ui/person-mini";

export const metadata = { robots: { index: false, follow: false } };

/** Состояние нужно до отрисовки: без него неизвестно, что показывать. */
export const instant = false;

const API_TIMEOUT_MS = 5_000;

/** Профиль в объёме allowlist карточки (8.5 + 11.4 + 12.10). */
export type UserCardProfile = {
  uuid: string;
  name: string | null;
  city: string | null;
  age: number | null;
  gender: "female" | "male" | null;
  status: "active" | "warned" | "banned";
  consent_version: string | null;
  photo_url: string | null;
  photo_expires_in: number | null;
};

/** Сводка истории: три числа, которые видит модератор (AC-6). */
export type UserCardSummary = {
  reports_against_total: number;
  sanctions_total: number;
  attendance_total: number;
};

export type UserCardView = {
  profile: UserCardProfile;
  summary: UserCardSummary;
};

/**
 * Состояние экрана: загружено, нет прав, нет пользователя, сбой.
 *
 * `forbidden` и `notFound` — разные тексты и разные действия читателя
 * (P-26, приём `metrics/page.tsx`): «нет прав» значит проверить роль,
 * «не найден» — проверить адрес. Слить их в один текст значит отправить
 * модератора перезагружать страницу вместо проверки.
 */
export type UserCardState =
  | { kind: "ok"; data: UserCardView }
  | { kind: "forbidden" }
  | { kind: "unauthorized" }
  | { kind: "notFound" }
  | { kind: "unavailable" };

/** Обязательные блоки ответа: серверное расширение не должно ломать отрисовку. */
type CardPayload = {
  profile?: Partial<UserCardProfile>;
  reports?: { against_total?: unknown };
  sanctions?: { total?: unknown };
  attendance?: { total?: unknown };
};

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Читает карточку через API и раскладывает ответ в состояние экрана.
 *
 * Отдельная функция, а не код внутри компонента (`W-12.7-l`): состояния
 * `200`/`403`/`404`/`5xx`/сеть проверяются без рендера, и тест не зависит от
 * разметки.
 */
export async function loadUserCard(cookieHeader: string, uuid: string): Promise<UserCardState> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(`${apiUrl}/api/v1/moderation/users/${uuid}`, {
      headers: { cookie: cookieHeader },
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });

    // CR M-6 (AC-7): `401` — сессия истекла, действие «войти заново», а не
    // «попробовать снова». Схлопывание в `unavailable` отправляло модератора
    // перезагружать страницу, которая и так не откроется без входа (P-26).
    if (response.status === 401) return { kind: "unauthorized" };
    // 403 — отказ по роли, а не сбой сервиса: разные действия читателя.
    if (response.status === 403) return { kind: "forbidden" };
    if (response.status === 404) return { kind: "notFound" };
    // `422`/`429`/`5xx` остаются «сбоем»: совет «попробовать снова» верен для
    // 429 (лимит) и 5xx (временный отказ), а `422` на этом маршруте означал бы
    // битый `uuid` — и сообщение «проверьте адрес» точнее перезагрузки.
    if (response.status === 422) return { kind: "notFound" };
    if (!response.ok) return { kind: "unavailable" };

    const payload = (await response.json()) as CardPayload;
    const profile = payload.profile;
    // Ответ без профиля — сбой контракта, а не «пустая анкета»: карточка
    // агрегат четырёх блоков, и профиль в ней обязателен.
    if (typeof profile !== "object" || profile === null) return { kind: "unavailable" };

    const againstTotal = asNumber(payload.reports?.against_total);
    const sanctionsTotal = asNumber(payload.sanctions?.total);
    const attendanceTotal = asNumber(payload.attendance?.total);
    if (againstTotal === null || sanctionsTotal === null || attendanceTotal === null) {
      return { kind: "unavailable" };
    }

    return {
      kind: "ok",
      data: {
        profile: {
          uuid: typeof profile.uuid === "string" ? profile.uuid : uuid,
          name: typeof profile.name === "string" ? profile.name : null,
          city: typeof profile.city === "string" ? profile.city : null,
          age: asNumber(profile.age),
          gender: profile.gender === "female" || profile.gender === "male" ? profile.gender : null,
          status:
            profile.status === "warned" || profile.status === "banned"
              ? profile.status
              : "active",
          consent_version:
            typeof profile.consent_version === "string" ? profile.consent_version : null,
          photo_url: typeof profile.photo_url === "string" ? profile.photo_url : null,
          photo_expires_in: asNumber(profile.photo_expires_in),
        },
        summary: {
          reports_against_total: againstTotal,
          sanctions_total: sanctionsTotal,
          attendance_total: attendanceTotal,
        },
      },
    };
  } catch {
    return { kind: "unavailable" };
  }
}

/** «Согласие: версия 1.2» / «Согласие: версия неизвестна» (AC-1, NFR36). */
function consentLabel(version: string | null): string {
  return version === null ? "Согласие: версия неизвестна" : `Согласие: версия ${version}`;
}

export default async function ModerationUserPage({
  params,
}: {
  params: Promise<{ uuid: string }>;
}) {
  const { uuid } = await params;
  const jar = await cookies();
  const cookieHeader = jar
    .getAll()
    .map(({ name, value }) => `${name}=${value}`)
    .join("; ");

  const state = await loadUserCard(cookieHeader, uuid);

  if (state.kind === "unauthorized") {
    return (
      <main className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Сессия истекла</h1>
        <p className="text-base leading-relaxed">
          Войдите заново — после входа откройте анкету по прежней ссылке.
        </p>
      </main>
    );
  }

  if (state.kind === "forbidden") {
    return (
      <main className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Недостаточно прав</h1>
        <p className="text-base leading-relaxed">
          Анкета доступна только модератору. Если роль недавно менялась — войдите заново.
        </p>
      </main>
    );
  }

  if (state.kind === "notFound") {
    return (
      <main className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Пользователь не найден</h1>
        <p className="text-base leading-relaxed">
          Возможно, аккаунт удалён или адрес скопирован с ошибкой.
        </p>
      </main>
    );
  }

  if (state.kind === "unavailable") {
    return (
      <main className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Анкета пользователя</h1>
        <p className="text-base leading-relaxed">
          Не удалось загрузить. Попробовать снова —{" "}
          <a
            href=""
            className="inline-flex min-h-11 items-center text-ink-secondary underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
          >
            перезагрузить страницу
          </a>
          .
        </p>
      </main>
    );
  }

  const { profile, summary } = state.data;

  return (
    <main className="flex flex-col gap-4">
      <a
        href="/mod"
        className="inline-flex min-h-11 items-center text-sm text-ink-secondary underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
      >
        ← Пользователь
      </a>

      {/* AC-1: фото (или инициалы), имя, вычисленный возраст, город. */}
      <PersonMini name={profile.name} age={profile.age} photoUrl={profile.photo_url} />

      {profile.city ? <p className="text-sm text-ink-secondary">Город: {profile.city}</p> : null}

      {/* AC-1: версия согласия. `null` — честное «неизвестна», а не пустая строка. */}
      <p className="text-sm text-ink-secondary">{consentLabel(profile.consent_version)}</p>

      <hr className="border-border" />

      {/* AC-6: три числа сводки в одной строке. Тикающих счётчиков нет (З-9). */}
      <p className="text-base">
        Жалобы: {summary.reports_against_total} · Санкции: {summary.sanctions_total} · Явки:{" "}
        {summary.attendance_total}
      </p>

      {/* FR60: факт аудита виден модератору — он знает, что его действие записано. */}
      <p className="text-sm text-ink-muted">Доступ к анкете залогирован (FR60)</p>
    </main>
  );
}
