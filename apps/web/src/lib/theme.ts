/**
 * Тема: `light` | `dark`, один атрибут на `<html>`, компоненты о теме не знают.
 *
 * Реализация по §17.4–17.7 артефакта дизайн-системы v2.3.0. Ключевые решения:
 *
 * 1. **`data-theme` на `<html>`, а не `dark:`-классы.** Иначе каждая фича — это
 *    два набора классов и два места для ошибки. При переопределении переменных
 *    компонент один, тема меняет только значения.
 * 2. **`useSyncExternalStore`, а не `useState`.** Источник правды — DOM-атрибут,
 *    который ставит inline-скрипт до гидратации. `useState` разошёлся бы с ним
 *    и потребовал синхронизации в эффекте (и дал бы FOUC).
 * 3. **Реестр подписчиков `subscribers` обязателен (Б-8).** Событие `storage`
 *    в ТОМ ЖЕ окне по спецификации HTML не срабатывает. Без `notifyAll()`
 *    клик по `ThemeToggle` менял бы атрибут, но React не перерисовывался бы —
 *    переключатель выглядел бы «залипшим».
 * 4. **`next-themes` не ставим** (§17.7): он приносит модель из трёх состояний
 *    (`light`/`dark`/`system`), которой у нас нет по §17.5 — система является
 *    дефолтом, а не выбором пользователя.
 */

import { useSyncExternalStore } from "react";

export type Theme = "light" | "dark";

const KEY = "karamel-theme";
const MEDIA = "(prefers-color-scheme: dark)";

/**
 * Реестр подписчиков. Без него переключение НЕ перерисовывает React.
 *
 * Почему модульный `Set`, а не `EventTarget`: `useSyncExternalStore` ждёт
 * функцию `subscribe(cb)`, и реестр — самый короткий честный вариант. Плюс
 * он переживает несколько смонтированных компонентов: подписчиков может быть
 * больше одного (например, `ThemeToggle` и любой будущий потребитель).
 */
const subscribers = new Set<() => void>();

/** Уведомить всех. Вызывается из `apply()` — единственного места смены темы. */
function notifyAll(): void {
  for (const cb of subscribers) cb();
}

// --- внешнее хранилище: сам DOM-атрибут ---

/**
 * Подписка для `useSyncExternalStore`.
 *
 * Три источника изменений:
 * - системная тема (`matchMedia`) — применяется только если выбора нет (§17.5);
 * - `storage` из ДРУГОЙ вкладки (в текущей не срабатывает по спецификации);
 * - прямой вызов `notifyAll()` из `apply()` — покрывает текущую вкладку.
 */
export function subscribe(cb: () => void): () => void {
  subscribers.add(cb);

  const mq = window.matchMedia(MEDIA);

  // Системная тема применяется только если пользователь не выбирал.
  // Иначе «переключили ОС — переключилось приложение» отменяло бы явный выбор.
  const onSystem = () => {
    if (getStored() === null) apply(getSystem(), false);
    else notifyAll();
  };

  // Синхронизация между вкладками. `apply(..., false)` — не записывать:
  // значение уже в localStorage другой вкладки, запись только разбудила бы
  // лишние события.
  const onStorage = (e: StorageEvent) => {
    if (e.key !== KEY) return;
    apply(e.newValue === "light" || e.newValue === "dark" ? e.newValue : getSystem(), false);
  };

  mq.addEventListener("change", onSystem);
  window.addEventListener("storage", onStorage);

  return () => {
    subscribers.delete(cb);
    mq.removeEventListener("change", onSystem);
    window.removeEventListener("storage", onStorage);
  };
}

/**
 * Снимок для React: читает уже готовый атрибут.
 *
 * Возвращает `'light'` для любого значения, кроме `'dark'`: сервер отдал
 * разметку без атрибута, а «неизвестно» в типе `Theme` выразить нельзя.
 */
function getSnapshot(): Theme {
  const v = document.documentElement.getAttribute("data-theme");
  return v === "dark" ? "dark" : "light";
}

/**
 * Серверный снимок: тема неизвестна, отдаём светлую.
 *
 * Расхождения гидратации не будет: атрибут ставит inline-скрипт в `<head>`
 * ДО гидратации, а `suppressHydrationWarning` на `<html>` уже включён (§17.7).
 */
function getServerSnapshot(): Theme {
  return "light";
}

/**
 * Хук темы: возвращает текущую тему и функцию переключения.
 *
 * Перерисовывает компонент при смене темы — это главное отличие от
 * «четырёх чистых функций», из-за которого `ThemeToggle` не обновлялся (Б-8).
 */
export function useTheme(): [Theme, (t: Theme) => void] {
  const theme = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  return [theme, (t: Theme) => apply(t, true)];
}

// --- императивные помощники ---

/**
 * Сохранённый выбор пользователя или `null`.
 *
 * `null` — не только «ключа нет», но и «значение испорчено» (§17.6): не
 * доверяем мусору и падаем на системную настройку. `try/catch` обязателен:
 * `localStorage.getItem` бросает в sandboxed iframe и приватном режиме.
 */
export function getStored(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

/** Системная тема. `no-preference` и отсутствие `matchMedia` дают светлую (§17.5). */
export function getSystem(): Theme {
  try {
    return window.matchMedia(MEDIA).matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

/**
 * Применить тему. Единственное место, где меняется атрибут и `theme-color`.
 *
 * `persist = true` — явный выбор пользователя (клик в `ThemeToggle`):
 * записываем в `localStorage`. `persist = false` — системная тема или
 * синхронизация вкладки: НЕ записываем, иначе «системная тёмная» превратилась
 * бы в «пользователь выбрал тёмную» и перестала следовать за системой (§17.6).
 */
export function apply(theme: Theme, persist: boolean): void {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme);
  // Дублирует CSS `color-scheme`, но нужен для случая, когда тема пришла из
  // localStorage до применения стилей: нативные контролы должны быть тёмными
  // с первого кадра (§17.8).
  root.style.colorScheme = theme;

  // Н-10: цвет адресной строки. `<meta media="(prefers-color-scheme: dark)">`
  // здесь не работает — он игнорирует явный выбор пользователя. Поэтому JS.
  let meta = document.querySelector('meta[name="theme-color"]:not([media])') as HTMLMetaElement | null;
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.appendChild(meta);
  }
  meta.content = theme === "dark" ? "#161513" : "#F4F2EE";

  if (persist) {
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      // Молча: недоступное хранилище — не ошибка пользователя (§17.6).
      // Переключение продолжает работать в рамках сессии.
    }
  }

  // КРИТИЧНО (Б-8): без этой строки `useSyncExternalStore` не узнает об
  // изменении. Событие `storage` в том же окне по спецификации HTML не
  // срабатывает, поэтому перерисовку обеспечивает прямой вызов.
  notifyAll();
}

/** Экспорт ключа для тестов и для inline-скрипта (единый источник имени). */
export const THEME_STORAGE_KEY = KEY;
