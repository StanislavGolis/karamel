/**
 * Единый компонент пустоты (UX §873, `EmptyState`; стори 4.5, Task 7.4; AC-09).
 *
 * Заменяет `EmptySelection` v1. Правило UX: **любой экран со списком обязан
 * отрендерить `EmptyState`, а не пустой `<div>`** — пустой экран без объяснения
 * и без действия читается как «сломалось», и человек уходит.
 *
 * `actionKind: "primary"` — акцентная кнопка, когда причина пустоты в том, что
 * данных ещё нет вообще («Создать карточку»). `"quiet"` — тихая, когда причина
 * в фильтре («Сбросить фильтр»): предлагать создать в этом случае значит
 * уводить от настоящей причины (UX §775).
 *
 * `reason` — короткое объяснение, почему пусто. Без него `title` звучал бы
 * приговором, а не приглашением.
 */

type ActionKind = "primary" | "quiet";

type EmptyStateProps = {
  title: string;
  reason: string;
  action?: string;
  actionKind?: ActionKind;
  onAction?: () => void;
};

/**
 * Классы кнопки по уровню акцента.
 *
 * Цвета — те же, что в формах проекта (`card-create-form.tsx`, `profile-form.tsx`):
 * тёмно-коричневый акцент и нейтральная рамка. Новых токенов не вводится,
 * иначе пустое состояние выпадало бы из палитры остальных экранов.
 */
const ACTION_CLASSES: Record<ActionKind, string> = {
  primary:
    "min-h-11 rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-ink transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
  quiet:
    "min-h-11 rounded-md border border-border px-4 py-2 text-sm font-medium text-ink transition-colors hover:bg-surface focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
};

export function EmptyState({
  title,
  reason,
  action,
  actionKind = "primary",
  onAction,
}: EmptyStateProps) {
  return (
    <div className="flex flex-col items-start gap-3 rounded-lg border border-border bg-surface-raised p-6">
      <h2 className="text-lg font-semibold tracking-tight text-ink">{title}</h2>
      <p className="text-sm leading-relaxed text-ink-secondary">{reason}</p>
      {action && onAction ? (
        <button type="button" onClick={onAction} className={ACTION_CLASSES[actionKind]}>
          {action}
        </button>
      ) : null}
    </div>
  );
}
