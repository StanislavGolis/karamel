"use client";

/**
 * Input — однострочное поле (§6.3, состояния: default/focus/filled/error/disabled/readOnly).
 *
 * **Высота 48 (min-h-12), не 32.** Сгенерированный shadcn-файл имел `h-8` = 32px,
 * что нарушает тап-зону ≥44×44 (§13.1) и правило «тап-зона на уровне токена» (§4.6).
 *
 * **Текст 16px, не 14.** `md:text-sm` из генератора нарушал §3.2: основной текст
 * на продуктовых экранах никогда не меньше 16 — иначе iOS Safari ещё и зумит
 * страницу при фокусе.
 *
 * **Ошибка — через `aria-invalid`,** а не отдельным булевым пропом: семантика
 * одна на все формы, и скринридер получает состояние бесплатно (§13.3).
 */

import * as React from "react";

import { cn } from "@/lib/utils";

export type InputProps = React.ComponentProps<"input">;

function Input({ className, type, ...props }: InputProps) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        // Тап-зона 48, радиус 8, паддинг 16 (§4.1 space-4).
        "flex min-h-12 w-full min-w-0 rounded-md border border-border-input bg-surface-raised px-4 py-2",
        "text-base text-ink",
        "placeholder:text-ink-muted",
        // §13.1: 2px focus-ring + offset 2px. `outline-none` без замены запрещён.
        "outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
        "transition-colors duration-fast ease-out",
        // Неактивность: фон и текст из §6.4; курсор — подсказка, что поле закрыто.
        "disabled:cursor-not-allowed disabled:border-border-subtle disabled:bg-disabled-bg disabled:text-disabled-ink",
        // readOnly — не ошибка и не «выключено»: данные есть, менять нельзя (§6.3).
        "read-only:border-border-subtle read-only:bg-surface read-only:text-ink-secondary",
        // Ошибка: рамка danger + текст остаётся ink (danger только как признак).
        "aria-invalid:border-danger aria-invalid:focus-visible:outline-danger",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
