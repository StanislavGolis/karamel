"use client";

/**
 * Форма заявки на карточку режима «заявки» (стори 11.5; AC-1, AC-3, AC-8;
 * FR76, FR82).
 *
 * **Зачем отдельный компонент, а не правка `JoinRequestSheet`.** Лист — это
 * подтверждение мгновенного присоединения открытого набора (`FR83`), где
 * сообщения нет вовсе. Заявка же требует текста (`min_length = 1`), счётчика
 * остатка и отдельного тона для незавершённого ввода. Один компонент на два
 * разных действия заставлял бы ветвиться на каждой строке и разошёлся бы при
 * первой правке любого из них (долг `W-11.5` по разделению режимов — 11.6).
 *
 * **Счётчик — свой `span`, а не примитив `Textarea`.** Примитив помечает
 * счётчик `aria-hidden`: он не знает, что для формы заявки число остатка —
 * смысл (`AC-1`), а не украшение. Здесь счётчик объявлен через
 * `aria-live="polite"`: человек, набирающий вслепую, слышит, сколько осталось.
 *
 * **Причина недоступности — `warning`, не `danger`** (решение владельца П-4б).
 * Пустое поле — незавершённый ввод, а не сбой сервера и не необратимое
 * действие; `danger` зарезервирован за удалением аккаунта и баном.
 *
 * **Коды ошибок — машинные**, тексты выбираются здесь: сервер не знает, на
 * каком экране показывается отказ (project-context §4). Формулировки
 * безличные (FR81): «вас не выбрали» и подобные запрещены.
 */

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { submitJoinRequest } from "@/lib/queries/cards";

/** Предел сообщения — из схемы `JoinRequestCreate` (`max_length=300`). */
const MESSAGE_MAX_LENGTH = 300;

/**
 * Тексты отказов по машинному коду.
 *
 * `LIMIT_REACHED` — `FR77`: две активные заявки уже есть, третья невозможна.
 * Формулировка называет лимит числом: «попробуйте позже» не объяснило бы,
 * что именно мешает и когда пройдёт (отмена освобождает слот).
 */
const ERROR_TEXT: Record<string, string> = {
  // CR 11.5 HIGH-1: сервер отдаёт `JOIN_REQUEST_LIMIT_REACHED`
  // (`join_request_service.py:116`), а не `LIMIT_REACHED`. Промах в один
  // префикс давал фолбэк «Не удалось отправить заявку» вместо причины —
  // ровно на том отказе, который AC-4 требует объяснить.
  JOIN_REQUEST_LIMIT_REACHED: "Уже 2 активные заявки. Отмените одну, чтобы подать новую",
  ALREADY_REQUESTED: "Вы уже подали заявку на эту встречу",
  CAPACITY_ONE_USES_REQUESTS: "Здесь собирают компанию по заявкам",
  CARD_NOT_ACCEPTING: "Набор закрыт",
  CARD_IS_FULL: "Свободных мест больше нет",
  // CR 11.5 MED: технический лимитер подачи (`429`). Без текста человек
  // жмёт снова и продлевает блокировку.
  rate_limited: "Слишком часто. Подождите минуту и попробуйте снова",
  network_error: "Нет связи. Попробуйте ещё раз",
};

function errorText(code: string): string {
  return ERROR_TEXT[code] ?? "Не удалось отправить заявку. Попробуйте ещё раз";
}

export type JoinRequestFormProps = {
  /** `uuid` карточки — адрес `POST /cards/{uuid}/requests`. */
  uuid: string;
  /** Успех: экран перечитывает карточку (`router.refresh()`). */
  onDone?: () => void;
  /** §8.3: без связи запрос заведомо провалится — кнопка блокируется. */
  offline?: boolean;
};

