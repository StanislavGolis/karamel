/**
 * Экран входа (Решение 2 код-ревью CR 1.5).
 *
 * До этой страницы все отказы OAuth (`_redirect_login_error`), гарда кабинета
 * и кнопка «Выйти» вели на `/login`, которого не существовало — пользователь
 * получал 404 ровно в момент, когда ему нужен путь назад в приложение.
 *
 * OAuth-кнопки — обычные `<a>` на `/api/proxy/...`: это редирект на сторону
 * провайдера, клиентская навигация Next неприменима, поэтому правило
 * `no-html-link-for-pages` отключено точечно с пояснением.
 *
 * `?error=` читается в дочернем компоненте под `<Suspense>`: `cacheComponents`
 * в Next 16 запрещает `searchParams` в пререндерируемом теле маршрута.
 * Шелл страницы пререндерится, блокируется только баннер ошибки.
 */

import Link from "next/link";
import { Suspense } from "react";

import { PhoneLoginForm } from "@/components/ui/phone-login-form";

/**
 * Тексты отказов. Ключи совпадают с кодами API (`_redirect_login_error`,
 * `_session_error`): новый код без записи здесь виден как общая фраза,
 * а не как пустой экран.
 */
const ERROR_MESSAGES: Record<string, string> = {
  oauth_denied: "Вход отменён на стороне провайдера. Попробуйте ещё раз.",
  oauth_failed: "Провайдер не ответил. Попробуйте войти ещё раз.",
  invalid_state: "Ссылка входа устарела или открыта в другом браузере. Начните вход заново.",
  invalid_callback: "Провайдер вернул неполные данные. Попробуйте войти ещё раз.",
  user_blocked: "Доступ к аккаунту ограничен. Напишите в поддержку.",
  session_expired: "Сессия истекла. Войдите снова.",
};

const DEFAULT_ERROR = "Не удалось войти. Попробуйте ещё раз.";

/** Общие классы кнопок-провайдеров: одинаковое поведение мыши и фокуса. */
const PROVIDER_BUTTON =
  "flex min-h-11 items-center justify-center rounded-md border border-border-input px-4 py-2 text-sm font-medium text-ink transition-colors hover:bg-surface focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring";

/**
 * Баннер отказа. Отдельный компонент, потому что только он трогает
 * `searchParams` — динамическое чтение не должно блокировать всю страницу.
 */
async function LoginError({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;
  if (!error) return null;
  // Неизвестный код не выводится как есть: он пришёл из адресной строки,
  // то есть контролируется клиентом.
  const message = ERROR_MESSAGES[error] ?? DEFAULT_ERROR;
  return (
    <p role="alert" className="rounded-md bg-danger-tint px-4 py-3 text-sm text-danger">
      {message}
    </p>
  );
}

export default function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-4 py-16">
      <div className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">Вход в Карамель</h1>
        <p className="text-sm text-ink-secondary">
          Один аккаунт — одна анкета. Вход по номеру телефона или через Яндекс ID и VK ID.
        </p>
      </div>

      <Suspense fallback={null}>
        <LoginError searchParams={searchParams} />
      </Suspense>

      <div className="flex flex-col gap-3">
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- редирект на сторону провайдера, не страница Next */}
        <a href="/api/proxy/auth/yandex/start" className={PROVIDER_BUTTON}>
          Войти через Яндекс ID
        </a>
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- редирект на сторону провайдера, не страница Next */}
        <a href="/api/proxy/auth/vk/start" className={PROVIDER_BUTTON}>
          Войти через VK ID
        </a>
      </div>

      <PhoneLoginForm />

      <Link href="/" className="text-sm text-ink-secondary underline underline-offset-4">
        На главную
      </Link>
    </main>
  );
}
