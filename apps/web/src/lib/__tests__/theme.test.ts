/**
 * Тесты темы (§17.4-17.7 артефакта дизайн-системы).
 *
 * **Главный тест — `apply()` перерисовывает подписчика (Б-8).** Это
 * единственный существенный дефект, найденный ревью: прошлая версия была
 * четырьмя чистыми функциями и называлась хуком. Атрибут на `<html>` менялся,
 * React-состояние нет, и `ThemeToggle` выглядел «залипшим».
 *
 * Второй по важности — тест, доказывающий, что перерисовку даёт именно
 * `notifyAll()` из `apply()`, а не что-то ещё. Он меняет `data-theme` напрямую
 * (как это сделал бы внешний скрипт) и утверждает, что React НЕ обновился.
 * Без него `notifyAll` можно было бы удалить, а тесты остались зелёными.
 */

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  apply,
  getStored,
  getSystem,
  subscribe,
  THEME_STORAGE_KEY,
  useTheme,
} from "@/lib/theme";
import { media, mediaQueryLists } from "@/test/setup";

describe("getStored", () => {
  it("возвращает null, если ключа нет", () => {
    expect(getStored()).toBeNull();
  });

  it.each(["light", "dark"] as const)("возвращает сохранённое значение %s", (theme) => {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
    expect(getStored()).toBe(theme);
  });

  it("игнорирует испорченное значение и не чинит его молча", () => {
    // §17.6: «Если значение испорчено — игнорируем, падаем на
    // prefers-color-scheme. Не чиним молча — просто не доверяем».
    localStorage.setItem(THEME_STORAGE_KEY, "purple");

    expect(getStored()).toBeNull();
    // Не перезаписали: чужое значение не наше дело.
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("purple");
  });

  it("не бросает, когда localStorage недоступен", () => {
    // §17.6: в sandboxed iframe и приватном режиме `getItem` бросает.
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });

    expect(() => getStored()).not.toThrow();
    expect(getStored()).toBeNull();

    spy.mockRestore();
  });
});

describe("getSystem", () => {
  it("возвращает dark при prefers-color-scheme: dark", () => {
    media.dark = true;
    expect(getSystem()).toBe("dark");
  });

  it("возвращает light при светлой системе", () => {
    media.dark = false;
    expect(getSystem()).toBe("light");
  });

  it("возвращает light, когда matchMedia недоступен", () => {
    // §17.5: `no-preference` и отсутствие API дают светлую.
    vi.stubGlobal("matchMedia", undefined);
    expect(getSystem()).toBe("light");
  });
});

describe("apply", () => {
  it("ставит data-theme на html", () => {
    apply("dark", false);
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("синхронизирует colorScheme для нативных контролов (§17.8)", () => {
    apply("dark", false);
    expect(document.documentElement.style.colorScheme).toBe("dark");
  });

  it("ставит theme-color из JS, а не через meta media (Н-10)", () => {
    apply("dark", false);

    const meta = document.querySelector('meta[name="theme-color"]:not([media])');
    expect(meta).not.toBeNull();
    expect(meta?.getAttribute("content")).toBe("#161513");
  });

  it("меняет theme-color на светлую при переключении", () => {
    apply("dark", false);
    apply("light", false);

    const meta = document.querySelector('meta[name="theme-color"]:not([media])');
    expect(meta?.getAttribute("content")).toBe("#F4F2EE");
  });

  it("не создаёт второй theme-color при повторном вызове", () => {
    apply("dark", false);
    apply("light", false);

    expect(document.querySelectorAll('meta[name="theme-color"]')).toHaveLength(1);
  });

  it("persist=true записывает выбор в localStorage", () => {
    apply("dark", true);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  it("persist=false НЕ записывает системную тему (§17.6)", () => {
    // Иначе «системная тёмная» превратилась бы в «пользователь выбрал тёмную»
    // и перестала бы следовать за системой.
    apply("dark", false);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it("не бросает, когда localStorage недоступен", () => {
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });

    expect(() => apply("dark", true)).not.toThrow();
    // Тема всё равно применилась: переключение работает в рамках сессии.
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");

    spy.mockRestore();
  });
});

describe("notifyAll через apply (Б-8) — главный тест волны 1", () => {
  it("apply() перерисовывает подписчика useSyncExternalStore", () => {
    const { result } = renderHook(() => useTheme());

    expect(result.current[0]).toBe("light");

    act(() => {
      apply("dark", true);
    });

    // Если убрать notifyAll() из apply(), эта проверка упадёт: атрибут
    // изменится, а React останется на 'light'. Ровно дефект Б-8.
    expect(result.current[0]).toBe("dark");
  });

  it("возвращённый из хука сеттер перерисовывает компонент", () => {
    const { result } = renderHook(() => useTheme());

    act(() => {
      result.current[1]("dark");
    });

    expect(result.current[0]).toBe("dark");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  it("прямая смена атрибута БЕЗ apply() не перерисовывает — источник правды notifyAll", () => {
    // Тест-страховка: он фиксирует, что перерисовку даёт именно notifyAll(),
    // а не наблюдение за DOM. Если кто-то заменит notifyAll на MutationObserver,
    // этот тест станет красным — и это правильно: решение было осознанным.
    const { result } = renderHook(() => useTheme());

    act(() => {
      document.documentElement.setAttribute("data-theme", "dark");
    });

    expect(result.current[0]).toBe("light");
  });

  it("несколько подписчиков получают уведомление", () => {
    const a = vi.fn();
    const b = vi.fn();
    const unsubA = subscribe(a);
    const unsubB = subscribe(b);

    apply("dark", false);

    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);

    unsubA();
    unsubB();
  });

  it("отписка прекращает уведомления", () => {
    const cb = vi.fn();
    const unsubscribe = subscribe(cb);

    apply("dark", false);
    expect(cb).toHaveBeenCalledTimes(1);

    unsubscribe();
    apply("light", false);
    expect(cb).toHaveBeenCalledTimes(1);
  });
});

describe("приоритет темы (§17.5)", () => {
  it("системная тёмная применяется, если выбора нет", () => {
    media.dark = true;

    const { result } = renderHook(() => useTheme());
    expect(result.current[0]).toBe("light");

    act(() => {
      // Событие ОС: пользователь не выбирал — следуем за системой.
      mediaQueryLists.forEach((mql) => mql.setMatches(true));
    });

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    // Системная тема НЕ сохраняется: иначе перестала бы следовать за ОС.
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it("явный выбор отменяет следование за системой", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "light");

    renderHook(() => useTheme());

    act(() => {
      mediaQueryLists.forEach((mql) => mql.setMatches(true));
    });

    // Выбор пользователя сильнее системной настройки.
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  });
});

