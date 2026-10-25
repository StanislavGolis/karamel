/**
 * Экран создания карточки события (CR 4.2, патч 4; AC-09).
 *
 * Дефект, найденный ревью: `CardCreateForm` был написан, но нигде не
 * рендерился — экрана не существовало, и AC-09 («экран создания карточки»)
 * не выполнялся: форму нельзя было открыть руками ни в браузере, ни на стенде.
 *
 * Серверный компонент: сессию и справочник мест нужно знать до отрисовки
 * (FR98: место выбирается только из активного справочника своего сообщества).
 *
 * Гарды дублируют layout кабинета намеренно (паттерн `app/profile/page.tsx`):
 * прямой рендер страницы без layout не должен открывать форму без сессии
 * или без согласия на ПД.
 *
 * `instant = false`: чтение cookie и запрос в API блокируют рендер — иначе
 * разметка формы ушла бы клиенту до проверки сессии.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { CardCreateForm, type PlaceOption } from "@/components/cards/card-create-form";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = {
  robots: { index: false, follow: false },
  title: "Новая карточка — Карамель",
};

export const instant = false;

const API_TIMEOUT_MS = 5_000;

/** Места сообщества: то, что отдаёт `GET /api/v1/places` (3.3). */
async function loadPlaces(cookieHeader: string | null): Promise<PlaceOption[] | null> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(`${apiUrl}/api/v1/places`, {
      headers: cookieHeader ? { cookie: cookieHeader } : {},
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
    // `null` — сбой сервиса, `[]` — справочник пуст. Разные экраны:
    // пустой справочник это состояние продукта, а не ошибка (§18.2).
    if (!response.ok) return null;
    const body = (await response.json()) as {
      items?: Array<{ uuid: string; name: string; address_text: string; category: string }>;
    };
    return (body.items ?? []).map((place) => ({
      uuid: place.uuid,
      name: place.name,
      address_text: place.address_text,
      category: place.category,
    }));
  } catch {
    return null;
  }
}

export default async function NewCardPage() {
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
          Не удалось загрузить данные аккаунта. Обновите страницу через минуту —
          выходить из аккаунта не нужно.
        </p>
      </main>
    );
  }

  if (result.kind === "blocked") redirect("/login?error=user_blocked");
  if (result.kind === "unauthenticated") redirect("/login");
  if (!hasConsent(result.user)) redirect("/consent");

  const places = await loadPlaces(cookieHeader || null);

  if (places === null) {
    return (
      <main className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Справочник недоступен</h1>
        <p className="text-base leading-relaxed">
          Не удалось загрузить места сообщества. Обновите страницу через минуту —
          карточку пока создать нельзя: без справочника выбрать место невозможно.
        </p>
      </main>
    );
  }

  return (
    <main className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Новая карточка</h1>
        <p className="text-base leading-relaxed text-[#5A5249]">
          Пять полей — что, когда, где, сколько человек и описание. Компания соберётся сама.
        </p>
      </div>
      <CardCreateForm places={places} />
    </main>
  );
}
