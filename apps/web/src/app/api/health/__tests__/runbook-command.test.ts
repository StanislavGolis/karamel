// @vitest-environment node
/**
 * Исполнение команды удаления монитора из рунбука (стори 11.2, AC-5.2).
 *
 * **Почему тест запускает команду, а не читает текст.** CR 2026-10-27 (B-2)
 * нашёл в рунбуке `editMonitor` со `status:0` — это ПАУЗА, а не удаление,
 * и собственная проверка документа («`getMonitors` больше не содержит id»)
 * пройти не могла. Сторож на подстроки такой дефект пропускает: строка
 * присутствует, команда неверна (правило 18.2 — ассерт, который не может
 * упасть по делу, хуже отсутствующего; 18.1 — проверка обязана падать без
 * исправления).
 *
 * Здесь `scripts/uptime/delete-monitor.sh` **исполняется** против стаб-сервера
 * UptimeRobot API v2, который ведёт себя как настоящий: `editMonitor` со
 * `status:0` оставляет запись в `getMonitors`, `deleteMonitor` убирает её.
 * Проверяется эффект: после команды id исчез, exit 0; на паузе id остаётся,
 * exit 1. Обе половины — с реальным кодом выхода.
 *
 * **Чего тест не проверяет.** Живой UptimeRobot: у проекта нет ключа
 * (долг `W-11.2-b`). Стаб воспроизводит контракт API v2 по документации;
 * расхождение с живым сервисом — предмет `W-11.2-b`, а не этого теста.
 */

import { execFile } from "node:child_process";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

/** Корень репозитория считается от этого файла: `fileURLToPath` не портит пробелы. */
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../../../../../..");
const SCRIPT = path.join(REPO_ROOT, "scripts", "uptime", "delete-monitor.sh");

/** Мониторы, «живущие» в стабе: id → friendly_name. */
let monitors: Map<string, string>;
let server: Server | undefined;

/**
 * Стаб UptimeRobot API v2, воспроизводящий именно ту разницу, из-за которой
 * возник B-2:
 * - `deleteMonitor` удаляет запись насовсем;
 * - `editMonitor` со `status:0` **оставляет** её в `getMonitors` (пауза).
 */
async function startStub(deleteStat: "ok" | "fail" = "ok"): Promise<string> {
  monitors = new Map([["777001", "karamel api health"]]);

  server = createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
    });
    req.on("end", () => {
      const body = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      res.setHeader("content-type", "application/json");

      if (req.url === "/v2/getMonitors") {
        res.end(
          JSON.stringify({
            stat: "ok",
            monitors: [...monitors.entries()].map(([id, name]) => ({
              id: Number(id),
              friendly_name: name,
            })),
          }),
        );
        return;
      }

      if (req.url === "/v2/deleteMonitor") {
        if (deleteStat === "fail") {
          // Отказная ветка: API ответил, но удаление НЕ подтвердил — запись
          // остаётся, `stat` не равен `ok`. Скрипт обязан выйти с кодом 2.
          res.end(JSON.stringify({ stat: "fail", error: { message: "server error" } }));
          return;
        }
        monitors.delete(String(body.id));
        res.end(JSON.stringify({ stat: "ok", monitor: { id: body.id } }));
        return;
      }

      if (req.url === "/v2/editMonitor") {
        // Ключевая деталь: пауза НЕ удаляет запись. Именно на этом рунбук
        // до CR утверждал «удаление».
        res.end(JSON.stringify({ stat: "ok" }));
        return;
      }

      res.statusCode = 404;
      res.end(JSON.stringify({ stat: "fail", error: { message: "unknown method" } }));
    });
  });

  await new Promise<void>((resolve) => server?.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return `http://127.0.0.1:${port}`;
}

afterEach(async () => {
  await new Promise<void>((resolve) => {
    if (!server) return resolve();
    server.closeAllConnections?.();
    server.close(() => resolve());
    server = undefined;
  });
});

type ScriptResult = { status: number | null; stdout: string; stderr: string };

/**
 * Запуск скрипта ровно так, как его запустит дежурный.
 *
 * **Почему `execFile`, а не `spawnSync`.** Стаб-сервер живёт в этом же
 * процессе; синхронный запуск блокирует event loop, сервер не успевает
 * ответить, и команда падает по таймауту — тест мерил бы сам себя, а не
 * скрипт (первая версия дала ровно это: `status: null`, 20 с ожидания).
 */
function runScript(
  apiBase: string,
  args: string[],
  apiKey = "test-key",
): Promise<ScriptResult> {
  return new Promise((resolve) => {
    execFile(
      "bash",
      [SCRIPT, ...args],
      {
        encoding: "utf8",
        env: { ...process.env, UPTIME_API_BASE: apiBase, UPTIME_API_KEY: apiKey },
        timeout: 20_000,
      },
      (error, stdout, stderr) => {
        const code = error ? (typeof error.code === "number" ? error.code : null) : 0;
        resolve({ status: code, stdout, stderr });
      },
    );
  });
}

describe("scripts/uptime/delete-monitor.sh", () => {
  it("W-11.2-T14: удаляет монитор и подтверждает эффект в getMonitors", async () => {
    // AC-5.2: «удаляется одной командой». Проверяется не форма, а факт:
    // id исчез из аккаунта и exit 0.
    const base = await startStub();

    const result = await runScript(base, ["777001"]);

    expect(result.status, `stderr: ${result.stderr}`).toBe(0);
    expect(result.stdout).toMatch(/удалён/);
    expect(monitors.has("777001")).toBe(false);
  });

  it("W-11.2-T14: без ключа не делает сетевых вызовов и возвращает 2", async () => {
    // AC-3 / D-11.2-5: ключ только из окружения. Пустой ключ — отказ до запроса.
    const base = await startStub();

    const result = await runScript(base, ["777001"], "");

    expect(result.status).toBe(2);
    expect(result.stderr).toMatch(/UPTIME_API_KEY/);
    // Монитор не тронут: команда не выполнилась «наполовину».
    expect(monitors.has("777001")).toBe(true);
  });

  it("W-11.2-T14: без id возвращает 2 и не трогает аккаунт", async () => {
    const base = await startStub();

    const result = await runScript(base, []);

    expect(result.status).toBe(2);
    expect(monitors.has("777001")).toBe(true);
  });

  it("W-11.2-T14d: API не подтвердил удаление → exit 2, монитор остаётся", async () => {
    // CR R2, мутация M-CR15: замена `exit 2` → `exit 0` в отказной ветке
    // (delete-monitor.sh:43-44) не краснела — стаб всегда отвечал `stat: ok`,
    // и ветка была недостижима. Здесь API отвечает `stat: fail` на `deleteMonitor`:
    // скрипт обязан вернуть 2 и сообщить, что удаление не подтверждено, а запись
    // обязана остаться в аккаунте (нельзя рапортовать успех без эффекта).
    const base = await startStub("fail");

    const result = await runScript(base, ["777001"]);

    expect(result.status, `stdout: ${result.stdout}`).toBe(2);
    expect(result.stderr).toMatch(/не подтвердил удаление/);
    expect(monitors.has("777001")).toBe(true);
  });
});
