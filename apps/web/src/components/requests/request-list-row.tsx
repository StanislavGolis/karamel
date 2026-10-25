"use client";

/**
 * Строка заявившегося с рабочей кнопкой «Выбрать»
 * (UX §863–865; стори 5а.4 Task 9.2 → стори 5б.2 Task 6; AC-10).
 *
 * Аватар 48px + имя, возраст + сообщение (≤3 строки) + кнопка «Выбрать».
 * **Никаких метрик и «активности»** (UX §863): ни числа встреч, ни рейтинга —
 * автор выбирает по человеку и его сообщению, а не по счёту.
 *
 * `message` обрезается тремя строками (`line-clamp-3`), а не многоточием в
 * коде: CSS не ломает слова и работает на любой длине.
 *
 * **Кнопка включена в 5б.2** (OQ-5a.4-1, вариант A, решение №6 ER 5а).
 * В 5а.4 она была `disabled` с пояснением «появится в следующем обновлении»:
 * выбора не существовало. Теперь действие есть (`POST /requests/{uuid}/select`),
 * и надпись-заглушка стала бы ложью.
 *
 * **Почему компонент клиентский, а список — нет** (D-5b.2-8). Нажатие живёт в
 * браузере, но клиентский СПИСОК дал бы мигание: пока грузится ответ, автор
 * видел бы пустой экран вместо уже известных заявок (UX §895). Поэтому
 * `RequestList` остаётся серверным и рендерит строки, а состояние нажатия
 * живёт в каждой строке отдельно.
 *
 * **Без оптимистичных статусов** (D-5b.2-4, X-43): WS нет. После успеха —
 * `router.refresh()`, и список перерисовывается сервером: у автора останется
 * ровно одна заявка (выбранная), остальные исчезнут по фильтру `selected`.
 *
 * **Тексты отказов безличные** (UX §636): `REQUEST_NOT_PENDING` — «Заявка
 * больше не активна», а не «вас не выбрали»; `CARD_ALREADY_FILLED` —
 * «Компания уже собрана». Слова «отклонили» и «не выбрали» запрещены:
 * отказ не должен персонализироваться.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { PersonMini } from "@/components/requests/person-mini";
import { selectRequest } from "@/lib/queries/cards";

/** Тексты отказов по машинному коду — безличные (UX §636). */
const ERROR_TEXT: Record<string, string> = {
  CARD_ALREADY_FILLED: "Компания уже собрана",
  REQUEST_NOT_PENDING: "Заявка больше не активна",
  CARD_NOT_ACCEPTING: "Набор закрыт",
  card_not_found: "Карточка недоступна",
  request_not_found: "Заявка больше не активна",
  network_error: "Нет связи. Попробуйте ещё раз",
};

/**
 * Подпись для неизвестного кода.
 *
 * Не показывает сырой код: `http_500` пользователю ничего не объясняет, а
 * любой текст здесь обязан оставаться безличным.
 */
function errorText(code: string): string {
  return ERROR_TEXT[code] ?? "Не удалось выбрать. Попробуйте ещё раз";
}

type RequestListRowProps = {
  /** AC-10: `uuid` ЗАЯВКИ — по нему идёт `POST /requests/{uuid}/select`. */
  requestUuid: string;
  person: { uuid: string; name: string | null; age: number | null; photo_url: string | null };
  message: string;
  /** `false` — открытый набор: выбора нет, кнопка не рисуется (FR70). */
  selectionAvailable: boolean;
  /**
   * AC-10: кнопка активна только при `status='published'`.
   *
   * Сервер отвечает `409` и сам, но заведомо провальный клик на собранной
   * карточке читается как «кнопка сломана». `selection_available` этого не
   * покрывает: он про РЕЖИМ карточки, а не про её состояние.
   */
  cardPublished: boolean;
};

export function RequestListRow({
  requestUuid,
  person,
  message,
  selectionAvailable,
  cardPublished,
}: RequestListRowProps) {
  const router = useRouter();
  // `pending` блокирует повторный клик: `select` НЕ идемпотентен (D-5b.2-9),
  // и второй запрос вернул бы `409 REQUEST_NOT_PENDING` на действии, которое
  // только что прошло успешно.
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function select() {
    if (pending) return;
    setPending(true);
    setError(null);
    const result = await selectRequest(requestUuid);
    setPending(false);
    if (!result.ok) {
      setError(errorText(result.code));
      return;
    }
    // AC-10: после успеха список перечитывается — выбранная заявка остаётся
    // единственной, остальные исчезают по серверному фильтру `selected`.
    router.refresh();
  }

  return (
    <li className="flex flex-col gap-3 rounded-lg border border-[#E4DCD1] bg-white p-4">
      <PersonMini name={person.name} age={person.age} photoUrl={person.photo_url} />
      <p className="line-clamp-3 text-sm leading-relaxed text-[#3D3529]">{message}</p>
      {selectionAvailable ? (
        <button
          type="button"
          onClick={select}
          disabled={pending || !cardPublished}
          className="min-h-11 self-start rounded-md bg-[#7A3E12] px-4 py-2 text-sm font-medium text-white hover:bg-[#5E2F0D] disabled:opacity-60"
        >
          {pending ? "Выбираем…" : "Выбрать"}
        </button>
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-[#8A2B12]">
          {error}
        </p>
      ) : null}
    </li>
  );
}
