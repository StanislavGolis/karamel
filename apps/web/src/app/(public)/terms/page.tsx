/**
 * Пользовательское соглашение (оферта) (Task 6.2; AC-06).
 *
 * Как и политика обработки ПД, страница открывается без авторизации и до
 * подтверждения: ссылка стоит рядом с чекбоксом согласия (D-11).
 */

import Link from "next/link";

export const metadata = {
  title: "Пользовательское соглашение — Карамель",
  description: "Условия использования сервиса «Карамель»: возраст, правила, ответственность.",
  // Патч 16 код-ревью 1.6: каркас не индексируется. Пометка «черновик»
  // не видна поисковику, а текст ещё изменится — индексация закрепила бы
  // черновые условия как действующую оферту.
  robots: { index: false, follow: false },
};

export default function TermsPage() {
  return (
    <main className="mx-auto max-w-2xl px-4 py-16">
      <article className="flex flex-col gap-6">
        <h1 className="text-3xl font-semibold tracking-tight">Пользовательское соглашение</h1>

        <p
          role="note"
          className="rounded-md border border-border-input bg-surface px-4 py-3 text-sm text-ink-secondary"
        >
          Черновик. Документ требует юридической вычитки: итоговые условия утверждает
          владелец сервиса.
        </p>

        <section className="flex flex-col gap-3">
          <h2 className="text-xl font-semibold">1. О сервисе</h2>
          <p className="text-base leading-relaxed">
            «Карамель» — дейтинг-сервис с ограниченным числом лайков в день. Сервис
            помогает познакомиться для отношений, а не для бесконечного просмотра анкет.
          </p>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-xl font-semibold">2. Возраст</h2>
          <p className="text-base leading-relaxed">
            Сервис доступен только лицам старше 18 лет. Возраст подтверждается при
            регистрации; при сомнениях администрация вправе запросить подтверждение и
            ограничить доступ.
          </p>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-xl font-semibold">3. Правила поведения</h2>
          <ul className="list-disc pl-6 text-base leading-relaxed">
            <li>только свои фотографии и достоверные данные о себе;</li>
            <li>без оскорблений, угроз, спама и рекламы;</li>
            <li>без публикации чужих персональных данных и переписок.</li>
          </ul>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-xl font-semibold">4. Модерация и санкции</h2>
          <p className="text-base leading-relaxed">
            Фотографии проходят проверку модератора. За нарушение правил применяются
            предупреждение или блокировка учётной записи с уведомлением.
          </p>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-xl font-semibold">5. Изменения условий</h2>
          <p className="text-base leading-relaxed">
            Условия могут изменяться; актуальная версия всегда доступна по этому адресу.
            Продолжая пользоваться сервисом, вы принимаете обновлённые условия.
          </p>
        </section>

        <Link href="/" className="text-sm text-ink-secondary underline underline-offset-4">
          На главную
        </Link>
      </article>
    </main>
  );
}
