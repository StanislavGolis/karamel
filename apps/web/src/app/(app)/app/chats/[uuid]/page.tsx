/**
 * Экран чата компании (стори 6.4, Task 6.2; AC-01, AC-02).
 *
 * Серверная оболочка: гард сессии и согласия нужен ДО отрисовки, иначе
 * гость увидел бы форму отправки, которая вернёт `401`. Гарды дублируют
 * layout кабинета намеренно (паттерн `app/profile/page.tsx`): прямой рендер
 * страницы без layout не должен открывать чат без сессии.
 *
 * Данные переписки грузятся НА КЛИЕНТЕ (`ChatScreen`): история листается
 * курсором и обновляется после отправки, а серверный рендер первой страницы
 * дублировал бы ту же логику в двух местах. Тот же выбор, что у ленты
 * (4.5) и списка заявившихся (5а.4).
 *
 * `instant = false`: чтение cookie и запрос сессии блокируют рендер — иначе
 * `cacheComponents` отдал бы разметку до проверки сессии.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { ChatScreen } from "@/components/chat/chat-screen";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = {
  robots: { index: false, follow: false },
  title: "Чат компании — Карамель",
};

export const instant = false;

export default async function ChatPage({ params }: { params: Promise<{ uuid: string }> }) {
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

  return (
    <main className="flex flex-col gap-4">
      <a href="/app" className="min-h-11 self-start text-sm font-medium text-[#7A3E12]">
        ← К ленте
      </a>
      {/* `uuid` зрителя — для выравнивания своих сообщений. Внутренний `id`
          наружу не выходит (D-2), поэтому сравнение идёт по публичному. */}
      <ChatScreen chatUuid={uuid} viewerUuid={result.user.uuid} />
    </main>
  );
}
