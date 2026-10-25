"use client";

/**
 * Textarea — многострочное поле (§6.3, состояния как у Input + счётчик остатка).
 *
 * **Счётчик — часть примитива,** а не забота каждого экрана: §13.3 требует,
 * чтобы скринридер слышал «0 из 200». Разбросанный по формам счётчик рано или
 * поздно разойдётся с `maxLength`, поэтому он считается из того же пропа.
 *
 * **Счётчик работает и в controlled, и в uncontrolled режиме.** Первая версия
 * читала только `value`: с `defaultValue` он показывал «200 из 200» при уже
 * введённом тексте — то есть врал в самом частом способе использования
 * (форма создаётся с начальным значением и правится пользователем). Тест поймал.
 *
 * Счётчик включается только при заданном `maxLength`: поле без лимита не должно
 * показывать «0 из ∞».
 */

import * as React from "react";

import { cn } from "@/lib/utils";

export interface TextareaProps extends React.ComponentProps<"textarea"> {
  /** Показывать ли счётчик остатка. По умолчанию — да, если задан `maxLength`. */
  showCount?: boolean;
}

function Textarea({ className, maxLength, showCount, value, defaultValue, onChange, ...props }: TextareaProps) {
  const isControlled = value !== undefined;

  // Начальная длина: controlled берёт `value`, uncontrolled — `defaultValue`.
  // Без второй ветки счётчик в uncontrolled-режиме показывал бы пустоту.
  const [length, setLength] = React.useState(() => {
    if (isControlled && typeof value === "string") return value.length;
    if (typeof defaultValue === "string") return defaultValue.length;
    return 0;
  });

  // Controlled: длина всегда следует за пропом, даже если родитель изменил
  // значение программно (сброс формы, подстановка черновика).
  const current = isControlled && typeof value === "string" ? value.length : length;

  const handleChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    if (!isControlled) setLength(event.target.value.length);
    onChange?.(event);
  };

  const withCount = showCount ?? Boolean(maxLength);
  const remaining = maxLength ? maxLength - current : 0;

  return (
    <div className="flex flex-col gap-1">
      <textarea
        data-slot="textarea"
        maxLength={maxLength}
        value={value}
        defaultValue={defaultValue}
        onChange={handleChange}
        className={cn(
          "flex min-h-24 w-full resize-y rounded-md border border-border-input bg-surface-raised px-4 py-3",
          "text-base text-ink",
          "placeholder:text-ink-muted",
          "outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
          "transition-colors duration-fast ease-out",
          "disabled:cursor-not-allowed disabled:border-border-subtle disabled:bg-disabled-bg disabled:text-disabled-ink",
          "read-only:border-border-subtle read-only:bg-surface read-only:text-ink-secondary",
          "aria-invalid:border-danger aria-invalid:focus-visible:outline-danger",
          className,
        )}
        {...props}
      />
      {withCount && maxLength ? (
        <span
          // §3.3: числа с tabular-nums — иначе счётчик «дёргается» при наборе.
          className="self-end text-xs text-ink-muted tabular-nums"
          // Не `aria-live`: счётчик меняется на каждое нажатие и забубнит ввод.
          // Значение читается при переходе в поле через `aria-describedby` формы.
          aria-hidden="true"
        >
          {`${remaining} из ${maxLength}`}
        </span>
      ) : null}
    </div>
  );
}

export { Textarea };
