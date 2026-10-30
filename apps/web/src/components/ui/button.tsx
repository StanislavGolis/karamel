"use client";

/**
 * Button — главный примитив (§6.4 артефакта дизайн-системы v2.3.0).
 *
 * Через него проходит вся семантика действий, поэтому контракт жёсткий:
 *
 * | Вариант | Фон | Текст | Когда |
 * |---|---|---|---|
 * | `primary` | `accent` | `accent-ink` | Главное действие экрана. ОДНО на экран |
 * | `secondary` | прозрачный + `border-input` | `ink` | Второе действие: «Написать», «Отмена» |
 * | `quiet` | прозрачный | `ink-secondary` | Третье: «Пожаловаться», «Позже» |
 * | `danger` | `danger` | **`accent-ink`** | Только необратимое |
 *
 * **Текст на `danger` = `accent-ink`, не `#FFFFFF`.** Жёсткий белый в тёмной
 * теме даёт 3.16:1 — это был реальный дефект (ревью Б-2). Замер: светлая
 * `#F4F2EE` на `#A0281C` = 6.67:1, тёмная `#161513` на `#E0705E` = 5.78:1.
 *
 * **Почему не оставлен сгенерированный shadcn-файл.** Он говорит на словаре
 * `bg-primary` / `text-muted-foreground` / `dark:*`, которого в нашей системе
 * нет: §17.4 запрещает `dark:`-классы (тема меняет значения переменных, а не
 * классы), а §6.5 п.2 требует токены вместо чужого словаря. Стиль — только
 * через варианты.
 */

import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  [
    "inline-flex shrink-0 items-center justify-center gap-2",
    // Радиус 8 (radius-md, §4.2). В плотном режиме 4 — задаётся потребителем в (mod).
    "rounded-md",
    // Высота 48 = space-7 (§4.1). Тап-зона ≥44 выполнена с запасом.
    "min-h-12 px-6",
    "text-sm font-medium",
    // §13.1: outline только с заменой, 2px + offset 2px.
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
    // §5.1: смена состояния 120ms, ease-out. Не `transition-all`: он анимирует
    // и размеры, из-за чего кнопка «дышит» при смене текста.
    "transition-colors duration-fast ease-out",
    // Неактивность: причина — в `aria-disabled`, а не только в цвете (§6.4).
    "disabled:cursor-not-allowed disabled:bg-disabled-bg disabled:text-disabled-ink",
    "[&_svg]:pointer-events-none [&_svg]:shrink-0",
  ].join(" "),
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-ink hover:bg-accent-hover active:bg-accent-pressed",
        secondary:
          "border border-border-input bg-transparent text-ink hover:bg-surface-hover active:bg-surface-raised",
        quiet: "bg-transparent text-ink-secondary hover:bg-surface-hover active:bg-surface-raised",
        // Опасно и необратимо. Не «ошибка пользователя» — факт домена.
        danger: "bg-danger text-accent-ink hover:opacity-90 active:opacity-95",
      },
      size: {
        // 48px — продуктовый размер (§6.4).
        default: "min-h-12",
        // 36px — плотный режим кабинета модератора (§4.4).
        compact: "min-h-9 rounded-sm px-4",
        // Кнопка-иконка: 44×44 обязательна (§13.1), текст заменяется `aria-label`.
        icon: "min-h-11 min-w-11 px-0",
      },
    },
    defaultVariants: {
      variant: "primary",
      size: "default",
    },
  },
);

export type ButtonVariant = NonNullable<VariantProps<typeof buttonVariants>["variant"]>;

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /**
   * Запрос в полёте (§6.4).
   *
   * Текст НЕ меняется: «Отправить» не превращается в «Отправляем…», иначе
   * кнопка прыгает по ширине. Меняется только `aria-busy` и появляется точка.
   */
  loading?: boolean;
  /** Причина недоступности — читается скринридером (§13.3). */
  disabledReason?: string;
}

/**
 * Кнопка.
 *
 * `aria-busy` вместо `disabled` при загрузке: блокировать фокус на время
 * запроса — значит выкинуть человека из потока. Кнопка остаётся в Tab-порядке.
 */
export function Button({
  className,
  variant = "primary",
  size = "default",
  loading = false,
  disabled,
  disabledReason,
  children,
  ...props
}: ButtonProps) {
  const reasonId = React.useId();
  const isDisabled = Boolean(disabled) && !loading;

  return (
    <button
      data-slot="button"
      data-variant={variant}
      data-size={size}
      // §6.4: `aria-disabled` сообщает причину; disabled-атрибут убирает из потока.
      disabled={isDisabled}
      aria-disabled={isDisabled || undefined}
      aria-busy={loading || undefined}
      aria-describedby={isDisabled && disabledReason ? reasonId : undefined}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    >
      {children}
      {loading ? (
        // Точка, а не спиннер-кольцо: кольцо читается как «загрузка экрана».
        // `aria-hidden` — состояние уже объявлено через aria-busy.
        <span
          aria-hidden="true"
          className="inline-block size-1.5 rounded-full bg-current motion-safe:animate-pulse"
        />
      ) : null}
      {isDisabled && disabledReason ? (
        <span id={reasonId} className="sr-only">
          {disabledReason}
        </span>
      ) : null}
    </button>
  );
}

export { buttonVariants };