export function JoinRequestForm({ uuid, onDone, offline = false }: JoinRequestFormProps) {
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // CR 11.5 HIGH-3 (раскрытие 4.1): после `JOIN_REQUEST_LIMIT_REACHED` кнопка
  // ОБЯЗАНА стать недоступной с причиной. До фикса показывался `role="alert"`,
  // а кнопка оставалась активной: тест назывался «кнопка недоступна», но
  // проверял только текст (тавтология §18.2). Состояние отдельным флагом, а не
  // вычислением из текста ошибки: текст — для человека, флаг — для логики.
  const [limitReached, setLimitReached] = useState(false);

  const trimmed = message.trim();
  // AC-3: пустое (или из одних пробелов) сообщение — кнопка недоступна.
  // `trim()` проверяется на КЛИЕНТЕ ровно тем же правилом, что `min_length=1`
  // после нормализации на сервере: иначе «   » уходило бы запросом и
  // возвращалось `422` на вводе, который форма считала валидным.
  const empty = trimmed.length === 0;
  // Раскрытие 4.1: лимит блокирует кнопку наравне с пустым вводом.
  const disabled = empty || sending || offline || limitReached;

  // Причина недоступности. Одна строка на все случаи: три отдельных абзаца
  // дублировали бы друг друга и спорили за внимание скринридера.
  const reason = offline
    ? "Нет связи. Отправить заявку можно будет, когда соединение вернётся"
    : limitReached
      ? // Тон `warning` (П-4б): это не сбой, а состояние счёта заявок.
        "Больше нельзя: у вас уже 2 активные заявки. Отмените одну в «Моих заявках»"
      : empty
        ? "Напишите хотя бы несколько слов — автор увидит это сообщение"
        : null;

  async function submit() {
    // AC-3: недоступная кнопка не отправляет запрос даже по Enter в поле —
    // гард здесь, а не только в `disabled`.
    if (disabled) return;
    setSending(true);
    setError(null);
    const result = await submitJoinRequest(uuid, trimmed);
    setSending(false);
    if (!result.ok) {
      // §8.4 п.3: ввод НЕ теряется — сообщение живёт рядом с полем, а не
      // вместо него.
      setError(errorText(result.code));
      // Раскрытие 4.1: лимит — не «попробуйте ещё раз», а состояние.
      // Повтор гарантированно провалится, пока человек не отменит заявку,
      // поэтому кнопка блокируется и причина остаётся на экране.
      if (result.code === "JOIN_REQUEST_LIMIT_REACHED") setLimitReached(true);
      return;
    }
    setMessage("");
    onDone?.();
  }

  const counterId = "join-request-counter";
  const reasonId = "join-request-reason";
  const errorId = "join-request-error";
  const fieldId = "join-request-message";

  // AC-1: поле связано и со счётчиком, и с причиной недоступности — человек,
  // перешедший в поле с клавиатуры, слышит оба факта.
  const describedBy = [counterId, reason ? reasonId : null, error ? errorId : null]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="flex flex-col gap-3">
      <label htmlFor={fieldId} className="text-base font-medium text-ink">
        Сообщение автору
      </label>
      <textarea
        id={fieldId}
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        // AC-1: предел тот же, что у сервера. Клиентское ограничение — не
        // защита (сервер проверяет заново), а способ не дать набрать текст,
        // который всё равно будет отвергнут.
        maxLength={MESSAGE_MAX_LENGTH}
        rows={3}
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        placeholder="Что возьмёте, во что играете, о чём хотите поговорить"
        className="flex min-h-24 w-full resize-y rounded-md border border-border-input bg-surface-raised px-4 py-3 text-base text-ink placeholder:text-ink-muted outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
      />

      {/* AC-1, §13.3: остаток объявлен через `aria-live="polite"` — число
          меняется на каждое нажатие, но перебивать ввод не должно. */}
      <span
        id={counterId}
        aria-live="polite"
        className="self-end text-xs text-ink-muted tabular-nums"
      >
        {`${MESSAGE_MAX_LENGTH - message.length} из ${MESSAGE_MAX_LENGTH}`}
      </span>

      {reason ? (
        // AC-3: причина в тоне `warning`, не `danger` (П-4б). Видимый текст,
        // а не только `sr-only` в кнопке: цвет и текст вместе, чтобы причина
        // читалась и глазами, и скринридером (NFR57).
        <p id={reasonId} className="text-sm text-warning">
          {reason}
        </p>
      ) : null}

      {error ? (
        // §8.3, §2.3: сбой действия — `danger` законен.
        <p id={errorId} role="alert" className="text-sm text-danger">
          {error}
        </p>
      ) : null}

      <Button
        type="button"
        variant="primary"
        className="self-start"
        loading={sending}
        disabled={disabled}
        // `aria-describedby` передаётся пропсом: видимая причина уже связана
        // с кнопкой по id, второй скрытой копии текста не нужно.
        aria-describedby={reason ? reasonId : undefined}
        onClick={() => void submit()}
      >
        Отправить
      </Button>
    </div>
  );
}
