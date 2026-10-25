"use client";

/**
 * Фотографии анкеты: статусы модерации, добавление первого фото и замена
 * отклонённого (Task 9.2–9.8; AC-01, AC-03, AC-05, AC-07).
 *
 * D-9 стори 2.4: новых маршрутов нет. Добавление и замена — это существующие
 * `POST /profiles/me/photos` (`201`) и `DELETE /profiles/me/photos/{uuid}`
 * (`204`). Поэтому здесь нет «экрана загрузки»: форма раскрывается по месту.
 *
 * AC-03: `url = null` у `pending` — норма, а не ошибка. Клиент рисует
 * заглушку «На проверке», а не битую картинку и не сообщение о сбое.
 *
 * AC-05: причина отказа приходит только владельцу и показывается рядом
 * с заглушкой. В логи не пишется (FR58) — здесь тоже: `console.*` нет.
 *
 * D-10 стори 2.3: запросы идут через BFF (`/api/proxy/...`), чтобы cookie
 * ставились на той же origin, где открыта страница.
 */

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  PHOTO_TILE_HEIGHT,
  PhotoPlaceholder,
  type PhotoPlaceholderState,
} from "@/components/profile/photo-placeholder";

/** Максимальный размер файла на клиенте — подсказка, не защита (Task 9.8). */
export const MAX_PHOTO_BYTES = 5_242_880;

type PhotoStatus = "pending" | "approved" | "rejected";

/** Фото в том виде, в каком его отдаёт `PhotoOwn` (AC-01). */
export type PhotoView = {
  uuid: string;
  position: number;
  status: PhotoStatus;
  /** `null` для `pending`: изображение не выдаётся (AC-03). */
  url: string | null;
  expires_in: number;
  content_type: string;
  /** Причина отказа; только у `rejected` и только владельцу (AC-05). */
  rejection_reason: string | null;
  created_at: string;
  moderated_at: string | null;
};

/** Коды отказов API, переведённые в текст (паттерн `ERRORS`, Task 9.6). */
const ERRORS: Record<string, string> = {
  photo_limit_reached: "Уже три фото — сначала удалите одно из них.",
  photo_not_image: "Это не изображение. Нужен JPEG, PNG или WebP.",
  photo_empty: "Файл пустой. Выберите другое фото.",
  photo_consent_required:
    "Нужно подтвердить, что это ваше фото и вы согласны на показ другим пользователям.",
  request_too_large: "Файл больше 5 МБ. Сожмите его или выберите другое.",
  storage_unavailable: "Хранилище недоступно. Попробуйте через минуту.",
  gender_required: "Укажите пол в анкете — без него фото не принимается.",
  unauthenticated: "Сессия истекла. Войдите снова.",
  photo_not_found: "Фото уже удалено.",
};

function messageFor(code: string | undefined, fallback: string): string {
  if (!code) return fallback;
  return ERRORS[code] ?? fallback;
}

/** Состояние заглушки по статусу фото (UX §16). */
function placeholderFor(photo: PhotoView): PhotoPlaceholderState {
  if (photo.status === "pending") return "pending";
  if (photo.status === "rejected") return "rejected";
  // `approved` без `url` — сбой подписи (AC-03): сервер отдал фото, но ссылку
  // собрать не удалось. Показываем «Фото нет», а не падаем.
  return "no-photo";
}

