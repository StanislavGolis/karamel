// @vitest-environment node
/**
 * Сторож документа `docs/runbook-uptime.md` (стори 11.2, AC-5.1, AC-6.1…AC-6.3).
 *
 * **Зачем тест на текст.** AC-5 и AC-6 — утверждения, а не код: «оговорка к
 * NFR65 содержит четыре условия», «источник — журнал UptimeRobot», «фраза
 * ”внешний монитор не внедряется“ больше не встречается». Единственный способ
 * сделать их проверяемыми — сторож на содержимое документа.
 *
 * **Почему проверяются маркеры, а не целые цитаты.** Тест на всю цитату
 * краснеет от любой правки запятой, и его отключат. Здесь фиксируются
 * смысловые маркеры: четыре условия исключения, приоритет источника,
 * запрет ключа в репозитории и отсутствие устаревшего утверждения.
 *
 * **Чего тест не проверяет.** Что монитор реально настроен и что алерт дошёл:
 * это внешний сервис (Task 3 и Task 5 стори), проверяется руками владельца.
 */

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * Путь от этого файла до репозитория.
 *
 * `__dirname` в ESM-тесте недоступен. `new URL(".", import.meta.url)
 * .pathname` оставлял бы percent-encoding: на пути с пробелом (`%20`)
 * `readFileSync` падал бы как «документ не найден», то есть тест врал бы
 * о причине (CR-находка N-5). `fileURLToPath` декодирует путь.
 */
const RUNBOOK_PATH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../../../../../docs/runbook-uptime.md",
);

function runbook(): string {
  return readFileSync(RUNBOOK_PATH, "utf8");
}

