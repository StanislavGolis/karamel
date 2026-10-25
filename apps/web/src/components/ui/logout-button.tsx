"use client";

/**
 * Кнопка «Выйти» (Task 10; AC-04).
 *
 * Клиентский компонент, потому что нужен `router`. Запрос идёт через BFF
 * (`/api/proxy/auth/logout`), а не напрямую в API: только так cookie
 * очищаются на той же origin, где были выданы (AC-03).
 *
 * Подтверждения нет: выход отменяем повторным входом, а project-context §11
 * требует не спрашивать там, где действие обратимо. Повторное нажатие
 * безопасно — эндпоинт идемпотентен (AC-04).
 */

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

export function LogoutButton() {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  async function logout(): Promise<void> {
    setError(null);
    try {
      // Проверка `res.ok` не нужна по смыслу: эндпоинт всегда 204.
      // Сетевой отказ — единственная реальная ошибка, её и показываем.
      await fetch("/api/proxy/auth/logout", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
      });
      startTransition(() => {
        router.replace("/login");
        // `refresh()` обязателен: layout — серверный компонент и держит
        // результат `cookies()` в кэше. Без сброса кэша переход на `/login`
        // остался бы в дереве со старым состоянием сессии.
        router.refresh();
      });
    } catch {
      setError("Не удалось выйти. Проверьте соединение и попробуйте снова.");
    }
  }

  return (
    <div className="flex flex-col items-end gap-2">
      <button
        type="button"
        onClick={logout}
        disabled={isPending}
        aria-label="Выйти из аккаунта"
        className="min-h-11 min-w-11 rounded-md border border-border-input px-4 py-2 text-sm font-medium text-ink transition-colors hover:bg-surface focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:opacity-60"
      >
        {isPending ? "Выходим…" : "Выйти"}
      </button>
      {error !== null && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
