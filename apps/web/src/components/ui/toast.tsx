"use client";

/**
 * Toast — результат действия, которое уже ушло (§6.3, §10.1-10.2).
 *
 * **Не стек: одновременно не больше одного** (§10.2). Второй заменяет первый.
 * Radix сам держит очередь, но политика «заменять, а не копить» — наша: тост,
 * который ждёт своей очереди, сообщает о событии, которое уже неактуально.
 *
 * **Разное время жизни:** успех 4 с, ошибка 6 с (§10.2). Ошибку надо успеть
 * прочитать; успех — просто подтверждение. Ошибка **не исчезает по наведению**.
 *
 * **`role="status"` для успеха, `role="alert"` для ошибки** (§13.2): разный вес,
 * и `alert` уместен только там, где человек должен прерваться.
 *
 * **Не перехватывает фокус** (§10.2). Тост, крадущий фокус, ломает ввод.
 *
 * **Не единственный носитель ошибки** (§10.1). Ошибка формы живёт inline; тост —
 * для результата действия. Если после исчезновения тоста непонятно, что не так,
 * это дефект, а не «не заметил».
 *
 * Почему не `sonner`: он тянет `next-themes`, а §17.7 прямо запрещает эту
 * зависимость — она приносит модель из трёх состояний (`light`/`dark`/`system`),
 * которой у нас нет. Radix Toast уже стоит как часть `radix-ui`.
 */

import * as React from "react";
import { Toast as ToastPrimitive } from "radix-ui";
import { XIcon } from "lucide-react";

import { cn } from "@/lib/utils";

export type ToastTone = "success" | "info" | "error";

/** §10.2: успех 4 с, ошибка 6 с. Info — как успех: это не тревога. */
const DURATION: Record<ToastTone, number> = {
  success: 4000,
  info: 4000,
  error: 6000,
};

const TONE_CLASS: Record<ToastTone, string> = {
  // Тинт, а не сплошная заливка: §2.4 — статусные цвета только как тинт под текстом.
  success: "bg-success-tint text-success border-success/30",
  // info — нейтральная поверхность: это не статус домена, а сообщение.
  info: "bg-surface-raised text-ink border-border",
  error: "bg-danger-tint text-danger border-danger/30",
};

/**
 * Провайдер: один на приложение.
 *
 * `swipeDirection` — на мобильном тост смахивается вбок; направление задаётся
 * здесь, чтобы не расходилось по экранам.
 */
function ToastProvider({
  children,
  ...props
}: React.ComponentProps<typeof ToastPrimitive.Provider>) {
  return (
    <ToastPrimitive.Provider swipeDirection="right" {...props}>
      {children}
      <ToastViewport />
    </ToastPrimitive.Provider>
  );
}

/** Область тостов: нижний край, над тап-зоной. Слой 60 (§5.4). */
function ToastViewport({ className, ...props }: React.ComponentProps<typeof ToastPrimitive.Viewport>) {
  return (
    <ToastPrimitive.Viewport
      data-slot="toast-viewport"
      className={cn(
        "fixed inset-x-0 bottom-0 z-[60] flex flex-col-reverse gap-2 p-4",
        // На десктопе — справа снизу, не на всю ширину: тост шириной в экран
        // читается как баннер и перекрывает работу.
        "sm:inset-x-auto sm:right-0 sm:max-w-sm",
        className,
      )}
      {...props}
    />
  );
}

/**
 * Тост.
 *
 * Время жизни берётся из варианта, а не задаётся на месте вызова: иначе
 * на одном экране ошибка висит 2 с, на другом — 10.
 */
function Toast({
  className,
  tone = "info",
  children,
  ...props
}: React.ComponentProps<typeof ToastPrimitive.Root> & { tone?: ToastTone }) {
  return (
    <ToastPrimitive.Root
      data-slot="toast"
      data-tone={tone}
      duration={DURATION[tone]}
      // §13.2: разный вес. `alert` прерывает чтение — только для ошибки.
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "pointer-events-auto flex items-start gap-3 rounded-md border px-4 py-3",
        "text-base leading-relaxed shadow-overlay",
        // §5.1: появление/исчезновение 200ms, гасится при reduced-motion.
        "motion-safe:transition-all motion-safe:duration-200 motion-safe:ease-out",
        "motion-safe:data-open:animate-in motion-safe:data-open:slide-in-from-bottom-2",
        "motion-safe:data-closed:animate-out motion-safe:data-closed:fade-out-0",
        TONE_CLASS[tone],
        className,
      )}
      {...props}
    >
      <div className="flex flex-1 flex-col gap-1">
        <ToastTitle />
        <ToastDescription>{children}</ToastDescription>
      </div>
      {/* Тап-зона 44: тост должен закрываться пальцем, а не только сам. */}
      <ToastPrimitive.Close
        aria-label="Закрыть уведомление"
        className={cn(
          "shrink-0 rounded-sm p-2 -m-1",
          "motion-safe:transition-colors motion-safe:duration-fast",
          "outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
          "hover:bg-ink/5",
        )}
      >
        <XIcon className="size-4" aria-hidden="true" />
      </ToastPrimitive.Close>
    </ToastPrimitive.Root>
  );
}

function ToastTitle({ className, ...props }: React.ComponentProps<typeof ToastPrimitive.Title>) {
  return (
    <ToastPrimitive.Title
      className={cn("text-sm font-semibold", className)}
      {...props}
    />
  );
}

function ToastDescription({ className, ...props }: React.ComponentProps<typeof ToastPrimitive.Description>) {
  return <ToastPrimitive.Description className={cn("text-base", className)} {...props} />;
}

/**
 * Императивный помощник для вызова из кода.
 *
 * Не заводим свой стор: Radix держит состояние в контексте, а дублирование
 * очереди — источник расхождения «показали не то, что в состоянии».
 */
function ToastAction({ className, ...props }: React.ComponentProps<typeof ToastPrimitive.Action>) {
  return (
    <ToastPrimitive.Action
      className={cn(
        "rounded-sm px-2 py-1 text-sm font-medium underline underline-offset-4",
        "outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
        className,
      )}
      {...props}
    />
  );
}

export {
  ToastProvider,
  ToastViewport,
  Toast,
  ToastTitle,
  ToastDescription,
  ToastAction,
};
