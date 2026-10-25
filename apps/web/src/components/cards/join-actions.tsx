"use client";

/**
 * Присоединение к карточке открытого набора и отмена участия
 * (стори 5б.2, Task 5.4; AC-09; FR104, X-5a.6-2).
 *
 * **Волна 3 миграции (§15.2): подтверждение переехало на примитив `Sheet`.**
 * До волны присоединение происходило мгновенно по нажатию кнопки: одно
 * случайное касание ставило человека в компанию, а отменить это он мог только
 * второй кнопкой, о которой не знал. `Sheet` даёт шаг подтверждения без
 * перехода на другую страницу (§8.3, `JoinRequestSheet`).
 *
 * **Серверное поведение НЕ менялось.** `joinCard`/`leaveCard` из
 * `@/lib/queries/cards` вызываются как раньше, с теми же аргументами: это
 * перекраска и добавление состояний, а не новая механика. Подача заявки в
 * режиме `capacity = 1` (`POST /cards/{uuid}/requests` с обязательным полем
 * `message`) на вебе по-прежнему отсутствует — см. долг L-DS3-1 в отчёте
 * волны: это поведение продукта, а не цвет, и решается владельцем.
 *
 * **Почему клиентский компонент, а не серверный.** Действие живёт по нажатию:
 * серверный компонент не умеет ждать `fetch` пользователя. Список заявившихся
 * и карточка остаются серверными — здесь только кнопка и панель.
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

import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
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
  /**
   * §8.3: `offline` — кнопка блокируется с пояснением.
   *
   * Запись при отсутствии связи заведомо провалится, и разрешать её значило бы
   * обещать действие, которого не будет. Чтение при этом не ограничивается
   * (§8.4 п.5) — карточку можно читать.
   *
   * Необязательный пропс с безопасным умолчанием `false`: существующие вызовы
   * (`app/(app)/app/cards/[uuid]/page.tsx`) продолжают компилироваться без правок.
   */
  offline?: boolean;
};

/** Состояние панели заявки: до отправки, в полёте, успех, отказ. */
type SheetState =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "success" }
  | { kind: "error"; code: string };

/**
 * Панель подтверждения присоединения (§8.3, `JoinRequestSheet`).
 *
 * Состояния по матрице:
 * - `error` → inline-сообщение под полем, **ввод сохраняется** (§8.4 п.3);
 * - `success` → карточка «Заявка отправлена» `role="status"`;
 * - `offline` → кнопка заблокирована с пояснением.
 *
 * Тост на успех не используется сознательно: `ToastProvider` не смонтирован
 * в дереве приложения (проверено grep по `app/` и `components/`). Вызвать
 * `toast()` без провайдера — уронить рендер. Поэтому успех показан карточкой
 * в самой панели, а подключение провайдера записано долгом L-DS3-2.
 */
