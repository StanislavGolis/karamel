"use client";

/**
 * Sheet — нижняя панель (§6.3, состояния: closed/opening/open/closing).
 *
 * **Варианты `bottom` (мобильный) и `side` (десктоп).** Не «right по умолчанию»:
 * на телефоне панель выезжает снизу — это жест, который человек уже знает; на
 * десктопе сбоку. Направление задаётся вариантом, а не классом на месте.
 *
 * **`sheet` может открыться поверх `sheet`** — максимум два уровня, второй —
 * детали (§5.4). `dialog` поверх `sheet` НЕ открывается: опасное действие
 * требует закрыть панель — иначе подтверждение перекрывает контекст.
 *
 * **Слой 40** (`--z-sheet`), scrim 30. Значения — из §5.4, не «z-50 на глаз».
 *
 * **`shadow-overlay` — единственная тень** (§4.3), и она здесь разрешена:
 * панель реально перекрывает содержимое. Карточке в ленте — запрещена.
 *
 * Radix даёт фокус-ловушку, `Escape`, возврат фокуса на триггер (§13.4) —
 * поэтому примитив не пишется с нуля.
 */

import * as React from "react";
import { Dialog as SheetPrimitive } from "radix-ui";
import { XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type SheetSide = "bottom" | "side";

function Sheet({ ...props }: React.ComponentProps<typeof SheetPrimitive.Root>) {
  return <SheetPrimitive.Root data-slot="sheet" {...props} />;
}

function SheetTrigger({ ...props }: React.ComponentProps<typeof SheetPrimitive.Trigger>) {
  return <SheetPrimitive.Trigger data-slot="sheet-trigger" {...props} />;
}

function SheetClose({ ...props }: React.ComponentProps<typeof SheetPrimitive.Close>) {
  return <SheetPrimitive.Close data-slot="sheet-close" {...props} />;
}

function SheetPortal({ ...props }: React.ComponentProps<typeof SheetPrimitive.Portal>) {
  return <SheetPrimitive.Portal data-slot="sheet-portal" {...props} />;
}

/** Затемнение под панелью. Слой 30 (§5.4), не 50. */
function SheetOverlay({ className, ...props }: React.ComponentProps<typeof SheetPrimitive.Overlay>) {
  return (
    <SheetPrimitive.Overlay
      data-slot="sheet-overlay"
      className={cn(
        "fixed inset-0 z-30 bg-ink/40",
        // §5.2: при reduced-motion движение гасится целиком.
        "motion-safe:data-open:animate-in motion-safe:data-open:fade-in-0",
        "motion-safe:data-closed:animate-out motion-safe:data-closed:fade-out-0",
        className,
      )}
      {...props}
    />
  );
}

function SheetContent({
  className,
  children,
  side = "bottom",
  showCloseButton = true,
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Content> & {
  side?: SheetSide;
  showCloseButton?: boolean;
}) {
  return (
    <SheetPortal>
      <SheetOverlay />
      <SheetPrimitive.Content
        data-slot="sheet-content"
        data-side={side}
        className={cn(
          "fixed z-40 flex flex-col gap-4 bg-surface-raised text-base text-ink shadow-overlay",
          // §5.1: появление панели 200ms ease-out.
          "motion-safe:transition-transform motion-safe:duration-200 motion-safe:ease-out",
          side === "bottom"
            ? "inset-x-0 bottom-0 max-h-[85vh] rounded-t-md border-t border-border px-4 pb-6 pt-4"
            : "inset-y-0 right-0 h-full w-3/4 max-w-sm border-l border-border p-4 sm:max-w-sm",
          "motion-safe:data-open:animate-in motion-safe:data-open:fade-in-0",
          side === "bottom"
            ? "motion-safe:data-open:slide-in-from-bottom-4"
            : "motion-safe:data-open:slide-in-from-right-4",
          "motion-safe:data-closed:animate-out motion-safe:data-closed:fade-out-0",
          side === "bottom"
            ? "motion-safe:data-closed:slide-out-to-bottom-4"
            : "motion-safe:data-closed:slide-out-to-right-4",
          className,
        )}
        {...props}
      >
        {children}
        {showCloseButton ? (
          <SheetPrimitive.Close asChild>
            {/* Тап-зона 44 и `aria-label` — кнопка-иконка без текста (§6.4). */}
            <Button variant="quiet" size="icon" className="absolute right-3 top-3" aria-label="Закрыть">
              <XIcon aria-hidden="true" />
            </Button>
          </SheetPrimitive.Close>
        ) : null}
      </SheetPrimitive.Content>
    </SheetPortal>
  );
}

function SheetHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="sheet-header" className={cn("flex flex-col gap-1", className)} {...props} />;
}

function SheetFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-footer"
      // `mt-auto` прижимает кнопки к низу: панель не «висит», действие на месте.
      className={cn("mt-auto flex flex-col gap-2", className)}
      {...props}
    />
  );
}

function SheetTitle({ className, ...props }: React.ComponentProps<typeof SheetPrimitive.Title>) {
  return (
    <SheetPrimitive.Title
      data-slot="sheet-title"
      // §3.2: заголовок карточки/панели 18px, не 16.
      className={cn("text-lg font-semibold tracking-tight text-ink", className)}
      {...props}
    />
  );
}

function SheetDescription({ className, ...props }: React.ComponentProps<typeof SheetPrimitive.Description>) {
  return (
    <SheetPrimitive.Description
      data-slot="sheet-description"
      className={cn("text-sm text-ink-secondary", className)}
      {...props}
    />
  );
}

export {
  Sheet,
  SheetTrigger,
  SheetClose,
  SheetContent,
  SheetHeader,
  SheetFooter,
  SheetTitle,
  SheetDescription,
};
