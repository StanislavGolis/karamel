/**
 * Правила модерации — публичная страница (стори 10.4; AC-01, AC-02, AC-03).
 *
 * **Почему `(public)`, а не `(app)`** (AC-01.3, D-10.4-5): защита маршрутов
 * живёт в `app/(app)/app/layout.tsx` — редирект есть только внутри группы
 * `(app)`, `middleware.ts` в проекте отсутствует. Файл в `(public)` не может
 * быть перехвачен по конструкции, но это доказывается тремя способами
 * (расположение, рендер без сессии, статический гард исходника), а не
 * рассуждением.
 *
 * **Почему `index: true`** (D-10.4-4): `/privacy` и `/terms` несут
 * `index: false` — они черновики до юридической вычитки (решение 1.6). Правила
 * модерации — рабочее обещание пользователю, их должны находить до регистрации.
 * Расхождение с соседями осознанное.
 *
 * **Текст приходит из константы** (`@/lib/moderation-rules`), а не хардкодится
 * в JSX: иначе версия и текст разъедутся, и гард хеша перестанет что-либо
 * значить (AC-03.1).
 */

import Link from "next/link";

import {
  MODERATION_RULES_SECTIONS,
  rulesVersionLabel,
} from "@/lib/moderation-rules";

export const metadata = {
  title: "Правила модерации — Карамель",
  description:
    "Что проверяется в анкете, что не оценивается никогда, за что выносится предупреждение и когда ограничивается доступ.",
  // D-10.4-4: правила индексируются — это рабочее обещание, а не черновик.
  robots: { index: true, follow: true },
};

/** Классы ссылки: тач-зона 44 px (AC-02.3) и видимый фокус (AC-02.4). */
const LINK_CLASS =
  "inline-flex min-h-11 items-center text-sm text-ink-secondary underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring";

export default function RulesPage() {
  return (
    <main className="mx-auto max-w-2xl px-4 py-16">
      <article className="flex flex-col gap-6">
        {/* AC-02.1: ровно один h1 на странице. */}
        <h1 className="text-3xl font-semibold tracking-tight">Правила модерации</h1>

        {/* AC-03.2: версия и дата — из константы, не литералом. */}
        <p className="text-sm text-ink-secondary">{rulesVersionLabel()}</p>

        <p className="text-base leading-relaxed">
          Ниже — то, как «Карамель» проверяет анкеты и что делает при нарушении.
          Прочитать можно без регистрации: правила действуют с момента, когда вы
          открыли сервис, а не с момента, когда отметили согласие.
        </p>

        {/* AC-02.2: разделы — section с h2, пункты — ul/li; псевдозаголовков нет. */}
        {MODERATION_RULES_SECTIONS.map((section) => (
          <section key={section.id} id={section.id} className="flex flex-col gap-3">
            <h2 className="text-xl font-semibold">{section.title}</h2>
            <ul className="list-disc pl-6 text-base leading-relaxed">
              {section.items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
        ))}

        <p className="text-base leading-relaxed text-ink-secondary">
          Правила могут меняться: новая редакция публикуется на этой странице с
          новой версией и датой. Если формулировка изменилась, версия меняется
          вместе с ней.
        </p>

        <nav aria-label="Другие документы" className="flex flex-col gap-2">
          <Link href="/" className={LINK_CLASS}>
            На главную
          </Link>
          <Link href="/privacy" className={LINK_CLASS}>
            Политика обработки персональных данных
          </Link>
          <Link href="/terms" className={LINK_CLASS}>
            Пользовательское соглашение
          </Link>
        </nav>
      </article>
    </main>
  );
}
