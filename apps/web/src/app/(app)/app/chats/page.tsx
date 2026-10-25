/**
 * Экран списка чатов (стори 6.6, Task 6.5; AC-01, AC-02).
 *
 * Серверная оболочка: гард сессии и согласия нужен ДО отрисовки, иначе гость
 * увидел бы скелетоны, которые никогда не наполнятся (`401`), и решил бы,
 * что «сломалось». Гарды дублируют layout кабинета намеренно (паттерн
 * `app/chats/[uuid]/page.tsx`): прямой рендер без layout не должен открывать
 * список без сессии.
 *
 * Данные грузятся НА КЛИЕНТЕ (`ChatList`): список листается курсором и
 * обновляется после прочтения, а серверный рендер первой страницы дублировал
 * бы ту же логику в двух местах. Тот же выбор, что у ленты (4.5) и переписки
 * (6.4).
 *
 * `instant = false`: чтение cookie и запрос сессии блокируют рендер — иначе
 * `cacheComponents` отдал бы разметку до проверки сессии.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { ChatList } from "@/components/chat/chat-list";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = {
  robots: { index: false, follow: false },
  title: "Чаты — Карамель",
};

export const instant = false;

export default async function ChatsPage() {
  const jar = await cookies();
  const cookieHeader = jar
    .getAll()
    .map(({ name, value }) => `${name}=${value}`)
    .join("; ");
  const result = await loadSession(cookieHeader || null);

  // Недоступность API — не повод выкидывать из аккаунта (паттерн 1.6).
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

  return (
    <main className="flex flex-col gap-4">
      <a href="/app" className="min-h-11 self-start text-sm font-medium text-[#7A3E12]">
        ← К встречам
      </a>
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Чаты</h1>
        <p className="text-sm text-[#5A5249]">
          Здесь переписка собранных компаний. Непрочитанные отмечены числом.
        </p>
      </header>
      <ChatList />
    </main>
  );
}
