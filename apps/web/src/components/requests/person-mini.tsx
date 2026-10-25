/**
 * Справка о человеке (UX §877, `PersonMini`; стори 5а.4, Task 9.1; AC-01).
 *
 * Аватар + имя + возраст. **Никогда** не показывает рейтинг, число встреч или
 * «надёжность» (UX §877): этих полей нет и в контракте API, и в компоненте
 * нет даже типа под них — то, чего нет, нельзя случайно отрисовать.
 *
 * `photo_url` — presigned URL из ответа (`photo_expires_in` сообщает срок
 * жизни). Ссылка протухает, поэтому нет `next/image`: оптимизатор кэшировал
 * бы подписанный адрес и через TTL отдавал битую картинку. `<img>` с
 * `loading="lazy"` не кэширует адрес дольше, чем живёт ссылка.
 *
 * Фото нет (нет анкеты, нет одобренного снимка, сбой подписи) → инициалы:
 * строка списка остаётся полезной, а не ломается (AC-06).
 */

type PersonMiniProps = {
  name: string | null;
  age: number | null;
  photoUrl: string | null;
};

/**
 * Инициалы из имени: «Анна Мария» → «АМ», «Анна» → «А».
 *
 * Пустое имя (анкеты нет) даёт «?» — не пустой кружок: пустой аватар читается
 * как «фото не загрузилось», хотя на самом деле нет анкеты.
 */
export function initialsOf(name: string | null): string {
  const parts = (name ?? "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const first = parts[0][0] ?? "";
  const second = parts.length > 1 ? (parts[1][0] ?? "") : "";
  return `${first}${second}`.toUpperCase();
}

/** «Анна, 28» — без возраста, если анкеты нет. */
export function personLabel(name: string | null, age: number | null): string {
  const base = name?.trim() ? name.trim() : "Без анкеты";
  return age === null ? base : `${base}, ${age}`;
}

export function PersonMini({ name, age, photoUrl }: PersonMiniProps) {
  return (
    <div className="flex items-center gap-3">
      {photoUrl ? (
        // eslint-disable-next-line @next/next/no-img-element -- подписанный URL протухает; см. docstring.
        <img
          src={photoUrl}
          alt=""
          width={48}
          height={48}
          loading="lazy"
          className="h-12 w-12 shrink-0 rounded-full object-cover"
        />
      ) : (
        <span
          aria-hidden="true"
          className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-[#F2EDE6] text-sm font-semibold text-[#7A3E12]"
        >
          {initialsOf(name)}
        </span>
      )}
      <span className="text-base font-medium text-[#2B241C]">{personLabel(name, age)}</span>
    </div>
  );
}