describe("docs/runbook-uptime.md", () => {
  it("W-11.2-T6: содержит все четыре условия исключения из NFR65", () => {
    // AC-5.1: оговорка к NFR65 — не «монитор разрешён», а четыре условия.
    // Вернуть старую шапку без условий — тест краснеет.
    const text = runbook().toLowerCase();

    // 1. только публичный GET /health
    expect(text).toContain("/api/health");
    // 2. без персональных данных и токенов
    // `\w` в JS не покрывает кириллицу — явный класс, иначе тест «проходит»
    // мимо смысла и краснеет на корректном тексте.
    expect(text).toMatch(/персональн[а-яё]* данных|токен/);
    // 3. не пишет в нашу БД
    expect(text).toMatch(/не (входит|пишет)[^.]{0,80}бд|не пишет в нашу бд/);
    // 4. удаляется одной командой
    expect(text).toContain("одной командой");
  });

  it("W-11.2-T6: источник uptime — UptimeRobot, а probe понижен до второго", () => {
    // AC-6.1/AC-6.2: журнал монитора — первый источник, журнал probe — второй.
    // Поменять порядок обратно (probe первым) — тест краснеет.
    const text = runbook();
    const uptimeRobotIndex = text.indexOf("Журнал UptimeRobot");
    const probeIndex = text.indexOf("Журнал алертов probe");

    expect(uptimeRobotIndex).toBeGreaterThan(-1);
    expect(probeIndex).toBeGreaterThan(-1);
    expect(uptimeRobotIndex).toBeLessThan(probeIndex);
    // Поле отчёта: значение именно UptimeRobot, а не «журнал алертов».
    expect(text).toMatch(/Источник\(и\) данных[^\n]*UptimeRobot/);
  });

  it("W-11.2-T6: содержит процедуру месячной выгрузки, а не ”выгрузить как-нибудь“", () => {
    // AC-6.4: закрывается процедурой и шаблоном (D-11.2-6), поэтому процедура
    // обязана называть путь в панели и период. Убрать шаги — тест краснеет.
    const text = runbook();

    expect(text).toMatch(/Monitors/);
    expect(text).toMatch(/Stats|Log/);
    expect(text).toMatch(/Custom range|период/i);
    expect(text).toMatch(/Uptime ratio|% uptime/i);
  });

  it("W-11.2-T6: утверждение ”внешний монитор не внедряется“ удалено", () => {
    // AC-6.3: фраза стала ложью 2026-10-27. Вернуть её в §5 — тест краснеет.
    const text = runbook().toLowerCase();

    expect(text).not.toContain("внешний монитор не внедряется");
    expect(text).not.toContain("nfr65 прямо запрещает apm в mvp");
    expect(text).not.toContain("до его появления ac-04 закрыт частично");
  });

  it("W-11.2-T6: в рунбуке нет ключа API UptimeRobot", () => {
    // AC-5.2 / D-11.2-5: удаление описано командой, но сам ключ в git не
    // попадает — только имя переменной окружения. Ключи UptimeRobot имеют вид
    // `u` + 6 цифр + `-` + 32 hex; ловим оба распространённых формата.
    const text = runbook();

    expect(text).toContain("UPTIME_API_KEY");
    // N-5д: прежний гард требовал ровно `u` + 6 цифр + дефис + 32 hex и
    // пропускал любой другой формат ключа. Теперь ловится сам вид секрета:
    // длинная hex/буквенно-цифровая последовательность после `u`/`ur` и
    // любая строка из 32+ hex-символов подряд.
    expect(text).not.toMatch(/\bu\d{6}-[0-9a-f]{32}\b/i);
    expect(text).not.toMatch(/\bur\d{6,}[0-9a-z]{6,}\b/i);
    expect(text).not.toMatch(/\bu\d{4,}[-_][0-9a-z]{16,}\b/i);
    expect(text).not.toMatch(/\b[0-9a-f]{32,}\b/i);
    // Ключ берётся только из окружения: имя переменной есть, значения нет.
    expect(text).not.toMatch(/UPTIME_API_KEY\s*=\s*["']?[0-9a-z]/i);
  });

  it("W-11.2-T15: шапка рунбука соответствует факту регистрации монитора", () => {
    // История гарда. B-1 (CR r1): шапка утверждала «монитор включён … долг
    // закрыт» при незарегистрированном мониторе — гард требовал честное «НЕ
    // зарегистрирован». Итерация 4 (2026-10-27): монитор РЕАЛЬНО
    // зарегистрирован (id 804437010, красный сценарий проведён, алерт
    // доставлен) — прежнее требование стало ложью. Гард инвертирован по
    // факту, а не удалён: шапка обязана называть живой id монитора и не имеет
    // права возвращать «НЕ зарегистрирован» / «не настроен».
    const text = runbook();

    expect(text).not.toMatch(/НЕ зарегистрирован/i);
    expect(text).not.toMatch(/монитор не настроен/i);
    // Регистрация подтверждена идентификатором, а не словами «включён».
    expect(text).toContain("804437010");
    // Дата регистрации и интервал — часть факта, а не украшение.
    expect(text).toMatch(/2026-10-27/);
    expect(text).toMatch(/300 с/);
  });

  it("W-11.2-T15: §6 удаляет монитор, а не ставит на паузу", () => {
    // B-2: `editMonitor` со `status:0` — пауза; запись остаётся в аккаунте.
    // Команда удаления — исполняемый скрипт, который проверяет эффект.
    const text = runbook();
    // Проверяется ИСПОЛНЯЕМОЕ, а не проза: §6 объясняет, что до CR здесь
    // стоял `editMonitor` — это объяснение и должно остаться. Гард смотрит
    // только внутрь ```bash-блоков: команда удаления не имеет права содержать
    // editMonitor (пауза) и обязана называть deleteMonitor + скрипт.
    const bashBlocks = [...text.matchAll(/```bash\n([\s\S]*?)```/g)].map((m) => m[1]);
    expect(bashBlocks.length).toBeGreaterThan(0);
    const executable = bashBlocks.join("\n");

    expect(executable).not.toMatch(/editMonitor/);
    expect(executable).toMatch(/deleteMonitor|delete-monitor\.sh/);
    expect(text).toContain("deleteMonitor");
    expect(text).toContain("scripts/uptime/delete-monitor.sh");
    // Условие 4 оговорки: команда обязана быть выполнимой, а не абстрактной.
    expect(existsSync(path.resolve(path.dirname(RUNBOOK_PATH), "..", "scripts", "uptime", "delete-monitor.sh"))).toBe(true);
  });

  it("W-11.2-T6: адрес монитора — домен стенда, а не чужой карамель.рф", () => {
    // Замер 2026-10-27: `карамель.рф` → <SERVER_IP_2> (openresty, x-cache-status),
    // это не стенд (<SERVER_IP>, Caddy). Монитор на том домене получает 403
    // и измеряет чужой сервер — худший дефект этой стори.
    const text = runbook();

    expect(text).toContain("xn--j1accp.xn--p1ai");
    expect(text).toMatch(/не путать с/i);
    expect(text).toContain("<SERVER_IP_2>");
  });
});
