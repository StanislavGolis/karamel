/**
 * Фолбэк загрузки анкеты модератора (стори 12.10; AC-8, D-12.10-5).
 *
 * **Первый `loading.tsx` в проекте.** До 12.10 кабинет модератора не имел ни
 * одного — серверные страницы отдавали пустой экран до готовности разметки.
 * AC-8 требует видимого фолбэка, а не пустоты.
 *
 * **Почему `loading.tsx`, а не клиентское состояние.** Страница — серверный
 * компонент (`instant = false`): она ждёт ответ API до отрисовки, и
 * клиентского состояния «загружается» у неё нет by design. `loading.tsx` —
 * единственный механизм Next, который показывает разметку, пока серверный
 * компонент не готов.
 *
 * **Совместимость с `cacheComponents: true` (Next 16.4.2).** `loading.tsx`
 * оборачивает сегмент в `Suspense`; `cacheComponents` запрещает
 * `export const dynamic`, но не `loading` — это разные оси (в 11.2 `dynamic`
 * был невозможен именно из-за кэш-компонентов). Проверено прогоном: страница
 * собирается и отдаёт фолбэк (тест `loading.tsx` рендерится и не пуст).
 *
 * **Скелетон, не спиннер** (§11 project-context: спиннеры запрещены).
 * `aria-busy` сообщает вспомогательным технологиям, что содержимое ещё идёт.
 */

export default function ModerationUserLoading() {
  return (
    <main className="flex flex-col gap-4" aria-busy="true" aria-live="polite">
      <span className="text-sm text-ink-secondary">← Пользователь</span>
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className="h-12 w-12 shrink-0 rounded-full bg-surface"
        />
        <span className="h-4 w-40 rounded-sm bg-surface" />
      </div>
      <span className="text-base text-ink-secondary">Загрузка анкеты…</span>
    </main>
  );
}
