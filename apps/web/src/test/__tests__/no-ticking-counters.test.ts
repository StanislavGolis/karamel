/**
 * Замок AC-4 стори 12.5: тикающих счётчиков в интерфейсе нет (З-9, UX §575).
 *
 * **Почему статический тест, а не тест компонента.** Тест `DeadlineNotice`
 * доказывает, что таймеров нет **в нём** (`vi.getTimerCount() === 0`). Но З-9
 * запрещает обратный отсчёт во всём интерфейсе, и следующий компонент мог бы
 * вернуть `setInterval(..., 1_000)` — ни один существующий тест этого не заметил бы.
 *
 * **Почему каталог `src`, а не `components/cards` (CR 12.5, H-2).** AC-4 звучит
 * как «их нет **ни в одном компоненте**», а первая редакция гарда смотрела только
 * в `components/cards/**`. Доказано мутацией: компонент с
 * `setInterval(() => setNow(...), 1_000)` и `role="timer"` в `components/chat/**`
 * оставлял гард ЗЕЛЁНЫМ — то есть AC-4 не был защищён там, где правило и
 * действует. Таймер в чате — тот же запрет З-9, что и таймер в карточке.
 *
 * **Почему именно `1_000` (и его варианты), а не любой `setInterval`.**
 * `setInterval` сам по себе не запрещён: запрещён **счётчик времени**. Интервал
 * в 1000 мс — единственная причина, по которой такой код существует. Прочие
 * интервалы (опрос, ретрай) правилом З-9 не запрещены: `setTimeout` на 5000 мс
 * для отмены запроса — не тикающий счётчик.
 *
 * **Что делает тест красным:** возврат `setInterval(() => setNow(...), 1_000)`,
 * `setTimeout(..., 1000)` в цикле перерисовки, `role="timer"` в любой разметке.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

// apps/web/src/test/__tests__ → apps/web/src
const SRC = resolve(import.meta.dirname, "../..");

/**
 * Все исходники интерфейса, кроме тестов и тестовой обвязки.
 *
 * `__tests__`, `*.test.ts(x)` и `src/test` исключены намеренно: в тестовом коде
 * `setInterval(..., 1_000)` — законный способ проверить поведение, и запрет
 * З-9 к нему не относится. Ложное исключение проверяется порогом ниже.
 */
function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "__tests__" || entry === "node_modules") continue;
      if (full === join(SRC, "test")) continue;
      out.push(...sourceFiles(full));
      continue;
    }
    if (!/\.tsx?$/.test(entry)) continue;
    if (/\.test\.tsx?$/.test(entry)) continue;
    out.push(full);
  }
  return out;
}

/**
 * Убирает комментарии — иначе объяснение «почему НЕЛЬЗЯ `setInterval(..., 1_000)`»
 * в docstring компонента само сделало бы тест красным.
 *
 * Грубая, но достаточная для `.ts`/`.tsx` реализация: строчные `//` и блочные
 * `/* … *\/`. Строковые литералы, содержащие `//`, в этих файлах не встречаются
 * без нужды, а ложное срабатывание видно сразу (тест краснеет с указанием файла).
 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

describe("AC-4: тикающих счётчиков в интерфейсе нет (З-9)", () => {
  it("во всём src нет интервала в одну секунду", () => {
    const files = sourceFiles(SRC);
    // Замер CR 12.5 (2026-10-29): `src` без тестов — 100+ исходников.
    // Порог держит десятую часть: пустой/переименованный `src` сделал бы гард
    // зелёным на пустоте (урок 11.10: тест, не исполняющий код, проверяет текст).
    expect(files.length).toBeGreaterThanOrEqual(10);

    const offenders = files.filter((file) => {
      const code = stripComments(readFileSync(file, "utf8"));
      return /set(Interval|Timeout)\s*\([\s\S]{0,120}?[\s,(]1_?000\s*[,)]/.test(code);
    });

    expect(offenders).toEqual([]);
  });

  it('во всём src нет role="timer"', () => {
    const offenders = sourceFiles(SRC).filter((file) => {
      const code = stripComments(readFileSync(file, "utf8"));
      return /role\s*=\s*["']timer["']/.test(code);
    });

    // Мутация «добавить role="timer"» в любой компонент — тест краснеет.
    expect(offenders).toEqual([]);
  });
});
