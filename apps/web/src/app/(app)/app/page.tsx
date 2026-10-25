/**
 * Кабинет: заглушка ожидания заявок (Task 9; AC-01, AC-02; CR 1.6: Решение 3, патчи 13, 14).
 *
 * Ни заявки, ни анкеты, ни очереди здесь нет — это эпики 2 и 3. Экран честно
 * говорит, что доступно сейчас, и не обещает сроков (project-context §11:
 * без ложных ожиданий и таймеров).
 *
 * Решение 3 код-ревью 1.6: экран ожидания включается **флагом от API**
 * (`awaiting_applications`), а не сравнением `gender === "male"` на клиенте.
 * Раньше «мужчина вне выборки» существовало только в UI: правило продукта
 * не было выражено в контракте, и любой другой клиент показывал бы мужчине
 * тот же экран, что и женщине.
 *
 * Предикат согласия — `hasConsent`, тот же, что в layout (патч 1): разное
 * условие здесь давало цикл `/app` ↔ `/consent`.
 */

import Link from "next/link";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { CardFeed } from "@/components/cards/card-feed";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = { robots: { index: false, follow: false } };

/** Состояние нужно до отрисовки: без него неизвестно, какую заглушку показывать. */
export const instant = false;

export default async function AppHomePage() {
  const jar = await cookies();
  const cookieHeader = jar
    .getAll()
    .map(({ name, value }) => `${name}=${value}`)
    .join("; ");
  const result = await loadSession(cookieHeader || null);

  // Патч 14: недоступность API — не повод выкидывать из аккаунта.
  if (result.kind === "unavailable") {
    return (
      <main className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Сервис недоступен</h1>
        <p className="text-base leading-relaxed">
          Не удалось загрузить данные аккаунта. Обновите страницу через минуту —
          выходить из аккаунта не нужно.
        </p>
      </main>
    );
  }

  // Патч 13: забаненный получает объяснение на странице входа.
  if (result.kind === "blocked") redirect("/login?error=user_blocked");

  if (result.kind === "unauthenticated") redirect("/login");

  // Патч 1: единый предикат. Layout уже проверил то же самое; повтор здесь —
  // страховка от прямого рендера страницы без layout (тесты, будущий рефакторинг).
  if (!hasConsent(result.user)) redirect("/consent");

  // Решение 3 (AC-02): мужчина видит честную заглушку, заявка не создаётся (эпик 3).
  if (result.user.awaiting_applications) {
    return (
      <main className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Заявка на участие</h1>
        <p className="text-base leading-relaxed">
          Сейчас в «Карамели» регистрируются женщины. Заявка на участие для мужчин
          откроется в следующем обновлении — мы напишем, когда это произойдёт.
        </p>
        <p className="text-sm text-[#5A5249]">
          Ничего заполнять не нужно: аккаунт уже создан и ждёт открытия заявок.
        </p>
      </main>
    );
  }

  // AC-01 стори 4.5, UX §968: лента карточек — ГЛАВНЫЙ экран кабинета
  // («вопрос человека: куда я могу пойти и есть ли там место?»).
  //
  // Раньше здесь была заглушка «Аккаунт готов» со ссылкой на анкету. Она
  // осталась ниже как вторичная ссылка: анкета нужна, но она не цель
  // продукта — цель «пойти на встречу». Человек, впервые открывший кабинет,
  // видит ленту сразу, без онбординг-карусели (UX §794).
  return (
    <main className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Встречи рядом</h1>
        <p className="text-sm text-[#5A5249]">
          Выберите, куда хотите присоединиться, или создайте свою встречу.
        </p>
      </header>

      <CardFeed />

      <footer className="flex flex-wrap gap-3 border-t border-[#F2EDE6] pt-4">
        <Link
          href="/app/cards/new"
          className="min-h-11 rounded-md bg-[#7A3E12] px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#7A3E12]"
        >
          Создать карточку
        </Link>
        <Link
          href="/app/profile"
          className="min-h-11 rounded-md border border-[#D8CFC4] px-4 py-2 text-sm font-medium text-[#3D3529] transition-colors hover:bg-[#F2EDE6] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#7A3E12]"
        >
          Моя анкета
        </Link>
        <p className="w-full text-sm text-[#5A5249]">
          Фотографии в анкете проходят проверку модератора — это касается фото, а не аккаунта.
        </p>
      </footer>
    </main>
  );
}
