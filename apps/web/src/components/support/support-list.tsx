"use client";

import { EmptyState } from "@/components/ui/empty-state";
/**
 * Список своих обращений в поддержку (стори 9.5; AC-02, AC-03; Task 6.3, 6.4).
 *
 * **Статус — ТЕКСТОМ и ИКОНКОЙ, не только цветом** (NFR56/NFR57). Цвет как
 * единственный носитель смысла недоступен части людей и не читается
 * скринридером; поэтому рядом с цветной плашкой стоит подпись.
 *
 * **Тело обращения сворачивается** (`<details>`): длинное письмо на 2000
 * символов иначе вытеснило бы из вида статус и ответ — то, ради чего экран
 * открывают. `<details>` вместо ручного стейта: раскрытие не является
 * серверным состоянием и не требует JS.
 *
 * **`unavailable` НЕ показывается этим компонентом.** Сбой `5xx`
 * обрабатывается страницей отдельным экраном: пустой список после ошибки
 * читался бы как «у меня нет обращений».
 */

/**
 * Один элемент списка в объёме, который нужен экрану.
 *
 * CR 9.5 LOW-2: ЕДИНСТВЕННОЕ объявление типа. До правки `support-form.tsx`
 * дублировал его независимо, и при расширении контракта одно из описаний
 * отставало бы — `tsc` молчал бы из-за структурной совместимости.
 */
export type SupportTicketView = {
  uuid: string;
  body: string;
  status: string;
  created_at: string;
  answer: string | null;
  answered_at: string | null;
};

/**
 * Подписи статусов. Неизвестный статус не теряется (NFR60): показываем его
 * техническое имя, а не пустую плашку — иначе обращение выглядело бы
 * «без статуса».
 */
const STATUS_LABELS: Record<string, string> = {
  open: "Ждёт ответа",
  answered: "Отвечено",
  closed: "Закрыто",
};

/**
 * Иконки статусов. `aria-hidden`, потому что смысл уже передан текстом:
 * дублировать его для скринридера значило бы читать «Ждёт ответа, иконка
 * часов» дважды.
 */
const STATUS_ICONS: Record<string, string> = {
  open: "⏳",
  answered: "✓",
  closed: "—",
};

/**
 * Цвет плашки — только токены (`warning` / `success` / `ink-secondary`).
 * Контраст пар `warning`/`surface` и `success`/`surface` проверяется гейтами 2 и 4
 * (`scripts/design/gate2-contrast-light.mjs`, `gate4-contrast-both.mjs`),
 * а не комментарием: значения меняются вместе с темой.
 *
 * Волна 4 (2026-10-26): раньше здесь были записаны hex из палитры A
 * (warning `8A5A00`, success `2F6B4F`). Оба мапятся на токены §2.2 в v2:
 * `success` пересчитан на `2B6349`, `warning` сохранён.
 */
const STATUS_CLASSES: Record<string, string> = {
  open: "text-warning",
  answered: "text-success",
  closed: "text-ink-secondary",
};

/**
 * Формат даты — ФИКСИРОВАННЫЙ `Intl.DateTimeFormat` с явной зоной.
 *
 * CR 9.5 LOW-4: `toLocaleDateString()` брал зону ИЗ ОКРУЖЕНИЯ. Клиентский
 * компонент рендерится сначала на сервере (Node, зона контейнера — обычно
 * UTC), затем гидрируется в браузере (зона человека). Для `created_at`
 * около полуночи строки расходились — React сообщал hydration mismatch.
 *
 * `timeZone: "UTC"` делает результат ОДИНАКОВЫМ на сервере и в браузере:
 * дата публикуется в UTC осознанно, как метка момента, а не «местное утро».
 * Формат `ru-RU` фиксирован — экран русскоязычный.
 */
const DATE_FORMAT = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return DATE_FORMAT.format(date);
}

export function SupportTicketList({ items }: { items: SupportTicketView[] }) {
  if (items.length === 0) {
    return (
      /* Task 6.4 + §8.3: пустота объясняет, КУДА писать, если форма не подходит
         (например, аккаунт заблокирован и форма даёт 403). `EmptyState` — единый
         компонент пустоты (§7.8), а не самописная плашка. */
      <EmptyState
        title="Обращений пока нет"
        reason="Опишите проблему в форме выше. Если войти не получается — напишите на почту, указанную на странице:"
        actionKind="quiet"
      >
        <a className="underline" href="/privacy#contact">
          «Конфиденциальность»
        </a>
      </EmptyState>
    );
  }

  return (
    <ul className="flex flex-col gap-3">
      {items.map((item) => {
        const label = STATUS_LABELS[item.status] ?? item.status;
        const icon = STATUS_ICONS[item.status] ?? "•";
        const colorClass = STATUS_CLASSES[item.status] ?? "text-ink-secondary";

        return (
          <li
            key={item.uuid}
            className="flex flex-col gap-2 rounded-2xl border border-border px-4 py-3"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className={`text-sm ${colorClass}`}>
                <span aria-hidden="true">{icon}</span> {label}
              </span>
              <span className="text-sm text-ink-secondary">{formatDate(item.created_at)}</span>
            </div>
            <details className="flex flex-col gap-1">
              <summary className="cursor-pointer text-base leading-relaxed">
                Ваше обращение
              </summary>
              {/* `whitespace-pre-line`: абзацы обращения значимы (D-9.5-12),
                  и склеивание строк сделало бы «шаги воспроизведения»
                  нечитаемыми. */}
              <p className="whitespace-pre-line text-base leading-relaxed text-ink-secondary">
                {item.body}
              </p>
            </details>
            {/* AC-03: ответ и дата видны в списке, без перехода на детальный
                экран (детального экрана в 9.5 нет, D-9.5-2). */}
            {item.status === "answered" && item.answer ? (
              <div className="flex flex-col gap-1 border-t border-border pt-2">
                <span className="text-sm font-semibold">Ответ поддержки</span>
                <p className="whitespace-pre-line text-base leading-relaxed">
                  {item.answer}
                </p>
                {item.answered_at ? (
                  <span className="text-sm text-ink-secondary">
                    {formatDate(item.answered_at)}
                  </span>
                ) : null}
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
