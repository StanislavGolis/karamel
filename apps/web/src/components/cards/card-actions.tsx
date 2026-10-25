/**
 * Действия автора на карточке события (стори 4.6, Task 7; AC-03, AC-04, AC-11).
 *
 * «Закрыть набор» (FR74) и «Отменить встречу» (FR100). Оба действия необратимы
 * для автора, поэтому отмена требует подтверждения (UX §618: sheet с явным
 * вопросом), а закрытие набора — нет: оно не отменяет встречу, участники
 * остаются, и лишний шаг только замедлял бы обычный сценарий.
 *
 * **Почему клиентский компонент.** `close`/`cancel` — мутации по нажатию;
 * серверный компонент не умеет обрабатывать клик без формы и `action=`,
 * а форма дала бы навигацию вместо ответа `409`, который нужно показать.
 *
 * **Никакого оптимистичного статуса.** После успеха карточка перезапрашивается
 * (`router.refresh()`): статус — серверный факт (`closed_at`, `cancelled`),
 * и подставлять его локально значило бы показывать состояние, которого в БД
 * может не быть (например, `cancel` проиграл гонку архиву — `409`).
 */

"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { cancelCard, closeCard } from "@/lib/queries/cards";

/** Текст отказа по машинному коду (§4: клиент ветвится по `code`). */
const ERROR_TEXT: Record<string, string> = {
  CARD_NOT_PUBLISHED: "Набор уже закрыт или встреча отменена. Обновите страницу.",
  CARD_NOT_CANCELLABLE: "Встреча уже прошла или отменена. Отменить её нельзя.",
  CARD_ALREADY_JOINED: "На карточку уже есть участник — отмена остаётся возможной.",
  card_not_found: "Карточка недоступна. Обновите страницу.",
  unauthenticated: "Сессия истекла. Войдите снова.",
  network_error: "Нет связи. Проверьте подключение и повторите.",
};

type Action = "close" | "cancel";

type CardActionsProps = {
  uuid: string;
  /** `closed`/`finished`/`cancelled` — действия недоступны (AC-11). */
  status: "draft" | "published" | "full" | "closed" | "finished" | "cancelled";
};

/** Доступно ли закрытие набора: только у опубликованной карточки (FR74). */
function canClose(status: CardActionsProps["status"]): boolean {
  return status === "published";
}

/** Доступна ли отмена: всё, кроме архива и уже отменённой (FR100). */
function canCancel(status: CardActionsProps["status"]): boolean {
  return status !== "finished" && status !== "cancelled";
}

export function CardActions({ uuid, status }: CardActionsProps) {
  const router = useRouter();
  const [pending, setPending] = useState<Action | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const closeAvailable = canClose(status);
  const cancelAvailable = canCancel(status);
  if (!closeAvailable && !cancelAvailable) return null;

  async function run(action: Action) {
    setPending(action);
    setError(null);
    const result = action === "close" ? await closeCard(uuid) : await cancelCard(uuid);
    setPending(null);
    setConfirming(false);
    if (result.ok) {
      // Статус — серверный факт: перечитываем карточку, а не подставляем локально.
      router.refresh();
      return;
    }
    setError(ERROR_TEXT[result.code] ?? "Не удалось выполнить действие. Повторите.");
  }

  return (
    <section aria-labelledby="card-actions-title" className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4">
      <h2 id="card-actions-title" className="text-base font-semibold text-ink">
        Управление встречей
      </h2>

      {error ? (
        // `role="alert"` — отказ по конкретному коду должен быть объявлен сразу.
        // §2.3: `danger` здесь законен — это сбой действия, а не штатный статус.
        <p role="alert" className="rounded-md bg-danger-tint px-3 py-2 text-sm text-danger">
          {error}
        </p>
      ) : null}

      {closeAvailable ? (
        <Button
          type="button"
          variant="secondary"
          loading={pending === "close"}
          disabled={pending !== null}
          onClick={() => void run("close")}
        >
          Закрыть набор
        </Button>
      ) : null}

      {cancelAvailable ? (
        confirming ? (
          <div className="flex flex-col gap-2 rounded-md bg-surface-raised p-3">
            {/* UX §618: вопрос и два действия; «Отмена» — тихая, не акцент. */}
            <p className="text-sm text-ink-secondary">
              Отменить встречу? Участники увидят отмену, вернуть карточку нельзя.
            </p>
            <div className="flex flex-wrap gap-2">
              {/* §2.4: отмена встречи необратима, но это не «опасное действие
                  модератора». `primary` — акцент на решении, красный не нужен. */}
              <Button
                type="button"
                variant="primary"
                loading={pending === "cancel"}
                disabled={pending !== null}
                onClick={() => void run("cancel")}
              >
                Отменить встречу
              </Button>
              <Button
                type="button"
                variant="secondary"
                disabled={pending !== null}
                onClick={() => setConfirming(false)}
              >
                Оставить
              </Button>
            </div>
          </div>
        ) : (
          <Button type="button" variant="secondary" onClick={() => setConfirming(true)}>
            Отменить встречу
          </Button>
        )
      ) : null}
    </section>
  );
}
