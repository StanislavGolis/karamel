/**
 * Skeleton — заглушка загрузки (§6.3).
 *
 * Три правила, каждое из которых легко нарушить:
 *
 * 1. **Повторяет финальные габариты** (§6.5 п.5, §8.4 п.1). Иначе CLS и провал
 *    NFR8: карточка 176px не может грузиться заглушкой 80px.
 * 2. **`aria-hidden`** (§13.3). Скелетон — визуальная заглушка; озвучивать
 *    серию пустых прямоугольников бессмысленно. Состояние объявляет контейнер
 *    через `aria-busy="true"`.
 * 3. **`prefers-reduced-motion` гасит полностью** (§5.2): пульсация — движение,
 *    и при reduced-motion она отключается целиком, а не «уменьшается».
 *
 * Цвет — `surface` (не `surface-raised`): заглушка лежит НА плоскости, а не над ней.
 */

import * as React from "react";

import { cn } from "@/lib/utils";

export type SkeletonKind = "text" | "card" | "row";

const KIND_CLASS: Record<SkeletonKind, string> = {
  // Строка текста: высота базовой линии, ширина — по родителю.
  text: "h-4 w-full",
  // Карточка: минимум 176px в `app` (§4.6).
  card: "h-44 w-full",
  // Строка списка: ≥56px в `app`, ≥36 в `mod` (§4.4).
  row: "h-14 w-full",
};

function Skeleton({
  className,
  kind = "text",
  ...props
}: React.ComponentProps<"div"> & { kind?: SkeletonKind }) {
  return (
    <div
      data-slot="skeleton"
      data-kind={kind}
      // §13.3: озвучивать заглушку не нужно, состояние несёт контейнер.
      aria-hidden="true"
      className={cn(
        "rounded-md bg-surface",
        // `motion-safe:` — пульсация только там, где движение разрешено (§5.2).
        // При reduced-motion остаётся статичная заливка, а не исчезает блок.
        "motion-safe:animate-pulse",
        KIND_CLASS[kind],
        className,
      )}
      {...props}
    />
  );
}

export { Skeleton };