export function JoinRequestSheet({
  uuid,
  open,
  onOpenChange,
  offline = false,
  onDone,
}: {
  uuid: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  offline?: boolean;
  /** Успех: экран перечитывает карточку (`router.refresh()`). */
  onDone?: () => void;
}) {
  const [state, setState] = useState<SheetState>({ kind: "idle" });

  // Успех показывается в этой же панели; закрытие сбрасывает состояние,
  // иначе при следующем открытии человек увидит чужой «Заявка отправлена».
  function handleOpenChange(next: boolean) {
    if (!next) setState({ kind: "idle" });
    onOpenChange(next);
  }

  async function submit() {
    if (offline) return;
    setState({ kind: "sending" });
    const result = await joinCard(uuid);
    if (!result.ok) {
      // §8.4 п.3: ввод не теряется — сообщение живёт РЯДОМ с содержимым панели,
      // а не вместо него. Панель остаётся открытой.
      setState({ kind: "error", code: result.code });
      return;
    }
    setState({ kind: "success" });
    onDone?.();
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent side="bottom">
        <SheetHeader>
          <SheetTitle>Присоединиться к встрече?</SheetTitle>
          <SheetDescription>
            Автор увидит, что вы в компании. Отменить участие можно в любой момент.
          </SheetDescription>
        </SheetHeader>

        {state.kind === "success" ? (
          // §8.3: success — карточка «Заявка отправлена». `role="status"`,
          // а не `alert`: это не тревога.
          <div
            role="status"
            className="flex flex-col gap-1 rounded-md bg-success-tint px-4 py-3"
          >
            <p className="text-base font-medium text-success">Заявка отправлена</p>
            <p className="text-sm text-ink-secondary">
              Вы в компании. Список участников обновится через мгновение.
            </p>
          </div>
        ) : null}

        {state.kind === "error" ? (
          // §8.3, §8.4 п.3: inline под полем, ввод сохранён. §2.3: `danger`
          // здесь законен — это сбой действия, а не штатное состояние карточки.
          <p role="alert" className="text-sm text-danger">
            {errorText(state.code)}
          </p>
        ) : null}

        {offline ? (
          // §8.3: offline — кнопка блокируется с пояснением. Пояснение — рядом
          // с кнопкой, а не вместо неё: человек должен понять, почему не выйдет.
          <p className="text-sm text-ink-muted">
            Нет связи. Присоединиться можно будет, когда соединение вернётся.
          </p>
        ) : null}

        <SheetFooter>
          {state.kind === "success" ? (
            <Button type="button" variant="primary" onClick={() => handleOpenChange(false)}>
              Готово
            </Button>
          ) : (
            <>
              <Button
                type="button"
                variant="primary"
                loading={state.kind === "sending"}
                disabled={offline || state.kind === "sending"}
                disabledReason={offline ? "Нет связи" : undefined}
                onClick={() => void submit()}
              >
                Присоединиться
              </Button>
              <Button
                type="button"
                variant="secondary"
                disabled={state.kind === "sending"}
                onClick={() => handleOpenChange(false)}
              >
                Отмена
              </Button>
            </>
          )}
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}

export function JoinActions({ uuid, participates, joinable, offline = false }: JoinActionsProps) {
  const router = useRouter();
  // `pending` блокирует повторный клик: двойное нажатие отправило бы второй
  // `POST`, а он на сервере НЕ идемпотентен (`ALREADY_JOINED`), и пользователь
  // увидел бы ошибку на действии, которое только что прошло.
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  // Ни одного действия не показываем, когда оно бессмысленно: карточка
  // собрана/закрыта — «Присоединиться» вело бы в гарантированный `409`.
  if (!participates && !joinable) return null;

  async function leave() {
    if (pending) return;
    setPending(true);
    setError(null);
    const result = await leaveCard(uuid);
    setPending(false);
    if (!result.ok) {
      setError(errorText(result.code));
      return;
    }
    // AC-09: после успеха экран перечитывает карточку — статус, места и
    // признак участия приходят с сервера, а не из локального состояния.
    router.refresh();
  }

  if (participates) {
    return (
      <div className="flex flex-col gap-2">
        <Button
          type="button"
          variant="secondary"
          className="self-start"
          loading={pending}
          disabled={pending || offline}
          disabledReason={offline ? "Нет связи" : undefined}
          onClick={() => void leave()}
        >
          Отменить участие
        </Button>
        {error ? (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <Button
        type="button"
        variant="primary"
        className="self-start"
        disabled={offline}
        disabledReason={offline ? "Нет связи" : undefined}
        onClick={() => setSheetOpen(true)}
      >
        Присоединиться
      </Button>
      {error ? (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      ) : null}
      <JoinRequestSheet
        uuid={uuid}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        offline={offline}
        onDone={() => {
          // Панель закрывается по кнопке «Готово», а карточка перечитывается
          // сразу: к моменту закрытия данные уже свежие.
          router.refresh();
        }}
      />
    </div>
  );
}
