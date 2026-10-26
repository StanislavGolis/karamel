/**
 * Кабинет модератора (эпик 6, дополнен стори 10.5, Task 7.6).
 *
 * Контейнер и шапка живут в `layout.tsx`: он же проверяет сессию и роль.
 * Страница отдаёт только содержимое — иначе при добавлении гарды получилось бы
 * два вложенных контейнера с разной шириной.
 *
 * **Ссылка на метрики пилота** (Task 7.6). Отдельный вход, а не вкладка внутри
 * очереди: метрики — про продукт, очередь — про карточки, и смешивать два
 * предмета на одном экране значит показывать модератору числа, пока он
 * разбирает контент. Тач-зона ссылки 44 px и видимый фокус — §10.
 */

export const metadata = { robots: { index: false, follow: false } };

export default function ModerationPage() {
  return (
    <main className="flex flex-col gap-2">
      <h1 className="text-2xl font-semibold">Модерация</h1>
      <p className="text-base">Заглушка кабинета модератора.</p>
      <a
        href="/mod/metrics"
        className="inline-flex min-h-11 items-center text-sm text-ink-secondary underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
      >
        Метрики пилота
      </a>
    </main>
  );
}
