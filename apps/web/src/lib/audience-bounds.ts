/**
 * Рамки аудитории карточки: одно правило для формы и для ленты.
 * (Стори 12.6, AC-1a/AC-2; CR 12.6 H-6/H-7, Task 7.1–7.3.)
 *
 * **Почему отдельный модуль.** Правило «рамки заданы» и правило «пара
 * границ не перевёрнута» нужны в двух местах — форма создания и карточка
 * ленты. Копия в каждом компоненте уже дала дефект на бэкенде (CR 12.6 H-6:
 * `has_audience_bounds` был скопирован строкой в `card_decision_service`, и
 * копия не знала про режим заявок). Один модуль — один риск.
 *
 * **Мягкий фильтр (FR81, ADR-19).** Рамки никого не отсекают: карточка
 * видна всем, заявку подать может любой, а признак «ты вне рамок» сервер
 * отдаёт ТОЛЬКО автору (AC-4). Поэтому здесь нет функции «подходит ли
 * зритель» — только подпись того, кого ждёт автор.
 */

import { z } from "zod";

/**
 * Допустима ли пара возрастных границ.
 *
 * Полуоткрытые пары легальны (AC-1a): «от 30», «до 35» — отсутствие одной
 * границы не ошибка. Перевёрнутая пара (`40/30`) — ошибка формы, а не
 * серверный отказ: сервер вернул бы `422 age_range_invalid`, но показывать
 * отказ API там, где ошибка видна сразу, значит гонять человека по кругу.
 */
export function isAgeRangeValid(min?: number | null, max?: number | null): boolean {
  if (min === undefined || min === null) return true;
  if (max === undefined || max === null) return true;
  return min <= max;
}

/** Сообщение формы для перевёрнутой пары. Одно на оба поля (UX `2а`). */
export const AGE_RANGE_MESSAGE = "Нижняя граница больше верхней";

/**
 * Zod-схема пары границ (Task 7.2).
 *
 * Вынесена из `formSchema`, чтобы её можно было проверить без рендера формы:
 * валидная пара, перевёрнутая и полуоткрытая — три случая, которые обязаны
 * различаться (P-21: без пары «валидная/перевёрнутая» правило `<=` не отличить
 * от константы).
 */
export const ageRangeFields = {
  age_min: z.number().int().min(18, "Не младше 18").max(99, "Не старше 99").optional(),
  age_max: z.number().int().min(18, "Не младше 18").max(99, "Не старше 99").optional(),
};

/**
 * Навешивает проверку пары на ЛЮБУЮ форму с `age_min`/`age_max`.
 *
 * Хелпер, а не копия `refine` в форме: правило `<=` живёт здесь одно, и
 * форма создания получает его, не повторяя условие (тот же урок, что H-6 на
 * бэкенде: копия правила в `card_decision_service` не знала про режим заявок).
 */
export function withAgeRange<T extends z.ZodType<{ age_min?: number; age_max?: number }>>(
  schema: T,
) {
  return schema.refine(
    (values) => isAgeRangeValid(values.age_min, values.age_max),
    { message: AGE_RANGE_MESSAGE, path: ["age_min"] },
  );
}

/** Схема пары — для проверки правила без формы. */
export const ageRangeSchema = withAgeRange(z.object(ageRangeFields));

/** Форма карточки в той части, которая нужна подписи рамок. */
export type AudienceBounds = {
  gender_filter: "female" | "male" | null;
  age_min: number | null;
  age_max: number | null;
};

/**
 * Подпись рамок для ленты (Task 7.3).
 *
 * Возвращает `null`, если рамок нет: «ищу любого» — это отсутствие
 * ограничения, а не намерение автора, и писать его вместо пустоты значило бы
 * добавлять шум в каждую карточку (UX §1048 «только если задано»).
 *
 * Тон констатации: подпись говорит, кого ждёт автор, но не оценивает
 * зрителя. «Вы не подходите» здесь сказать нельзя — фильтр мягкий (FR81).
 */
export function audienceBoundsText(card: AudienceBounds): string | null {
  const gender =
    card.gender_filter === "female"
      ? "женщин"
      : card.gender_filter === "male"
        ? "мужчин"
        : null;
  const min = card.age_min;
  const max = card.age_max;

  // Полуоткрытые пары легальны (AC-1a): «от 30», «до 35», «25–35».
  const age =
    min !== null && max !== null
      ? `${min}–${max}`
      : min !== null
        ? `от ${min}`
        : max !== null
          ? `до ${max}`
          : null;

  if (gender !== null && age !== null) return `Ищет ${gender}, ${age} лет`;
  if (gender !== null) return `Ищет ${gender}`;
  if (age !== null) return `Ищет возраст: ${age}`;
  return null;
}
