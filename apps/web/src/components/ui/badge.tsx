"use client";

/**
 * Badge — статусная метка (§6.3).
 *
 * **Варианта `danger` нет и не будет** (§6.3 п.3, §2.3): бейдж не бывает
 * тревогой. `danger` в системе зарезервирован под сбой и необратимые
 * действия; штатный статус карточки им не кодируется. Сгенерированный
 * shadcn-файл предлагал `destructive` — удалён.
 *
 * **Текст обязателен.** §2.4 и NFR57: цвет не единственный носитель смысла,
 * поэтому бейдж всегда содержит текст, а иконка — дополнение, не замена.
 *
 * Контрасты проверены гейтом 4: `success` на `success-tint` = 6.00/4.81,
 * `warning` на `warning-tint` = 5.03/5.90, `ink-secondary` на `surface` = 7.13/8.51.
 */

import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  [
    "inline-flex w-fit shrink-0 items-center justify-center gap-1",
    // Радиус 4 (radius-sm, §4.2) — бейдж мельче карточки, и 8 на нём выглядит рыхло.
    "rounded-sm px-2 py-0.5",
    // 14px: служебный текст читаемый, 12 — только метки (§3.2).
    "text-sm font-medium whitespace-nowrap",
  ].join(" "),
  {
    variants: {
      variant: {
        /** Свободные места, подтверждение. Цвет = информация (§2.3). */
        success: "bg-success-tint text-success",
        /** Осталось ≤1 ч. Только для порога, не для «скоро» вообще. */
        warning: "bg-warning-tint text-warning",
        /** «Компания собрана», нейтральный статус. */
        neutral: "bg-surface text-ink-secondary",
        /** Метки, служебное, время. Самый тихий вариант. */
        muted: "text-ink-muted",
        /** Контурная метка без заливки: категория, город. */
        outline: "border border-border text-ink-secondary",
      },
    },
    defaultVariants: {
      variant: "neutral",
    },
  },
);

export type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;

export interface BadgeProps
  extends React.ComponentProps<"span">,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant = "neutral", ...props }: BadgeProps) {
  return (
    <span data-slot="badge" data-variant={variant} className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
