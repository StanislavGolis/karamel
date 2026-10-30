"use client";

/**
 * Переключатель темы — `radiogroup` из двух опций (§17.9).
 *
 * **Почему `radio`, а не `switch`.** `switch` — бинарное состояние
 * «включено/выключено» (звук, уведомления). Тема — выбор из двух вариантов,
 * а не включение функции. Скринридер при `switch` скажет «тёмная тема,
 * переключатель, выключено» — то есть подразумевает, что тема одна и её
 * можно выключить. При `radio` — «тёмная тема, переключатель, 2 из 2».
 *
 * **Почему в настройках профиля, а не в шапке.** Шапка занята городом и
 * фильтрами; тема — редкое действие (один раз). Иконка-солнце в шапке —
 * визуальный шум и «настройка ради настройки».
 *
 * **Почему третий вариант «как в системе» не показан.** Он существует только
 * как дефолт (§17.5). Иначе человек, выбравший «как в системе», не понимал
 * бы, почему вечером всё стало тёмным.
 *
 * **Layout shift — ноль.** Опции фиксированной ширины, иконка 16px в
 * фиксированном слоте, текст не меняется: переключение меняет только цвета.
 */

import { useRef } from "react";

import { type Theme, useTheme } from "@/lib/theme";

const OPTIONS: ReadonlyArray<{ value: Theme; label: string }> = [
  { value: "light", label: "Светлая" },
  { value: "dark", label: "Тёмная" },
];

export function ThemeToggle() {
  const [theme, setTheme] = useTheme();
  // Refs на кнопки: стрелке нужно и перевести фокус, и применить выбор.
  const refs = useRef<Record<Theme, HTMLButtonElement | null>>({
    light: null,
    dark: null,
  });

  const currentIndex = OPTIONS.findIndex((o) => o.value === theme);

  /**
   * Стрелки переключают опции и применяют выбор сразу (§17.9).
   *
   * Почему не полагаться на браузер: `role="radiogroup"` без нативных
   * `input[type=radio]` не даёт клавиатурного поведения бесплатно —
   * WAI-ARIA Authoring Practices требует реализовать его явно.
   * `Tab` входит на выбранную опцию (roving tabindex), стрелки ходят по кругу.
   */
  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();

    const step = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
    const next = OPTIONS[(currentIndex + step + OPTIONS.length) % OPTIONS.length];
    setTheme(next.value);
    refs.current[next.value]?.focus();
  }

  return (
    <div className="flex flex-col gap-2">
      {/* Видимая метка группы. `aria-label` не нужен: у нас есть текст, и
          дублировать его именем региона значило бы читать вслух дважды. */}
      <span id="theme-label" className="text-sm font-medium text-ink-secondary">
        Тема
      </span>
      <div
        role="radiogroup"
        aria-labelledby="theme-label"
        onKeyDown={onKeyDown}
        className="flex gap-2"
      >
        {OPTIONS.map(({ value, label }) => {
          const selected = value === theme;
          return (
            <button
              key={value}
              ref={(el) => {
                refs.current[value] = el;
              }}
              type="button"
              role="radio"
              aria-checked={selected}
              // Roving tabindex: Tab входит в группу на выбранной опции (§17.9).
              tabIndex={selected ? 0 : -1}
              onClick={() => setTheme(value)}
              className={[
                // Тап-зона ≥44px — на уровне компонента (§17.9, §4.6).
                "flex min-h-11 min-w-11 items-center justify-center gap-2 rounded-md border px-4 text-sm font-medium",
                "transition-colors duration-fast ease-out",
                // focus-visible: 2px focus-ring + offset 2px (§17.9).
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
                selected
                  ? "border-accent bg-accent text-accent-ink"
                  : "border-border-input bg-surface-raised text-ink hover:bg-surface-hover",
              ].join(" ")}
            >
              {/* Иконка + текст обязательны (NFR57): статус не только цветом
                  и не только иконкой. Слот иконки фиксирован — layout shift ноль. */}
              <span aria-hidden="true" className="inline-flex h-4 w-4 items-center justify-center">
                {value === "light" ? <SunIcon /> : <MoonIcon />}
              </span>
              {label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** Иконка «светлая». `aria-hidden`: текст рядом уже называет опцию. */
function SunIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="3" />
      <path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3.05 3.05l1.06 1.06M11.89 11.89l1.06 1.06M12.95 3.05l-1.06 1.06M4.11 11.89l-1.06 1.06" />
    </svg>
  );
}

/** Иконка «тёмная». */
function MoonIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M13.5 9.5A6 6 0 0 1 6.5 2.5a6 6 0 1 0 7 7Z" />
    </svg>
  );
}
