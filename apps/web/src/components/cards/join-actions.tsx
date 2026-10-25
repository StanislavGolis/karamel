"use client";

/**
 * Присоединение к карточке открытого набора и отмена участия
 * (стори 5б.2, Task 5.4; AC-09; FR104, X-5a.6-2).
 *
 * До этой стори FR104 был недостижим в вебе: `POST /join` и `DELETE /join`
 * существовали на сервере (5а.5, 5а.6), но ни одной кнопки не было.
 *
 * **Почему клиентский компонент, а не серверный.** Действие живёт по нажатию:
 * серверный компонент не умеет ждать `fetch` пользователя. Список заявившихся
 * и карточка остаются серверными — здесь только кнопка.
 *
 * **Без оптимистичного статуса** (D-5b.2-4, X-43): WS нет, и «мгновенно
 * покажем, что вы в компании» разошлось бы с БД при отказе. После успеха —
 * `router.refresh()`: сервер отдаёт уже посчитанный `viewer_participates`
 * и `filled_count`.
 *
 * **Коды ошибок — машинные** (`code`), тексты выбираются здесь: сервер не
 * знает, на каком экране показывается отказ (project-context §4). Слова
 * «вас не выбрали» и подобные запрещены (UX §636): отказ должен быть
 * безличным.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { joinCard, leaveCard } from "@/lib/queries/cards";

/** Тексты отказов по машинному коду — безличные формулировки (UX §636). */
const ERROR_TEXT: Record<string, string> = {
  CARD_IS_FULL: "Свободных мест больше нет",
  CARD_NOT_ACCEPTING: "Набор закрыт",
  ALREADY_JOINED: "Вы уже в этой компании",
  PARTICIPATION_NOT_FOUND: "Вы не в этой компании",
  CAPACITY_ONE_USES_REQUESTS: "Здесь собирают компанию по заявкам",
  network_error: "Нет связи. Попробуйте ещё раз",
};

/**
 * Подпись для неизвестного кода: текст не раскрывает состояние других людей.
 *
 * `http_5xx` тоже попадает сюда: сервер вернул код без `code`, и показывать
 * сырой номер пользователю бессмысленно.
 */
function errorText(code: string): string {
  return ERROR_TEXT[code] ?? "Не удалось выполнить действие. Попробуйте ещё раз";
}

export type JoinActionsProps = {
  uuid: string;
  /** `true` — зритель в составе компании (`viewer_participates`). */
  participates: boolean;
  /** `true` — карточка принимает участие (`published` с открытым окном). */
  joinable: boolean;
};

export function JoinActions({ uuid, participates, joinable }: JoinActionsProps) {
  const router = useRouter();
  // `pending` блокирует повторный клик: двойное нажатие отправило бы второй
  // `POST`, а он на сервере НЕ идемпотентен (`ALREADY_JOINED`), и пользователь
  // увидел бы ошибку на действии, которое только что прошло.
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Ни одного действия не показываем, когда оно бессмысленно: карточка
  // собрана/закрыта — «Присоединиться» вело бы в гарантированный `409`.
  if (!participates && !joinable) return null;

  async function run(action: "join" | "leave") {
    if (pending) return;
    setPending(true);
    setError(null);
    const result = action === "join" ? await joinCard(uuid) : await leaveCard(uuid);
    setPending(false);
    if (!result.ok) {
      setError(errorText(result.code));
      return;
    }
    // AC-09: после успеха экран перечитывает карточку — статус, места и
    // признак участия приходят с сервера, а не из локального состояния.
    router.refresh();
  }

  return (
    <div className="flex flex-col gap-2">
      {participates ? (
        <button
          type="button"
          onClick={() => run("leave")}
          disabled={pending}
          className="min-h-11 self-start rounded-md border border-[#D8CFC4] px-4 py-2 text-sm font-medium text-[#3D3529] hover:bg-[#F2EDE6] disabled:opacity-60"
        >
          {pending ? "Отменяем…" : "Отменить участие"}
        </button>
      ) : (
        <button
          type="button"
          onClick={() => run("join")}
          disabled={pending}
          className="min-h-11 self-start rounded-md bg-[#7A3E12] px-4 py-2 text-sm font-medium text-white hover:bg-[#5E2F0D] disabled:opacity-60"
        >
          {pending ? "Присоединяемся…" : "Присоединиться"}
        </button>
      )}
      {error ? (
        <p role="alert" className="text-sm text-[#8A2B12]">
          {error}
        </p>
      ) : null}
    </div>
  );
}
