/**
 * Экран «Уведомления» (стори 9.3; AC-01, AC-02; Task 5.1).
 *
 * Серверный компонент: сессию и согласие нужно знать до отрисовки. Гарды
 * дублируют layout кабинета намеренно (паттерн `profile/page.tsx`): прямой
 * рендер страницы без layout не должен открывать настройки без сессии или
 * без согласия на ПД.
 *
 * **Три состояния, не два** (урок CR 2.3, патч 11). Любая ошибка API
 * (`5xx`, таймаут, нечитаемое тело) — это `unavailable`, а НЕ пустой список:
 * схлопнув сбой в `[]`, экран показал бы «все уведомления отключены»,
 * и пользователь, нажимая переключатели, отправлял бы запросы на сервер,
 * который уже не отвечает.
 *
 * `consent_required` — не «нет данных», а «нужен шаг согласия»: редирект на
 * `/consent`, а не пустой экран.
 *
 * `instant = false`: чтение cookie и запрос в API блокируют рендер. Иначе
 * `cacheComponents` отдал бы разметку до проверки сессии.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import {
  NotificationSettings,
  type NotificationSettingView,
} from "@/components/notifications/notification-settings";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = {
  robots: { index: false, follow: false },
  title: "Уведомления — Карамель",
};

export const instant = false;

const API_TIMEOUT_MS = 5_000;

/**
 * Результат чтения настроек.
 *
 * `ok` — список пришёл; `unavailable` — сбой сервиса; `redirect` — нужен
 * повторный вход или согласие (различаются вызывающим).
 */
type SettingsState =
  | { kind: "ok"; items: NotificationSettingView[] }
  | { kind: "unavailable" };

/**
 * Читает настройки через API.
 *
 * `403 consent_required` обрабатывается вызывающим кодом до этого вызова
 * (через `hasConsent`), поэтому здесь он попадает в `unavailable`: повторная
 * проверка согласия по коду ответа создала бы второй источник правды для
 * одного условия.
 */
async function loadSettings(cookieHeader: string): Promise<SettingsState> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(`${apiUrl}/api/v1/notifications/settings`, {
      headers: { cookie: cookieHeader },
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });

    if (!response.ok) return { kind: "unavailable" };

    const payload = (await response.json()) as { items?: unknown };
    if (!Array.isArray(payload.items)) return { kind: "unavailable" };

    // NFR60: неизвестный тип ИГНОРИРУЕТСЯ. Фильтр по форме (`type` строкой,
    // `enabled`/`required` булевы) не даёт серверному расширению сломать
    // отрисовку: элемент без нужных полей просто не появится.
    const items = payload.items.filter((item): item is NotificationSettingView => {
      if (typeof item !== "object" || item === null) return false;
      const candidate = item as Record<string, unknown>;
      return (
        typeof candidate.type === "string" &&
        typeof candidate.enabled === "boolean" &&
        typeof candidate.required === "boolean"
      );
    });

    // Пустой перечень — тоже сбой: сервер обязан отдавать 11 типов, и пустой
    // список означал бы «показывать нечего», а не «всё отключено».
    if (items.length === 0) return { kind: "unavailable" };

    return { kind: "ok", items };
  } catch {
    return { kind: "unavailable" };
  }
}

export default async function NotificationSettingsPage() {
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

  const settings = await loadSettings(cookieHeader);

  if (settings.kind === "unavailable") {
    return (
      <main className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Настройки недоступны</h1>
        <p className="text-base leading-relaxed">
          Не удалось загрузить настройки уведомлений. Обновите страницу через
          минуту — настройки не потеряны.
        </p>
      </main>
    );
  }

  return (
    <main className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Уведомления</h1>
        <p className="text-base leading-relaxed text-ink-secondary">
          Выберите, о чём присылать. Решения по жалобам и санкции приходят
          всегда — их нельзя отключить.
        </p>
      </div>
      <NotificationSettings initial={settings.items} />
    </main>
  );
}
