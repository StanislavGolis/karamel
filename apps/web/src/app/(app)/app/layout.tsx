/**
 * Гарда защищённых экранов зоны `(app)/app` (Task 9.2–9.4; AC-07; CR 1.6: патчи 1, 13, 14, 15).
 *
 * Серверный layout, а не middleware (D-10): в Next 16.4.2 живут обе конвенции
 * (`MIDDLEWARE_FILENAME` и `PROXY_FILENAME`), сборка переименовывает файл,
 * и опора на его имя — движущаяся мишень. Layout закрывает поддерево целиком
 * и не конфликтует с `cacheComponents: true`.
 *
 * CR 1.5, патч 5: проверяется **валидность** сессии через `/api/v1/auth/me`,
 * а не наличие cookie. Раньше `document.cookie='karamel_access=x'` из консоли
 * открывал весь кабинет.
 *
 * CR 1.6, патч 1: предикат согласия — `hasConsent` из `lib/auth-server`,
 * один на все три гарды. Раньше здесь проверялся только `age_confirmed`,
 * и учётка с `age_confirmed=true` и `consent_given_at=null` получала полный
 * доступ без согласия на обработку ПД (нарушение 152-ФЗ). Там же был источник
 * цикла `/app` ↔ `/consent`: три места использовали три разных условия.
 *
 * CR 1.6, патчи 13/14: четыре исхода проверки сессии различаются. Забаненный
 * получает объяснение, а не форму входа; недоступность API не стирает живую
 * сессию.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { LogoutButton } from "@/components/ui/logout-button";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = { robots: { index: false, follow: false } };

/**
 * Гарда обязана решать до отрисовки — это блокирующий маршрут (AC-07).
 *
 * `cacheComponents: true` в Next 16 по умолчанию требует, чтобы чтение
 * `cookies()` было обёрнуто в `<Suspense>` и страница пререндерилась
 * потоком. Для гарды это неприемлемо: пререндер отдал бы разметку кабинета
 * до проверки cookie, то есть ровно то, что AC-07 запрещает.
 * `instant = false` разрешает блокировку — сборка перестаёт требовать Suspense.
 */
export const instant = false;

export default async function AppLayout({ children }: Readonly<{ children: ReactNode }>) {
  // Next 16: `cookies()` асинхронный.
  const jar = await cookies();
  // Заголовок `Cookie` собирается вручную: `cookies()` отдаёт значения по
  // одному, а `/auth/me` ждёт обычный заголовок. HttpOnly-cookie доступна
  // здесь целиком — код исполняется на сервере.
  const cookieHeader = jar
    .getAll()
    .map(({ name, value }) => `${name}=${value}`)
    .join("; ");
  const result = await loadSession(cookieHeader || null);

  // Патч 14: API недоступен — сессия не опровергнута. Показываем честный экран
  // вместо редиректа на вход: иначе каждое подвисание API выкидывало бы
  // пользователя из аккаунта без обратной дороги.
  if (result.kind === "unavailable") {
    return (
      <main className="mx-auto max-w-[640px] px-4 py-16">
        <h1 className="text-2xl font-semibold tracking-tight">Сервис недоступен</h1>
        <p className="mt-3 text-base leading-relaxed">
          Не удалось проверить сессию. Это временная неисправность на нашей стороне —
          обновите страницу через минуту. Выходить из аккаунта не нужно.
        </p>
      </main>
    );
  }

  // Патч 13: бан объясняется на странице входа, а не маскируется под «нет сессии».
  if (result.kind === "blocked") redirect("/login?error=user_blocked");

  // AC-07: сессия не подтверждена — редирект до отрисовки содержимого.
  if (result.kind === "unauthenticated") redirect("/login");

  // Патч 1 (AC-05): единый предикат вместо `age_confirmed`.
  // Проверяется после сессии: без входа `/consent` отправит на `/login`,
  // и пользователь получил бы цепочку из двух редиректов вместо одного.
  if (!hasConsent(result.user)) redirect("/consent");

  return (
    <div className="mx-auto max-w-[640px] px-4 py-6">
      <header className="flex items-center justify-between gap-4 pb-6">
        <span className="text-sm text-[#5A5249]">Карамель</span>
        <LogoutButton />
      </header>
      {children}
    </div>
  );
}
