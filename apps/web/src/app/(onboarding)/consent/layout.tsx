/**
 * Layout экрана подтверждения (Task 8.1, 8.2; D-2; CR 1.6: патчи 1, 13, 14, 15).
 *
 * Отдельная route group `(onboarding)`, а не `(app)/app`: гарда кабинета
 * редиректит на `/consent` при отсутствии согласия, и страница внутри
 * `(app)/app` редиректила бы на саму себя — бесконечный цикл.
 *
 * Патч 1: условие «согласие уже дано» — общий `hasConsent`, а не
 * `age_confirmed`. Раньше здесь проверялся только `age_confirmed`, поэтому
 * учётка с подтверждённым возрастом и `gender=null` уходила на `/app`,
 * откуда её отправляли обратно на `/consent` → `ERR_TOO_MANY_REDIRECTS`.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { LogoutButton } from "@/components/ui/logout-button";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = { robots: { index: false, follow: false } };

/** Тот же приём, что в гарде кабинета: проверка до отрисовки (AC-05). */
export const instant = false;

export default async function ConsentLayout({ children }: Readonly<{ children: ReactNode }>) {
  const jar = await cookies();
  const cookieHeader = jar
    .getAll()
    .map(({ name, value }) => `${name}=${value}`)
    .join("; ");
  const result = await loadSession(cookieHeader || null);

  // Патч 14: недоступность API — не повод выкидывать из аккаунта.
  if (result.kind === "unavailable") {
    return (
      <div className="mx-auto max-w-[640px] px-4 py-16">
        <h1 className="text-2xl font-semibold tracking-tight">Сервис недоступен</h1>
        <p className="mt-3 text-base leading-relaxed">
          Не удалось проверить сессию. Обновите страницу через минуту — выходить
          из аккаунта не нужно.
        </p>
      </div>
    );
  }

  // Патч 13: забаненный получает объяснение на странице входа.
  if (result.kind === "blocked") redirect("/login?error=user_blocked");

  // AC-07: без сессии экран подтверждения бессмысленен — согласие привязано
  // к учётке (D-1), а не к браузеру.
  if (result.kind === "unauthenticated") redirect("/login");

  // AC-05, обратный случай (патч 1): согласие дано целиком — форма не нужна.
  if (hasConsent(result.user)) redirect("/app");

  return (
    <div className="mx-auto max-w-[640px] px-4 py-6">
      <header className="flex items-center justify-between gap-4 pb-6">
        <span className="text-sm text-[#5A5249]">Карамель</span>
        {/* Task 8.7 (AC-07): выход доступен до подтверждения. */}
        <LogoutButton />
      </header>
      {children}
    </div>
  );
}