export function ProfilePhotos({
  hasProfile,
  initialPhotos,
}: {
  hasProfile: boolean;
  /**
   * Фото, прочитанные на СЕРВЕРЕ (Task 9.2).
   *
   * `null` — запрос не удался; `[]` — фото действительно нет (AC-12).
   * Различие принципиально: сбой сети не должен выглядеть как «фото
   * пропали».
   */
  initialPhotos: PhotoView[] | null;
}) {
  const router = useRouter();

  // CR 2.4 патч 6: ошибка ЧТЕНИЯ списка выводится из пропса на каждом рендере,
  // а не живёт в `useState`. Инициализатор `useState` срабатывает только при
  // монтировании, поэтому после `router.refresh()` с валидными данными
  // сообщение «Не удалось загрузить фото» оставалось на экране навсегда.
  const loadError = initialPhotos === null ? "Не удалось загрузить фото. Обновите страницу." : null;
  const [actionError, setActionError] = useState<string | null>(null);
  const error = actionError ?? loadError;

  const photos = initialPhotos ?? [];
  const [busy, setBusy] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [replacing, setReplacing] = useState<string | null>(null);

  // CR 2.4 патч 7: подписанная ссылка живёт `expires_in` (900 с), а страница —
  // серверный компонент, отрисованный один раз. Без обновления через 15 минут
  // все `<img>` превращались в битые картинки. Таймер обновляет страницу
  // заранее (за минуту до истечения) — данные снова приходят с сервера.
  let minTtl: number | null = null;
  for (const photo of photos) {
    if (photo.status === "approved" && photo.url) {
      minTtl = minTtl === null ? photo.expires_in : Math.min(minTtl, photo.expires_in);
    }
  }

  useEffect(() => {
    if (minTtl === null) return;
    const delay = Math.max((minTtl - 60) * 1000, 5_000);
    const timer = setTimeout(() => router.refresh(), delay);
    return () => clearTimeout(timer);
  }, [minTtl, router]);

  async function postPhoto(file: File, consent: boolean): Promise<Response> {
    const form = new FormData();
    form.append("file", file);
    form.append("consent", consent ? "true" : "false");
    return fetch("/api/proxy/profiles/me/photos", {
      method: "POST",
      body: form,
      credentials: "same-origin",
      cache: "no-store",
    });
  }

  async function deletePhoto(uuid: string): Promise<Response> {
    return fetch(`/api/proxy/profiles/me/photos/${uuid}`, {
      method: "DELETE",
      credentials: "same-origin",
      cache: "no-store",
    });
  }

  /**
   * Замена файла существующего фото — `PUT /me/photos/{uuid}` (Task 8.1).
   *
   * Стори 2.5, D-1: `uuid` и `position` сохраняются на сервере, статус уходит
   * в `pending`. Ветка «`409` → `DELETE` → повторный `POST`» (CR 2.4 патч 5)
   * удалена: между двумя запросами фото физически отсутствовало, а при отказе
   * второго шага пользователь оставался без фото вовсе.
   */
  async function putPhoto(uuid: string, file: File, consent: boolean): Promise<Response> {
    const form = new FormData();
    form.append("file", file);
    form.append("consent", consent ? "true" : "false");
    return fetch(`/api/proxy/profiles/me/photos/${uuid}`, {
      method: "PUT",
      body: form,
      credentials: "same-origin",
      cache: "no-store",
    });
  }

  async function readCode(response: Response): Promise<string | undefined> {
    const body = (await response.json().catch(() => null)) as { code?: string } | null;
    return body?.code;
  }

  /**
   * Загрузка НОВОГО фото (Task 8.1 стори 2.5).
   *
   * Ветка «`409` → `DELETE` → повторный `POST`» удалена вместе с путём
   * замены через удаление: замена теперь `PUT` одной строкой (D-1), поэтому
   * лимит в три фото не может помешать — позиция занята тем же фото.
   * `409` здесь означает реальный предел галереи и показывается как есть.
   */
  async function submitPhoto(file: File, consent: boolean) {
    setBusy("new");
    setActionError(null);

    // Task 9.8: клиентский предел — подсказка. Сервер проверяет фактический
    // размер тела и сигнатуру, и отказ придёт доменным кодом.
    if (file.size > MAX_PHOTO_BYTES) {
      setBusy(null);
      setActionError(ERRORS.request_too_large ?? null);
      return;
    }

    try {
      const uploaded = await postPhoto(file, consent);

      if (!uploaded.ok) {
        setActionError(messageFor(await readCode(uploaded), "Не удалось загрузить фото."));
        return;
      }

      setAdding(false);
      // Новый список приходит с сервера: страница — серверный компонент,
      // и `refresh()` перерисовывает её с актуальными фото.
      router.refresh();
    } catch {
      setActionError("Не удалось загрузить фото. Проверьте соединение.");
    } finally {
      setBusy(null);
    }
  }

  /**
   * Замена файла существующего фото (Task 8.1, AC-02).
   *
   * Один запрос вместо двух: `uuid` и `position` сохраняет сервер, фото
   * ни на мгновение не исчезает из карточки. После успеха сервер вернул
   * `pending` — заглушку «На проверке» рисует существующий `PhotoPlaceholder`.
   */
  async function replacePhoto(uuid: string, file: File, consent: boolean) {
    setBusy(uuid);
    setActionError(null);

    if (file.size > MAX_PHOTO_BYTES) {
      setBusy(null);
      setActionError(ERRORS.request_too_large ?? null);
      return;
    }

    try {
      const replaced = await putPhoto(uuid, file, consent);
      if (!replaced.ok) {
        // `404` при замене легален: фото удалили в другой вкладке. Список
        // всё равно обновляем — иначе пользователь видит несуществующее фото.
        if (replaced.status === 404) {
          router.refresh();
          return;
        }
        setActionError(messageFor(await readCode(replaced), "Не удалось заменить фото."));
        return;
      }

      setReplacing(null);
      router.refresh();
    } catch {
      setActionError("Не удалось заменить фото. Проверьте соединение.");
    } finally {
      setBusy(null);
    }
  }

  /**
   * Удаление фото (Task 8.2).
   *
   * Вызывается только там, где действие разрешено правилом AC-06:
   * `pending` — всегда, `approved` — только когда одобренных больше одного.
   */
  async function removePhoto(uuid: string) {
    setBusy(uuid);
    setActionError(null);

    try {
      const deleted = await deletePhoto(uuid);
      // `404` — фото уже удалили в другой вкладке; список обновляем.
      if (!deleted.ok && deleted.status !== 404) {
        setActionError(messageFor(await readCode(deleted), "Не удалось удалить фото."));
        return;
      }
      router.refresh();
    } catch {
      setActionError("Не удалось удалить фото. Проверьте соединение.");
    } finally {
      setBusy(null);
    }
  }

  // AC-06 стори 2.5: удаление одобренного фото разрешено, только когда
  // одобренных больше одного — иначе анкета осталась бы без показываемых
  // изображений. Считается по данным сервера, не по локальному стейту.
  let approvedCount = 0;
  for (const photo of photos) {
    if (photo.status === "approved") approvedCount += 1;
  }

  const canAdd = hasProfile && photos.length < 3 && adding === false;

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-medium">Фотографии</h2>
        <p className="text-sm leading-relaxed text-ink-secondary">
          Фото проверяет модератор. До проверки его видите только вы.
        </p>
      </div>

      {error ? (
        <p className="text-sm leading-relaxed text-danger" role="alert">
          {error}
        </p>
      ) : null}

      {photos.length === 0 ? (
        <PhotoPlaceholder state="no-photo" />
      ) : (
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {photos.map((photo) => (
            <li key={photo.uuid} className="flex flex-col gap-2">
              {photo.status === "approved" && photo.url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={photo.url}
                  alt={`Фото ${photo.position}`}
                  className={`${PHOTO_TILE_HEIGHT} w-full rounded-xl object-cover`}
                />
              ) : (
                <PhotoPlaceholder
                  state={placeholderFor(photo)}
                  reason={photo.status === "rejected" ? photo.rejection_reason : null}
                />
              )}

              {/* AC-06 стори 2.5: «Заменить» есть у `approved` и `rejected`,
                  «Удалить» — у `pending`. У `approved` удаление доступно,
                  только когда одобренных больше одного: единственное
                  одобренное фото можно заменить, но не удалить, иначе
                  анкета потеряет все показываемые изображения. */}
              {replacing === photo.uuid ? (
                <PhotoForm
                  submitLabel="Заменить"
                  disabled={busy === photo.uuid}
                  onCancel={() => setReplacing(null)}
                  onSubmit={(file, consent) => void replacePhoto(photo.uuid, file, consent)}
                />
              ) : photo.status === "pending" ? (
                <button
                  type="button"
                  onClick={() => void removePhoto(photo.uuid)}
                  disabled={busy !== null}
                  className="min-h-11 rounded-lg bg-surface px-3 py-2 text-sm font-medium text-ink disabled:opacity-60"
                >
                  Удалить
                </button>
              ) : (
                <div className="flex flex-col gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setActionError(null);
                      setReplacing(photo.uuid);
                    }}
                    disabled={busy !== null}
                    className="min-h-11 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-ink disabled:opacity-60"
                  >
                    Заменить
                  </button>
                  {photo.status === "approved" && approvedCount > 1 ? (
                    <button
                      type="button"
                      onClick={() => void removePhoto(photo.uuid)}
                      disabled={busy !== null}
                      className="min-h-11 rounded-lg bg-surface px-3 py-2 text-sm font-medium text-ink disabled:opacity-60"
                    >
                      Удалить
                    </button>
                  ) : null}
                  {photo.status === "approved" && approvedCount <= 1 ? (
                    <p className="text-sm leading-relaxed text-ink-muted">
                      Сначала загрузите новое фото
                    </p>
                  ) : null}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {/* CR 2.4 патч 9: добавление ПЕРВОГО фото. До правки единственный
          `input[type=file]` жил внутри «Заменить» у отклонённого фото, поэтому
          пользователь с пустой галереей не мог загрузить ничего вообще:
          видел «Фото нет» и не имел элемента управления. */}
      {adding ? (
        <PhotoForm
          submitLabel="Загрузить"
          disabled={busy === "new"}
          onCancel={() => setAdding(false)}
          onSubmit={(file, consent) => void submitPhoto(file, consent)}
        />
      ) : null}

      {canAdd ? (
        <button
          type="button"
          onClick={() => {
            setActionError(null);
            setAdding(true);
          }}
          disabled={busy !== null}
          className="min-h-11 self-start rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-ink disabled:opacity-60"
        >
          Добавить фото
        </button>
      ) : null}

      {!hasProfile ? (
        <p className="text-sm leading-relaxed text-ink-muted">
          Фото можно добавить после заполнения анкеты.
        </p>
      ) : null}

      {hasProfile && photos.length >= 3 && !adding ? (
        <p className="text-sm leading-relaxed text-ink-muted">
          Три фото — предел анкеты.
        </p>
      ) : null}
    </section>
  );
}

/**
 * Форма выбора файла и согласия (Task 9.4, FR64).
 *
 * CR 2.4 патч 10: форма — ОТДЕЛЬНЫЙ компонент, который размонтируется при
 * закрытии. Раньше состояние жило в родителе, и после «Отмена» → «Заменить»
 * галочка согласия и выбранный ранее файл сохранялись: согласие на показ
 * фото другим людям переиспользовалось без повторного действия пользователя,
 * хотя это юридически значимое волеизъявление, а не настройка интерфейса.
 */
function PhotoForm({
  submitLabel,
  disabled,
  onCancel,
  onSubmit,
}: {
  submitLabel: string;
  disabled: boolean;
  onCancel: () => void;
  onSubmit: (file: File, consent: boolean) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [consent, setConsent] = useState(false);

  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (file && consent) onSubmit(file, consent);
      }}
    >
      <input
        type="file"
        accept="image/jpeg,image/png,image/webp"
        onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        className="text-sm"
        aria-label="Новое фото"
      />
      <label className="flex items-start gap-2 text-sm leading-relaxed">
        <input
          type="checkbox"
          checked={consent}
          onChange={(event) => setConsent(event.target.checked)}
          className="mt-1"
        />
        <span>Это моё фото, и я согласен на показ другим пользователям.</span>
      </label>
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={!file || !consent || disabled}
          className="min-h-11 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-ink disabled:opacity-60"
        >
          {submitLabel}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={disabled}
          className="min-h-11 rounded-lg bg-surface px-3 py-2 text-sm font-medium text-ink disabled:opacity-60"
        >
          Отмена
        </button>
      </div>
    </form>
  );
}
