"use client";

/**
 * Действия на карточке: присоединение к открытому набору, подача заявки
 * (`capacity = 1`) и отмена участия (стори 5б.2, 11.5, 11.6; FR83, FR104,
 * FR76; AC-1..AC-4 стори 11.6).
 *
 * **Два режима — два числа шагов (П-2, AC-5 стори 11.6).** Открытый набор
 * (`capacity` 2–6) — **одно касание**: «Присоединиться» сразу зовёт
 * `joinCard(uuid)`, панели нет. Заявка (`capacity = 1`) — **два шага**: кнопка
 * «Подать заявку» открывает панель с формой, где `message` обязателен и выбор
 * автора необратим (§7.5, §8.4 п.6).
 *
 * **Почему разделение, а не единый шаг подтверждения.** До 11.6 было
 * наоборот: шаг подтверждения стоял на обратимом действии (участие отменяется
 * «Отменить участие» на той же карточке — FR104), а на необратимом (заявка)
 * его не было — форма висела инлайн прямо на карточке. Это нарушение П-2,
 * зафиксированное `ds-wave-3-cards.md` R-2; владелец выбрал разделение
 * режимов, а не «убрать панель везде» или «оставить везде».
 *
 * **Серверное поведение НЕ менялось.** `joinCard`/`leaveCard`/
 * `submitJoinRequest`/`cancelJoinRequest` из `@/lib/queries/cards` вызываются
 * как раньше, с теми же аргументами.
 *
 * **Почему клиентский компонент, а не серверный.** Действие живёт по нажатию:
 * серверный компонент не умеет ждать `fetch` пользователя. Список заявившихся
 * и карточка остаются серверными — здесь только кнопки и панель.
 *
 * **Без оптимистичного статуса** (D-5b.2-4, X-43, §8.4 п.6): WS нет, и
 * «мгновенно покажем, что вы в компании» разошлось бы с БД при отказе. После
 * успеха — `router.refresh()`: сервер отдаёт уже посчитанные
 * `viewer_participates`, `viewer_requested` и `filled_count`. Одно касание ≠
 * оптимистичный статус: запрос уходит сразу, ответ — перечитывание.
 *
 * **Коды ошибок — машинные** (`code`), тексты выбираются здесь: сервер не
 * знает, на каком экране показывается отказ (project-context §4). Слова
 * «вас не выбрали» и подобные запрещены (UX §636, FR81): отказ должен быть
 * безличным.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { JoinRequestForm } from "@/components/cards/join-request-form";
import { cancelJoinRequest, joinCard, leaveCard } from "@/lib/queries/cards";

/** Тексты отказов по машинному коду — безличные формулировки (UX §636). */
const ERROR_TEXT: Record<string, string> = {
  CARD_IS_FULL: "Свободных мест больше нет",
  CARD_NOT_ACCEPTING: "Набор закрыт",
  ALREADY_JOINED: "Вы уже в этой компании",
  PARTICIPATION_NOT_FOUND: "Вы не в этой компании",
  CAPACITY_ONE_USES_REQUESTS: "Здесь собирают компанию по заявкам",
  // AC-5 стори 11.5: выбранную заявку отменить нельзя — это уже участие.
  // Формулировка безличная (FR81): про решение автора ни слова.
  REQUEST_SELECTED: "Заявка уже принята — отменить её нельзя",
  REQUEST_NOT_ACTIVE: "Заявки уже нет: она истекла или отменена",
  // CR 11.5 MED: серверный код — `request_not_found` (`join_request_service.py:135`),
  // в нижнем регистре. Без него параллельно убранная заявка давала бы
  // «Не удалось выполнить действие» и приглашала жать мёртвую кнопку.
  request_not_found: "Заявки уже нет — обновите страницу",
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
   * AC-1, AC-7 стори 11.5 (FR70): вместимость выбирает РЕЖИМ действия.
   *
   * `1` — режим заявок: панель с формой (`JoinRequestForm`), затем «Моя
   * заявка». `2..6` — открытый набор: одно касание и `joinCard` (FR83).
   *
   * **Обязателен с 12.4** (`W-11.5-c`). До этого тип был `capacity?: number`
   * с умолчанием `2` в деструктуризации: забытый проп молча уводил карточку
   * `capacity=1` в открытый набор, и пользователь видел «Присоединиться»,
   * получая `CAPACITY_ONE_USES_REQUESTS` без способа подать заявку.
   *
   * Замер 12.4: единственный вызывающий (`app/(app)/app/cards/[uuid]/page.tsx`)
   * пропс уже передаёт, поэтому обязательность ничего не ломает — она лишь
   * делает ошибку компиляции вместо тихого неверного поведения.
   */
  capacity: number;
  /** AC-2 стори 11.5: зритель подал заявку, и она ещё `pending`. */
  requested?: boolean;
  /**
   * AC-5 стори 11.5 (FR82): `uuid` своей заявки — адрес отмены.
   *
   * `null`/отсутствие при `requested` означает «заявка есть, но отменить
   * нечего»: кнопка отмены не показывается. Такого состояния сервер не
   * отдаёт (`viewer_requested` и `viewer_request_uuid` приходят одной
   * строкой), но контракт клиента не должен на него падать.
   */
  requestUuid?: string | null;
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

/**
 * Панель ЗАЯВКИ (`capacity = 1`, §7.5, §8.3).
 *
 * **Только режим заявки, и только форма.** У открытого набора шага нет
 * (`AC-1` стори 11.6): там одно касание, и эта панель из той ветки не
 * рендерится вообще. Имя "Sheet" осталось историческим — переименование в
 * `JoinRequestPanel` дало бы 0 поведения (долг `W-11.6-a`).
 *
 * **Состояний успеха и ошибки здесь больше нет.** Раньше панель звала
 * `joinCard` и показывала «Заявка отправлена», хотя заявки не подавала — имя
 * врало о поведении (тот же класс, что 11.3). Теперь успех виден на карточке
 * («Моя заявка», сервер отдаёт `viewer_requested=true`), а отказы живут в
 * самой форме рядом с полем (§8.4 п.3: ввод сохраняется).
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
  /** Успех: панель закрывается, экран перечитывает карточку (§7.5). */
  onDone?: () => void;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      {/* AC-3.1: `Sheet` — Radix, фокус-ловушка, `Escape`, возврат фокуса.
          С нуля не пишем (§7.5). */}
      <SheetContent side="bottom">
        <SheetHeader>
          <SheetTitle>Заявка на встречу</SheetTitle>
          <SheetDescription>
            Автор прочитает сообщение и решит сам. Отменить заявку можно в любой момент.
          </SheetDescription>
        </SheetHeader>
        {/* AC-3.1: форма — существующий компонент как есть (197 строк, фиксы
            CR 11.5 H-1/H-3 внутри). Не переписывать. */}
        <JoinRequestForm
          uuid={uuid}
          offline={offline}
          onDone={() => {
            // AC-3.3: успешная отправка закрывает панель; карточку
            // перечитывает экран, и «Моя заявка» приходит с сервера.
            onOpenChange(false);
            onDone?.();
          }}
        />
      </SheetContent>
    </Sheet>
  );
}

