/**
 * Системное сообщение чата компании (стори 6.7, AC-04; UX §9).
 *
 * **Зачем отдельный компонент.** Системное сообщение — не реплика: у него нет
 * автора, его нельзя удалить, и оно описывает ФАКТ («участник отменил
 * участие»), а не речь человека. Отрисовка через обычный пузырь выдала бы ему
 * аватар и кнопку удаления, которых у него быть не может (UX §9: «нейтральный
 * тон, серый фон, без иконки предупреждения»).
 *
 * **Центрирование и `role="status"`.** NFR57/UX: системная строка читается как
 * подпись события, а не как сообщение сбоку. `role="status"` объявляет её
 * вспомогательным технологиям как пояснение, а не как элемент переписки.
 *
 * **Только текст.** Проп `body` вставляется как текст React'ом, не как HTML:
 * подстановка `dangerouslySetInnerHTML` сделала бы шаблон каналом XSS, а
 * серверный шаблон — единственный источник текста (D-6.7-5).
 */

type SystemMessageProps = {
  /** Текст системного сообщения. Формируется СЕРВЕРОМ, клиент его не сочиняет. */
  body: string;
  /** Время события в ISO 8601 UTC: показывается как «19:34». */
  createdAt: string;
};

/** ISO 8601 UTC → «19:34» в локальной зоне читателя. */
function timeOf(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

export function SystemMessage({ body, createdAt }: SystemMessageProps) {
  return (
    <li className="flex flex-col items-center gap-1">
      <p
        role="status"
        className="max-w-[90%] rounded-md bg-[#F2EDE6] px-3 py-2 text-center text-sm leading-relaxed text-[#5A5045]"
      >
        {body}
      </p>
      <span className="text-xs text-[#5A5045]">{timeOf(createdAt)}</span>
    </li>
  );
}
