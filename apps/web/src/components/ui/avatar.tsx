"use client";

/**
 * Avatar — справка о человеке (§6.3, §17.11).
 *
 * **Фото не инвертируется, не затемняется и не «улучшается»** (§17.11).
 * Сгенерированный shadcn-файл накладывал `after:mix-blend-darken` /
 * `dark:after:mix-blend-lighten` — это ровно то, что спека запрещает:
 * «лица становятся мертвецки-синими», а затемнение — оценка внешности.
 *
 * **Граница зависит от темы, а не от класса `dark:`.** На тёмном фоне
 * светлое фото «плавает» без края, поэтому там `border-input` (3.67:1),
 * на светлом достаточно декоративной `border` (§17.11). Это делает CSS:
 * цвет меняет токен, компонент остаётся один (§17.4).
 *
 * **Инициал — `ink` на `bg`, без цвета по имени** (§2.4): цвет по имени —
 * скрытый рейтинг.
 *
 * Битая картинка: `AvatarFallback` подставляет инициал; подложка `surface`
 * одинакова в обеих темах (§17.11).
 */

import * as React from "react";
import { Avatar as AvatarPrimitive } from "radix-ui";

import { cn } from "@/lib/utils";

export type AvatarSize = "sm" | "md" | "lg";

/** 40px в `app` (§4.6: автор вторичен, фото не доминирует), 32 и 24 — мельче. */
const SIZE_CLASS: Record<AvatarSize, string> = {
  sm: "size-6",
  md: "size-8",
  lg: "size-10",
};

function Avatar({
  className,
  size = "md",
  ...props
}: React.ComponentProps<typeof AvatarPrimitive.Root> & { size?: AvatarSize }) {
  return (
    <AvatarPrimitive.Root
      data-slot="avatar"
      data-size={size}
      className={cn(
        "relative flex shrink-0 select-none overflow-hidden rounded-full",
        SIZE_CLASS[size],
        // Граница аватара. На тёмном фоне светлое фото «плавает» без края,
        // поэтому там нужен `border-input` (3.67:1 к surface). Вариант задан
        // правилом в globals.css по `[data-theme="dark"]`, а НЕ классом `dark:`:
        // §17.4 — тема меняет значения переменных, а не наборы классов.
        // Первая версия содержала `dark:border-border-input` и нарушала §17.4;
        // поймано тестом после починки сломанного regex в FORBIDDEN.
        "border border-border",
        className,
      )}
      {...props}
    />
  );
}

function AvatarImage({ className, ...props }: React.ComponentProps<typeof AvatarPrimitive.Image>) {
  return (
    <AvatarPrimitive.Image
      data-slot="avatar-image"
      className={cn("aspect-square size-full rounded-full object-cover", className)}
      {...props}
    />
  );
}

function AvatarFallback({ className, ...props }: React.ComponentProps<typeof AvatarPrimitive.Fallback>) {
  return (
    <AvatarPrimitive.Fallback
      data-slot="avatar-fallback"
      className={cn(
        // Подложка и инициал одинаковы в обеих темах (§17.11).
        "flex size-full items-center justify-center bg-surface text-ink",
        "text-sm font-medium",
        className,
      )}
      {...props}
    />
  );
}

export { Avatar, AvatarImage, AvatarFallback };
