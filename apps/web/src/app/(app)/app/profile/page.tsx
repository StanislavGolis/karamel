/**
 * Экран анкеты (Task 10.4; AC-07).
 *
 * Серверный компонент: сессию и наличие анкеты нужно знать до отрисовки.
 * Форма получает `initial` и сама решает, `POST` или `PATCH` (D-1 стори 2.3).
 *
 * Гарды дублируют layout кабинета намеренно (паттерн `app/page.tsx`):
 * прямой рендер страницы без layout (тесты, будущий рефакторинг) не должен
 * открывать форму без сессии или без согласия на ПД.
 *
 * `instant = false`: чтение cookie и запрос в API блокируют рендер. Иначе
 * `cacheComponents` отдал бы разметку формы до проверки сессии (AC-07).
 */

import Link from "next/link";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { ProfileForm, type ProfileValues } from "@/components/profile/profile-form";
import { ProfilePhotos, type PhotoView } from "@/components/profile/profile-photos";
import { ThemeToggle } from "@/components/profile/theme-toggle";
import { hasConsent, loadSession } from "@/lib/auth-server";

export const metadata = {
  robots: { index: false, follow: false },
  title: "Анкета — Карамель",
};

export const instant = false;

const API_TIMEOUT_MS = 5_000;

/**
 * Результат чтения анкеты.
 *
 * CR 2.3, патч 11: три состояния вместо двух. Раньше любая ошибка API
 * (`403`, `500`, таймаут) схлопывалась в `null` — «анкеты нет», — и
 * пользователь с готовой анкетой видел пустую форму создания: отправка
 * давала `409 profile_exists`, и выбраться из этого круга было нельзя.
 */
type ProfileState =
  | { kind: "missing" }
  | { kind: "found"; values: Partial<ProfileValues> }
  | { kind: "consent" }
  | { kind: "unavailable" };

/**
 * Читает свою анкету через API.
 *
 * `404` — анкеты нет (нормальный случай первого заполнения).
 * `403 consent_required` — нужен шаг согласия, а не новая анкета.
 * `5xx`, таймаут, нечитаемое тело — сбой сервиса: показывать форму нельзя,
 * иначе пользователь отправит данные, которые уже есть.
 *
 * Возвращается плоский объект из четырёх полей, а не ответ целиком: форме
 * не нужны `uuid`, `created_at`, `gender` — и передавать их в клиентский
 * компонент значило бы класть лишние ПД в разметку.
 */
async function loadProfile(cookieHeader: string): Promise<ProfileState> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(`${apiUrl}/api/v1/profiles/me`, {
      headers: { cookie: cookieHeader },
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });

    // AC-07: анкеты нет — форма создания. `404` здесь ожидаем и не является
    // сбоем: без него пользователь не смог бы заполнить анкету впервые.
    if (response.status === 404) return { kind: "missing" };

    // CR 2.3, патч 11: `403` — это не «анкеты нет», а «нет согласия».
    // Показав форму создания, страница отправила бы пользователя заполнять
    // анкету, которую сервер всё равно не примет.
    if (response.status === 403) return { kind: "consent" };

    // CR 2.3, патч 11: `5xx` и прочие отказы — сбой, а не пустая анкета.
    if (!response.ok) return { kind: "unavailable" };

    const payload = (await response.json()) as {
      name?: string | null;
      city?: string | null;
      age?: number | null;
      bio?: string | null;
    };

    return {
      kind: "found",
      values: {
        name: payload.name ?? "",
        city: payload.city ?? "",
        // Число → строка: `value` поля ввода должно быть строкой, иначе React
        // ругается на смену контролируемости при пустом возрасте.
        age: payload.age === null || payload.age === undefined ? "" : String(payload.age),
        bio: payload.bio ?? "",
      },
    };
  } catch {
    // CR 2.3, патч 11: недоступный API — это сбой, а не «анкеты нет».
    // Пустая форма создания заставила бы пользователя с готовой анкетой
    // отправить её заново и получить `409 profile_exists`.
    return { kind: "unavailable" };
  }
}

/**
 * Фото владельца для первичной отрисовки (Task 9.2, AC-01, AC-03).
 *
 * Загрузка на сервере, а не в `useEffect` клиента: ESLint
 * `react-hooks/set-state-in-effect` справедливо запрещает `setState`
 * в теле эффекта, а тянуть данные в браузер ради первой отрисовки
 * значило бы показать пустую сетку, пока идёт запрос.
 *
 * `null` — запрос не удался: клиент покажет сообщение, но НЕ «фото нет».
 * Пустой список и сбой различимы: `[]` — фото действительно нет (AC-12).
 */
