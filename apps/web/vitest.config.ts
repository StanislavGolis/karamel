import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import path from "node:path";

/**
 * Конфигурация Vitest (Chore P-5, решение эпика 6 п.6).
 *
 * **Зачем раннер.** До этой задачи фронт проверялся только `tsc` и `eslint`:
 * ни один UI-инвариант (скелетоны вместо спиннеров, отсутствие XSS, отмена
 * запроса при размонтировании) не мог покраснеть. Правило проекта
 * «то, что нельзя проверить, нельзя обещать» относилось и к фронту.
 *
 * **Почему отдельный `vitest.config.ts`, а не секция в `next.config.ts`.**
 * `next build` читает только свою конфигурацию; тестовая настройка в ней
 * либо игнорировалась бы, либо требовала `next/jest`-совместимого слоя.
 * Отдельный файл не участвует в сборке и не может её сломать.
 *
 * **Почему `jsdom`.** `ChatScreen` — клиентский компонент: `useState`,
 * `useEffect`, `fetch` и `AbortController`. В `node`-окружении этих API нет,
 * и тест проверял бы заглушки, а не поведение.
 *
 * **Почему `alias` повторяет `tsconfig.paths`.** Импорты вида `@/components/...`
 * разрешает сборщик Next; Vitest без явного alias не нашёл бы модуль и
 * упал бы на первом же тесте — при этом `tsc` остался бы зелёным.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // Тесты лежат рядом с кодом в `src/**/__tests__`: файл теста виден там,
    // где живёт компонент, и не теряется в отдельном дереве.
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/components/**/*.tsx", "src/lib/**/*.ts"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**"],
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
