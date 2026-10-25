/**
 * Гарда кабинета модератора (Решение 3 код-ревью CR 1.5).
 *
 * `/mod` был добавлен в диффе без защиты: любой посетитель видел кабинет,
 * предназначенный модераторам. Перед эпиком 6 (модерация) зона закрывается
 * двумя проверками: сессия подтверждена `/auth/me` и `role === "moderator"`.
 *
 * Роль берётся из БД (её отдаёт `/auth/me`), а не из JWT: в payload роль
 * тоже есть, но она отражает момент выдачи токена, а повышение/понижение
 * прав должно действовать немедленно — так же, как бан в `deps.py`.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { getSessionUser } from "@/lib/auth-server";

export const metadata = { robots: { index: false, follow: false } };

/** Гарда блокирующая: разметка не должна отдаваться до проверки роли. */
export const instant = false;

export default async function ModLayout({ children }: Readonly<{ children: ReactNode }>) {
  const jar = await cookies();
  const cookieHeader = jar
    .getAll()
    .map(({ name, value }) => `${name}=${value}`)
    .join("; ");
  const user = await getSessionUser(cookieHeader || null);

  // Нет сессии — на вход. Отдельный редирект, а не общий: пользователь
  // без сессии не должен получить «доступ запрещён» вместо возможности войти.
  if (user === null) redirect("/login");

  // CR 1.5: обычный пользователь не узнаёт о существовании зоны модерации —
  // его уводит на главную. `redirect` вместо 403: 403 подтверждал бы,
  // что маршрут существует, и приглашал бы к перебору.
  if (user.role !== "moderator") redirect("/");

  return (
    <div className="mx-auto max-w-[320px] px-2 py-6">
      <header className="flex items-center justify-between gap-4 pb-4">
        <span className="text-sm text-ink-secondary">Карамель · модерация</span>
      </header>
      {children}
    </div>
  );
}
