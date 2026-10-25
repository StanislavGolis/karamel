/**
 * Экран «Поддержка» (стори 9.5; AC-01…AC-03; Task 6.1).
 *
 * Серверный компонент: сессию нужно знать до отрисовки. Гарды дублируют layout
 * кабинета намеренно (паттерн `profile/notifications/page.tsx`): прямой рендер
 * страницы без layout не должен открывать поддержку без сессии.
 *
 * **Три состояния, не два** (урок CR 2.3, патч 11). Любая ошибка API (`5xx`,
 * таймаут, нечитаемое тело) — это `unavailable`, а НЕ пустой список: схлопнув
 * сбой в `[]`, экран показал бы «Обращений пока нет» человеку, чьё обращение
 * на самом деле не загрузилось. Это ровно тот класс дефекта, из-за которого
 * в 9.3 настройки не показывались при недоступном сервисе.
 *
 * `blocked` и `unauthenticated` — РАЗНЫЕ редиректы: заблокированному нет
 * смысла идти на логин (D-9.5-3, У-5), а публичный путь «написать» остаётся
 * `/privacy#contact`.
 *
 * `instant = false`: чтение cookie и запрос в API блокируют рендер. Иначе
 * `cacheComponents` отдал бы разметку до проверки сессии.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import type { SupportTicketView } from "@/components/support/support-list";
import { SupportTicketsBoard } from "@/components/support/support-tickets-board";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = {
  robots: { index: false, follow: false },
  title: "Поддержка — Карамель",
};

export const instant = false;

const API_TIMEOUT_MS = 5_000;

/**
 * Результат чтения обращений.
 *
 * `ok` — список пришёл (возможно пустой: это НОРМАЛЬНОЕ состояние);
 * `unavailable` — сбой сервиса.
 */
type TicketsState =
  | { kind: "ok"; items: SupportTicketView[]; total: number; cursor: string | null }
  | { kind: "unavailable" };

/**
 * Проверяет форму элемента списка (NFR60: неизвестное ИГНОРИРУЕТСЯ).
 *
 * Фильтр по форме, а не `as`: серверное расширение контракта не должно
 * ломать отрисовку. Элемент без `uuid` или `status` не отрисуется вовсе —
 * у него нечего показать.
 */
function isTicketView(value: unknown): value is SupportTicketView {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.uuid === "string" &&
    typeof candidate.body === "string" &&
    typeof candidate.status === "string" &&
    typeof candidate.created_at === "string" &&
    (candidate.answer === null || typeof candidate.answer === "string") &&
    (candidate.answered_at === null || typeof candidate.answered_at === "string")
  );
}

/**
 * Читает обращения через API.
 *
 * `403 consent_required` сюда не доходит: согласие проверяется `hasConsent`
 * до вызова, и повторная проверка по коду создала бы второй источник правды
 * для одного условия (та же логика, что в `notifications/page.tsx`).
 */
async function loadTickets(cookieHeader: string): Promise<TicketsState> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(`${apiUrl}/api/v1/support/tickets`, {
      headers: { cookie: cookieHeader },
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });

    if (!response.ok) return { kind: "unavailable" };

    const payload = (await response.json()) as {
      items?: unknown;
      total?: unknown;
      next_cursor?: unknown;
    };
    if (!Array.isArray(payload.items)) return { kind: "unavailable" };
    // CR 9.5 MEDIUM-3: `total` и `next_cursor` доезжают до экрана — иначе
    // список обрезан первой страницей, а 21-е обращение недостижимо.
    if (typeof payload.total !== "number") return { kind: "unavailable" };
    if (payload.next_cursor !== null && typeof payload.next_cursor !== "string") {
      return { kind: "unavailable" };
    }

    // Пустой массив — ВАЛИДНЫЙ ответ (обращений нет), поэтому здесь нет
    // проверки `items.length === 0 → unavailable` как в настройках: там
    // пустой перечень был признаком сбоя (сервер обязан отдавать 11 типов),
    // а тут «нет обращений» — нормальное состояние нового пользователя.
    return {
      kind: "ok",
      items: payload.items.filter(isTicketView),
      total: payload.total,
      cursor: payload.next_cursor,
    };
  } catch {
    return { kind: "unavailable" };
  }
}

export default async function SupportPage() {
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
          Не удалось проверить сессию. Обновите страницу через минуту — выходить
          из аккаунта не нужно.
        </p>
      </main>
    );
  }

  if (result.kind === "blocked") redirect("/login?error=user_blocked");
  if (result.kind === "unauthenticated") redirect("/login");

  // AC-07 стори 2.1: без согласия на ПД экран недоступен (та же гарда, что в layout).
  if (!hasConsent(result.user)) redirect("/consent");

  const tickets = await loadTickets(cookieHeader);

  if (tickets.kind === "unavailable") {
    return (
      <main className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Поддержка недоступна</h1>
        <p className="text-base leading-relaxed">
          Не удалось загрузить ваши обращения. Обновите страницу через минуту —
          отправленные обращения не потеряны.
        </p>
      </main>
    );
  }

  return (
    <main className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Поддержка</h1>
        <p className="text-base leading-relaxed text-[#5A5249]">
          Опишите проблему — ответ придёт в уведомления. Если войти не
          получается, напишите на почту со страницы «Конфиденциальность».
        </p>
      </div>
      {/* D-9.5-5: форма и список живут в одном клиентском компоненте, чтобы
          `201` вставлял созданную строку в уже отрисованный список без второго
          запроса — иначе экран мигал бы перезагрузкой. */}
      <SupportTicketsBoard
        initial={tickets.items}
        initialTotal={tickets.total}
        initialCursor={tickets.cursor}
      />
    </main>
  );
}
