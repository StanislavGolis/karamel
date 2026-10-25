/**
 * Custom server Next с прокси `/ws` (chore P-7, X-6.5-4).
 *
 * **Почему этот файл существует.** Next не проксирует `Upgrade` ни через
 * `rewrites`, ни через route handlers. Единственная точка перехвата в Next 16
 * — `nextServer.getUpgradeHandler()`, и её отдаёт только сервер, который сам
 * создал Next. Поэтому `npm run dev`/`start` запускают ЭТОТ файл, а не
 * `next dev` напрямую.
 *
 * **Что здесь и чего здесь нет.** Только маршрутизация `Upgrade`: путь `/ws`
 * уходит в туннель (см. `server/ws-proxy.mjs`), всё остальное — штатному
 * обработчику Next (HMR в dev идёт по WS и обязан остаться рабочим).
 * HTTP-обработка не дублируется: `getRequestHandler()` — ровно тот же
 * обработчик, что поднимает `next dev`.
 */

import { createServer } from "node:http";

import next from "next";

import { attachWsProxy } from "./server/ws-proxy.mjs";

const dev = process.env.NODE_ENV !== "production";
const hostname = process.env.HOSTNAME ?? "0.0.0.0";
const port = Number(process.env.PORT ?? 3000);

const app = next({ dev, hostname, port });
const handle = app.getRequestHandler();

await app.prepare();

const server = createServer((req, res) => {
  void handle(req, res);
});

// `API_URL` читается в момент запроса: в образе его меняют без пересборки
// (тот же довод, что в `api/proxy/[...path]/route.ts`).
attachWsProxy(server, {
  apiUrl: () => process.env.API_URL ?? "http://127.0.0.1:8000",
  // HMR и любые будущие WS-маршруты Next остаются за Next: иначе
  // `npm run dev` теряет перезагрузку и правки не доезжают до браузера.
  fallback: app.getUpgradeHandler(),
});

server.listen(port, hostname, () => {
  console.log(JSON.stringify({ event: "web_listen", host: hostname, port, dev }));
});