export function JoinActions({
  uuid,
  participates,
  joinable,
  capacity,
  requested = false,
  requestUuid = null,
  offline = false,
}: JoinActionsProps) {
  const router = useRouter();
  // `pending` блокирует повторный клик: двойное нажатие отправило бы второй
  // `POST`, а он на сервере НЕ идемпотентен (`ALREADY_JOINED`), и пользователь
  // увидел бы ошибку на действии, которое только что прошло (AC-1.2).
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Флаг называет режим: у открытого набора панели нет вообще (AC-1.1),
  // и общий `sheetOpen` держал бы для него переменную, всегда равную `false`.
  const [requestSheetOpen, setRequestSheetOpen] = useState(false);
  // Отмена ЗАЯВКИ — отдельное состояние от `pending` участия: у заявки и
  // участия разные маршруты и разные отказы (`REQUEST_SELECTED` против
  // `PARTICIPATION_NOT_FOUND`). Общий флаг заблокировал бы обе кнопки сразу.
  const [cancelling, setCancelling] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);

  // AC-2, AC-6 стори 11.5: режим заявок — СВОЯ ветка, до общей проверки
  // `joinable`. Причина: у поданной заявки действие остаётся (отмена), даже
  // когда карточка уже не принимает новые заявки. Общая проверка ниже
  // вернула бы `null` и отняла бы у человека возможность отменить заявку.
  const requestMode = capacity === 1;

  async function cancelRequest() {
    if (cancelling || requestUuid === null) return;
    setCancelling(true);
    setRequestError(null);
    const result = await cancelJoinRequest(requestUuid);
    setCancelling(false);
    if (!result.ok) {
      setRequestError(errorText(result.code));
      // CR 11.5 MED: заявки уже нет (`selected`/истекла/отменена параллельно)
      // — состояние на экране устарело, и без перечитывания человек будет
      // жать мёртвую кнопку, каждый раз получая тот же отказ.
      if (
        result.code === "REQUEST_SELECTED" ||
        result.code === "REQUEST_NOT_ACTIVE" ||
        result.code === "request_not_found"
      ) {
        router.refresh();
      }
      return;
    }
    // AC-5 стори 11.5: лимит освободился — экран перечитывает карточку, и
    // `viewer_requested` приходит уже `false` с сервера.
    router.refresh();
  }

  /** Отмена УЧАСТИЯ (открытый набор и выбранная заявка режима заявок). */
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

  /**
   * AC-1.1 стори 11.6: присоединение открытого набора — ОДНО касание.
   *
   * Ни панели, ни второго подтверждения: участие обратимо (FR104,
   * «Отменить участие» на этой же карточке), и лишний шаг защищал бы от того,
   * что и так отменяется (П-2). `pending` — не шаг, а защита от двойного
   * `POST` (AC-1.2).
   */
  async function join() {
    if (pending) return;
    setPending(true);
    setError(null);
    const result = await joinCard(uuid);
    setPending(false);
    if (!result.ok) {
      // AC-1.4: отказ — инлайн под кнопкой, кнопка остаётся на месте.
      setError(errorText(result.code));
      return;
    }
    // AC-1.3: `viewer_participates` приходит с сервера, клиент не держит
    // второго представления ресурса.
    router.refresh();
  }

  if (requestMode) {
    // AC-2, AC-5 стори 11.5: заявка подана — «Моя заявка» вместо формы. Второй
    // подачи быть не может (уникальность пары «карточка + заявитель»), поэтому
    // форма и блок «Моя заявка» взаимоисключающие.
    if (requested) {
      return (
        <div className="flex flex-col gap-2">
          <p className="text-base font-medium text-ink">Моя заявка</p>
          {requestUuid !== null ? (
            <Button
              type="button"
              variant="secondary"
              className="self-start"
              loading={cancelling}
              disabled={cancelling || offline}
              disabledReason={offline ? "Нет связи" : undefined}
              onClick={() => void cancelRequest()}
            >
              Отменить заявку
            </Button>
          ) : null}
          {requestError ? (
            <p role="alert" className="text-sm text-danger">
              {requestError}
            </p>
          ) : null}
        </div>
      );
    }
    // CR 11.5 HIGH-2 (регрессия FR104): УЧАСТНИК режима заявок.
    //
    // Сценарий пилота: автора выбрал заявку → `selected` →
    // `viewer_participates=true`, `viewer_requested=false`, карточка `full` →
    // `joinable=false`. Без этой ветки выполнение уходило в `if (!joinable)
    // return null` ниже, и участник компании из одного человека НЕ МОГ
    // выйти из неё: кнопка исчезала, хотя сервер отмену допускает.
    //
    // Порядок важен (AC-2.2, AC-2.3 стори 11.6): `participates` проверяется ДО
    // `joinable`, потому что участие — состоявшееся действие, а `joinable` —
    // про приём НОВЫХ заявок.
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
    // AC-3.6: форма только пока карточка принимает заявки. Истёкшее окно —
    // `joinable=false`, и форма обещала бы действие, которое вернёт `409`.
    if (!joinable) return null;
    return (
      <div className="flex flex-col gap-2">
        {/* AC-3.2: подпись совпадает с лентой (`actionLabel`,
            `event-card.tsx:140-146` возвращает «Подать заявку» при
            `capacity === 1`). До 11.6 здесь стояло «Присоединиться» —
            расхождение одного действия на двух экранах. */}
        <Button
          type="button"
          variant="primary"
          className="self-start"
          disabled={offline}
          disabledReason={offline ? "Нет связи" : undefined}
          // AC-3.5: при `offline` кнопка недоступна и панель не открывается.
          onClick={() => setRequestSheetOpen(true)}
        >
          Подать заявку
        </Button>
        {/* AC-3.1: поля формы НЕТ на карточке до открытия панели. */}
        <JoinRequestSheet
          uuid={uuid}
          open={requestSheetOpen}
          onOpenChange={setRequestSheetOpen}
          offline={offline}
          onDone={() => router.refresh()}
        />
      </div>
    );
  }

  // Ни одного действия не показываем, когда оно бессмысленно: карточка
  // собрана/закрыта — «Присоединиться» вело бы в гарантированный `409`.
  if (!participates && !joinable) return null;

  // AC-2.1: `participates` проверяется ДО `!joinable` — выход из компании не
  // зависит от приёма новых (`full`/`closed`).
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
      {/* AC-1.1: одно касание. Панели здесь нет и быть не может. */}
      <Button
        type="button"
        variant="primary"
        className="self-start"
        loading={pending}
        // AC-1.2: `loading` держит `aria-busy`, `pending` не даёт второго
        // `POST` из обработчика; AC-1.5: `offline` блокирует с причиной.
        disabled={pending || offline}
        disabledReason={offline ? "Нет связи" : undefined}
        onClick={() => void join()}
      >
        Присоединиться
      </Button>
      {error ? (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
