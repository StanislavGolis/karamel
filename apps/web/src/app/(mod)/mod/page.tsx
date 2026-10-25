/**
 * Заглушка кабинета модератора (эпик 6).
 *
 * Контейнер и шапка живут в `layout.tsx`: он же проверяет сессию и роль.
 * Страница отдаёт только содержимое — иначе при добавлении гарды получилось бы
 * два вложенных контейнера с разной шириной.
 */

export const metadata = { robots: { index: false, follow: false } };

export default function ModerationPage() {
  return (
    <main className="flex flex-col gap-2">
      <h1 className="text-2xl font-semibold">Модерация</h1>
      <p className="text-base">Заглушка кабинета модератора.</p>
    </main>
  );
}
