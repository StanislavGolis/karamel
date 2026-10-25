"use client";

/**
 * Dialog — модальное подтверждение (§6.3).
 *
 * **Только для необратимого** (§6.5 п.4, §10.1). Всё отменяемое — без диалога:
 * правило продукта «отменяемое не спрашиваем». Диалог на «вы уверены, что
 * хотите отправить заявку?» — это не забота, а трение.
 *
 * **`dialog` не открывается поверх `sheet`** (§5.4): опасное действие требует
 * закрыть панель, иначе подтверждение перекрывает контекст, ради которого
 * человек панель открыл.
 *
 * **Два варианта:** `confirm` (нейтральное необратимое — «Удалить фото?») и
 * `destructive` (необратимое опасное — «Удалить аккаунт», «Вынести санкцию»).
 * Разница — в кнопке подтверждения, а не в цвете всего диалога: красная
 * рамка вокруг безопасного текста читается как тревога без причины.
 *
 * Radix даёт фокус-ловушку, `Escape` = отмена и возврат фокуса (§13.4).
 */

import * as React from "react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type DialogTone = "confirm" | "destructive";

function Dialog({ ...props }: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="dialog" {...props} />;
}

function DialogTrigger({ ...props }: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="dialog-trigger" {...props} />;
}

function DialogPortal({ ...props }: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />;
}

function DialogClose({ ...props }: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="dialog-close" {...props} />;
}

/** Затемнение. Слой 30 (`--z-scrim`), §5.4. */
function DialogOverlay({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        "fixed inset-0 z-30 bg-ink/40",
        "motion-safe:data-open:animate-in motion-safe:data-open:fade-in-0",
        "motion-safe:data-closed:animate-out motion-safe:data-closed:fade-out-0",
        className,
      )}
      {...props}
    />
  );
}

function DialogContent({
  className,
  children,
  showCloseButton = true,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & { showCloseButton?: boolean }) {
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          // Слой 50 (`--z-dialog`), §5.4.
          "fixed left-1/2 top-1/2 z-50 grid w-full max-w-[calc(100%-2rem)] -translate-x-1/2 -translate-y-1/2 gap-4",
          "rounded-md border border-border bg-surface-raised p-6 text-base text-ink shadow-overlay",
          "outline-none sm:max-w-sm",
          "motion-safe:data-open:animate-in motion-safe:data-open:fade-in-0 motion-safe:data-open:zoom-in-95",
          "motion-safe:data-closed:animate-out motion-safe:data-closed:fade-out-0 motion-safe:data-closed:zoom-out-95",
          className,
        )}
        {...props}
      >
        {children}
        {showCloseButton ? (
          <DialogPrimitive.Close asChild>
            <Button variant="quiet" size="icon" className="absolute right-2 top-2" aria-label="Закрыть">
              <XIcon aria-hidden="true" />
            </Button>
          </DialogPrimitive.Close>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPortal>
  );
}

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="dialog-header" className={cn("flex flex-col gap-2", className)} {...props} />;
}

/**
 * Кнопки диалога.
 *
 * На мобильном — колонкой (главная снизу, под большим пальцем), на десктопе —
 * строкой справа. `flex-col-reverse` даёт правильный порядок в обоих случаях.
 */
function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
        "-mx-6 -mb-6 mt-2 flex flex-col-reverse gap-2 rounded-b-md border-t border-border bg-surface p-4 sm:flex-row sm:justify-end",
        className,
      )}
      {...props}
    />
  );
}

/**
 * Кнопка подтверждения.
 *
 * Вынесена отдельно, потому что вариант `destructive` обязан быть `danger`,
 * а `confirm` — `primary`: разница между «Удалить фото?» и «Удалить аккаунт?»
 * не должна решаться на месте вызова.
 */
function DialogConfirm({
  tone = "confirm",
  children,
  ...props
}: React.ComponentProps<typeof Button> & { tone?: DialogTone }) {
  return (
    <Button variant={tone === "destructive" ? "danger" : "primary"} {...props}>
      {children}
    </Button>
  );
}

function DialogTitle({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn("text-lg font-semibold tracking-tight text-ink", className)}
      {...props}
    />
  );
}

function DialogDescription({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn("text-base leading-relaxed text-ink-secondary", className)}
      {...props}
    />
  );
}

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
  DialogConfirm,
};
