/**
 * Замки гейта 1 (стори 11.9; AC-6…AC-9).
 *
 * **Почему эти тесты появились только сейчас.** До 11.9 у гейта 1 не было ни
 * одного теста (замер: `grep -rln gate1 --include=*.test.* → пусто`). Гейт
 * вызывался в CI и падал/проходил, но никто не проверял, что он падает по той
 * причине, ради которой существует. Классический дефект «зелёный CI без
 * проверки»: гейт краснел в baseline на одном файле, и это не замечалось
 * неделями (стори 11.9, З-2).
 *
 * **Что проверяется эффектом, а не формой (P-25).** Тесты 2–4 запускают
 * настоящий гейт на настоящем файле и смотрят на код возврата. Проверка
 * «в исходнике есть слово allowlist» прошла бы и при мёртвой ветке.
 *
 * **Почему пробный файл в `src/app/`, а не в `src/components/`.** Гейт кроме
 * цветов проверяет полноту манифеста: незадекларированный `.tsx` в
 * `components/` — сам по себе ошибка. Проба там падала бы по чужой причине
 * и тест «allowlist глушит нарушение» не смог бы стать зелёным ни при каком
 * allowlist. `src/app/**` в манифест не входит — проба изолирована.
 */

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

// apps/web/src/test/__tests__ → корень репозитория (karamel)
const REPO_ROOT = resolve(import.meta.dirname, "../../../../..");
const GATE1 = join(REPO_ROOT, "scripts/design/gate1-hex.mjs");
const ALLOWLIST = join(REPO_ROOT, "scripts/design/legacy-allowlist.json");
const PROBE_DIR = join(REPO_ROOT, "apps/web/src/app/__gate1_probe__");
const PROBE_FILE = join(PROBE_DIR, "probe.tsx");
/** Путь от `apps/web/src` — в таком виде его сравнивает гейт и хранит allowlist. */
const PROBE_ALLOWLIST_PATH = "app/__gate1_probe__/probe.tsx";

