/**
 * Общая настройка тестов (Chore P-5).
 *
 * `@testing-library/jest-dom` добавляет матчеры, которые проверяют
 * ДОСТУПНОСТЬ, а не разметку: `toBeInTheDocument`, `toHaveAttribute`,
 * `toBeVisible`. Это важно для проекта: UX-правила требуют текстовых
 * статусов (NFR57) и `aria-busy` на скелетонах, и тест должен уметь
 * утверждать именно это, а не искать класс `animate-pulse` строкой.
 *
 * `cleanup` подключён явно: Vitest не вызывает его автоматически (в отличие
 * от Jest с `@testing-library/react`), и без него DOM первого теста
 * оставался бы во втором — утверждение «скелетонов нет» проходило бы
 * по неверной причине.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

/**
 * `matchMedia` в jsdom отсутствует.
 *
 * Без него тема не тестируется вообще: `lib/theme.ts` строит на нём весь
 * приоритет «явный выбор > системная настройка > светлая» (§17.5). Заглушка
 * обязана быть управляемой: тест переключает системную тему и проверяет, что
 * приложение за ней следует ТОЛЬКО если выбора нет.
 *
 * Реализация минимальна, но честна: `addEventListener`/`removeEventListener`
 * через `EventTarget` и `dispatchEvent` — ровно тот контракт, который
 * использует код. Мок без реального `addEventListener` превратил бы проверку
 * подписки в фикцию.
 */
export class FakeMediaQueryList extends EventTarget {
  matches: boolean;
  media: string;
  onchange: ((e: MediaQueryListEvent) => void) | null = null;

  constructor(media: string, matches: boolean) {
    super();
    this.media = media;
    this.matches = matches;
  }

  /** Смена системной темы в тесте: подписчик получит событие `change`. */
  setMatches(matches: boolean): void {
    this.matches = matches;
    this.dispatchEvent(new Event("change"));
  }
}

/** Все созданные matchMedia-объекты: тест меняет системную тему. */
export const mediaQueryLists: FakeMediaQueryList[] = [];

/**
 * Системная тема для следующего `matchMedia`. По умолчанию светлая.
 *
 * Нужна как переменная, а не аргумент `beforeEach`: `lib/theme.ts` вызывает
 * `window.matchMedia` в момент подписки, и тест обязан задать настройку ДО
 * рендера компонента.
 */
export const media = { dark: false };

/**
 * Есть ли DOM. `ws-proxy.test.ts` и `bff-proxy.test.ts` работают в
 * `node`-окружении (`environment` в `vitest.config.ts` — глобальный `jsdom`,
 * но эти файлы помечены `// @vitest-environment node`). В `node` нет ни
 * `window`, ни `document`, и обращение к ним роняет тест, который к теме
 * отношения не имеет.
 *
 * Проверка нужна именно здесь, а не в самих тестах: `setupFiles` выполняется
 * для КАЖДОГО файла, и общая настройка не имеет права предполагать браузер.
 */
const hasDom = typeof window !== "undefined" && typeof document !== "undefined";

beforeEach(() => {
  mediaQueryLists.length = 0;
  media.dark = false;

  if (!hasDom) return;

  // `localStorage` между тестами: тема пишется в него и обязана не протекать
  // из теста в тест, иначе «первый визит» проверить нельзя.
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.style.colorScheme = "";
  document.querySelector('meta[name="theme-color"]')?.remove();

  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => {
      const mql = new FakeMediaQueryList(query, media.dark);
      mediaQueryLists.push(mql);
      return mql;
    }),
  );
});

afterEach(() => {
  if (hasDom) cleanup();
  vi.unstubAllGlobals();
});
