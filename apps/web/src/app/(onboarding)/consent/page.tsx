/**
 * Экран подтверждения 18+ и согласия на обработку ПД (Task 8; AC-02, AC-03, AC-06).
 *
 * D-1: подтверждение живёт после входа, отдельным экраном, а не на `/login`:
 * согласие привязано к учётной записи (`users`), а до входа учётки ещё нет.
 *
 * Гарда (сессия, уже данное согласие) — в layout этой route group (Task 8.2).
 */

import { ConsentForm } from "@/components/ui/consent-form";

export default function ConsentPage() {
  return (
    <main className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Подтверждение</h1>
        <p className="text-sm text-[#5A5249]">
          Два пункта и пол — этого достаточно, чтобы начать. Анкета появится в следующем
          обновлении.
        </p>
      </div>

      <ConsentForm />
    </main>
  );
}