/** Запускает гейт и возвращает код возврата и объединённый вывод. */
function runGate(extraArgs: string[] = []): { code: number; output: string } {
  try {
    const stdout = execFileSync("node", [GATE1, ...extraArgs], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    return { code: 0, output: stdout };
  } catch (error) {
    const e = error as { status?: number; stdout?: string; stderr?: string };
    return { code: e.status ?? 1, output: `${e.stdout ?? ""}${e.stderr ?? ""}` };
  }
}

/** Кладёт пробный файл с литеральным цветом. */
function writeProbe(): void {
  mkdirSync(PROBE_DIR, { recursive: true });
  // Литерал в произвольном классе Tailwind — ровно то, что гейт обязан ловить.
  writeFileSync(
    PROBE_FILE,
    'export const Probe = () => <div className="bg-[#123456]" />;\n',
    "utf8",
  );
}

/** Убирает пробный файл и каталог. */
function cleanupProbe(): void {
  rmSync(PROBE_DIR, { recursive: true, force: true });
}

/** Кладёт путь пробы в allowlist; возвращает исходное содержимое для отката. */
function allowProbe(): string {
  const original = readFileSync(ALLOWLIST, "utf8");
  const patched = JSON.parse(original) as { paths: string[] };
  patched.paths = [PROBE_ALLOWLIST_PATH];
  writeFileSync(ALLOWLIST, `${JSON.stringify(patched, null, 2)}\n`, "utf8");
  return original;
}

describe("гейт 1: ratchet исключений", () => {
  it("AC-7: исключений ноль, задокументировано ровно 17 путей волны 0", () => {
    // Краснеет, если кто-то положит 18-й путь в `paths` (ratchet вниз) или
    // удалит задокументированные пути (потеря истории замера).
    const raw = JSON.parse(readFileSync(ALLOWLIST, "utf8")) as {
      paths: string[];
      documentedPaths: string[];
    };
    expect(raw.paths).toEqual([]);
    expect(raw.documentedPaths).toHaveLength(17);
  });

  it("CR 11.9 M-16: documentedPaths совпадает с legacy-путями манифеста", () => {
    // Мутация CR: заменить все 17 `documentedPaths` на 17 выдуманных путей —
    // проходила ВСЕ 6 тестов (ассерт был только по длине). То есть «17 путей
    // волны 0 задокументированы» не проверялось: длина совпадала, а содержимое
    // могло быть любым, включая несуществующие файлы.
    //
    // Формат в allowlist — `components/…` (от `apps/web/src`), в манифесте —
    // `…` (от `src/components`). Приводим манифест к тому же виду.
    const allowlist = JSON.parse(readFileSync(ALLOWLIST, "utf8")) as {
      documentedPaths: string[];
    };
    const manifest = JSON.parse(
      readFileSync(join(REPO_ROOT, "apps/web/src/components/design-system.manifest.json"), "utf8"),
    ) as { components: Record<string, { path: string; status?: string }> };

    const legacyFromManifest = Object.values(manifest.components)
      .filter((entry) => entry.status === "legacy")
      .map((entry) => `components/${entry.path}`)
      .sort();

    expect([...allowlist.documentedPaths].sort()).toEqual(legacyFromManifest);
  });

  it("AC-6: hex в файле вне allowlist — гейт падает", () => {
    writeProbe();
    try {
      const { code, output } = runGate();
      expect(code).toBe(1);
      // Сообщение обязано называть файл: иначе в CI непонятно, что править.
      expect(output).toContain("__gate1_probe__/probe.tsx");
      expect(output).toContain("произвольный цвет");
    } finally {
      cleanupProbe();
    }
  });

  it("AC-8: путь в allowlist глушит нарушение — исключение явное, а не по умолчанию", () => {
    // Проверяется и обратная сторона ratchet: allowlist РАБОТАЕТ (иначе он был
    // бы декорацией), но только для объявленного пути.
    writeProbe();
    const original = allowProbe();
    try {
      const { code, output } = runGate();
      expect(code).toBe(0);
      expect(output).not.toContain("__gate1_probe__/probe.tsx");
    } finally {
      writeFileSync(ALLOWLIST, original, "utf8");
      cleanupProbe();
    }
  });

  it("CR H-5: незадекларированный компонент — гейт падает (полнота манифеста)", () => {
    // Мутация CR 11.9: закомментировать `errors.push(...checkManifestCompleteness(byPath))`
    // — проходила ВСЕ четыре прежних теста. Полнота манифеста не проверялась
    // ничем, хотя именно она ловит файл, созданный в обход гейта (З-2: так
    // `join-request-form.tsx` из 11.5 попал в «легаси» сам).
    // Проба ОБЯЗАНА лежать в `components/`: полноту манифеста гейт проверяет
    // только там (`walk(COMPONENTS_DIR)`), а `src/app/**` в манифест не входит.
    // Проба в `app/` вернула бы 0 при любом коде гейта — тест-дублёр самого себя.
    const dir = join(REPO_ROOT, "apps/web/src/components/__gate1_probe__");
    const probe = join(dir, "probe.tsx");
    mkdirSync(dir, { recursive: true });
    // Без единого цвета: единственная причина отказа — отсутствие объявления.
    writeFileSync(probe, "export const Probe = () => <div />;\n", "utf8");
    try {
      const { code, output } = runGate();
      expect(code).toBe(1);
      // Сообщение обязано называть файл и слово «манифест»: иначе непонятно,
      // что чинить — цвет или объявление.
      expect(output).toContain("__gate1_probe__/probe.tsx");
      expect(output).toContain("манифест");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("CR H-3: CI вызывает гейт с `--strict`", () => {
    // Мутация CR 11.9: убрать `--strict` из `.github/workflows/ci.yml` —
    // выживала. Проверка ТЕКСТОВАЯ, потому что эффект локально ненаблюдаем:
    // пайплайн исполняется только в GitHub Actions. Это единственный
    // доступный способ закрепить AC-9 в CI; форма здесь и есть эффект.
    const ci = readFileSync(join(REPO_ROOT, ".github/workflows/ci.yml"), "utf8");
    expect(ci).toContain("gate1-hex.mjs --strict");
    // Дефолтный вызов без флага в CI означал бы, что первое же исключение
    // проедет молча — ratchet откатится незаметно.
    expect(ci).not.toMatch(/run: node .*gate1-hex\.mjs\s*$/m);
  });

  it("AC-9: --strict не признаёт исключений", () => {
    // Краснеет, если `--strict` окажется синонимом дефолта: тогда первый же
    // legacy-путь проедет в CI, и ratchet откатится молча.
    writeProbe();
    const original = allowProbe();
    try {
      const { code, output } = runGate(["--strict"]);
      expect(code).toBe(1);
      expect(output).toContain("__gate1_probe__/probe.tsx");
    } finally {
      writeFileSync(ALLOWLIST, original, "utf8");
      cleanupProbe();
    }
  });
});