async function loadPhotos(cookieHeader: string): Promise<PhotoView[] | null> {
  const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

  try {
    const response = await fetch(`${apiUrl}/api/v1/profiles/me/photos`, {
      headers: { cookie: cookieHeader },
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });

    if (!response.ok) return null;

    const payload = (await response.json()) as { items?: PhotoView[] };
    return payload.items ?? [];
  } catch {
    return null;
  }
}

export default async function ProfilePage() {
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

  // AC-07: без согласия на ПД экран недоступен (та же гарда, что в layout).
  if (!hasConsent(result.user)) redirect("/consent");

  const profile = await loadProfile(cookieHeader);

  // CR 2.3, патч 11: согласие и сбой сервиса — разные экраны, не форма.
  if (profile.kind === "consent") redirect("/consent");
  if (profile.kind === "unavailable") {
    return (
      <main className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Анкета недоступна</h1>
        <p className="text-base leading-relaxed">
          Не удалось загрузить вашу анкету. Обновите страницу через минуту —
          данные не потеряны.
        </p>
      </main>
    );
  }

  const initial = profile.kind === "found" ? profile.values : null;
  const isEdit = initial !== null;

  // Task 9.2: фото читаются на сервере вместе с анкетой — одна поездка
  // в API на отрисовку и никакого «сначала пусто, потом список».
  const photos = await loadPhotos(cookieHeader);

  return (
    <main className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          {isEdit ? "Ваша анкета" : "Заполните анкету"}
        </h1>
        <p className="text-base leading-relaxed text-ink-secondary">
          {isEdit
            ? "Изменения видно сразу — их не проверяет модератор."
            : "Это ваша карточка в подборе. Заполнить можно за пару минут."}
        </p>
      </div>
      {/* Стори 9.3 (Task 5.3): вход в настройки канала уведомлений (FR46).
          Ссылка стоит ДО формы анкеты: настройка уведомлений — частая причина
          зайти в профиль («перестаньте мне писать»), и заставлять человека
          прокручивать анкету ради неё значило бы прятать пункт.
          `prefetch={false}`: экран настроек делает запрос в API на рендере,
          и предзагрузка при каждом открытии профиля давала бы лишний вызов
          на пользователя, который в настройки не собирался. */}
      <Link
        href="/app/profile/notifications"
        prefetch={false}
        className="flex items-center justify-between gap-3 rounded-2xl border border-border px-4 py-3 text-base leading-relaxed transition-colors hover:border-border-input"
      >
        <span>Уведомления</span>
        <span className="text-ink-secondary" aria-hidden="true">
          Выбрать, о чём присылать
        </span>
      </Link>
      {/* Стори 9.5 (Task 6.5): вход в поддержку (FR55). Ссылка рядом с
          «Уведомлениями», а не в подвале: обращение — действие того же уровня,
          что настройка канала. `prefetch={false}` — экран делает запрос в API
          на рендере, и предзагрузка при каждом открытии профиля давала бы
          лишний вызов тому, кто в поддержку не собирался. */}
      <Link
        href="/app/profile/support"
        prefetch={false}
        className="flex items-center justify-between gap-3 rounded-2xl border border-border px-4 py-3 text-base leading-relaxed transition-colors hover:border-border-input"
      >
        <span>Поддержка</span>
        <span className="text-ink-secondary" aria-hidden="true">
          Написать обращение
        </span>
      </Link>
      {/* Волна 1 дизайн-системы (§17.9): переключатель темы живёт в настройках
          профиля, а не в шапке. Шапка занята городом и фильтрами, а тема —
          редкое действие (один раз); иконка-солнце в шапке была бы визуальным
          шумом. Блок стоит ПОСЛЕ ссылок на уведомления и поддержку: те две —
          частые причины зайти в профиль, тема — редкая. */}
      <section className="flex flex-col gap-3 rounded-2xl border border-border p-4">
        <h2 className="text-lg font-semibold tracking-tight">Внешний вид</h2>
        <ThemeToggle />
      </section>
      <ProfileForm initial={initial} />
      {/* Task 9.7 стори 2.4: статусы модерации фото и замена отклонённого.
          `hasProfile` нужен, чтобы при пустой анкете подсказать порядок
          действий («фото можно добавить после заполнения анкеты»), а не
          показывать пустую сетку без объяснения. */}
      <ProfilePhotos hasProfile={isEdit} initialPhotos={photos} />
    </main>
  );
}
