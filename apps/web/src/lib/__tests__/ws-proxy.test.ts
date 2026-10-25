// @vitest-environment node
/**
 * Тест прокси `/ws` (chore P-7, X-6.5-4).
 *
 * **Почему `node`-окружение.** Прокси работает на уровне TCP: handshake —
 * обычный HTTP/1.1 с `Upgrade: websocket`, а после ответа `101` по тому же
 * сокету идут кадры. В jsdom нет ни `node:net`, ни настоящего `WebSocket`,
 * и тест проверял бы заглушки.
 *
 * **Почему поднимаются ДВА сервера.** Проверяется именно туннель: клиент
 * идёт на `/ws` прокси, прокси обязан открыть соединение к upstream и
 * переслать туда handshake с заменой пути на `/api/v1/ws`. Один сервер
 * доказал бы только то, что клиент умеет подключаться.
 *
 * **Что доказывает тест.** Заголовок `X-6.5-4`: браузер в проде не
 * подключался к WS, потому что Next не проксирует `Upgrade`. Здесь
 * соединение проходит через прокси-путь, а данные идут в ОБЕ стороны:
 * серверный кадр доходит до клиента, клиентский — до upstream.
 */

import { createHash } from "node:crypto";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

import { afterEach, describe, expect, it } from "vitest";

import { WS_UPSTREAM_PATH, attachWsProxy, isProxyPath } from "../../../server/ws-proxy.mjs";

/** GUID из RFC 6455: без него клиент отвергает handshake. */
const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/**
 * Минимальный upstream: отвечает на handshake и шлёт ОДИН текстовый кадр.
 *
 * Полноценный WebSocket-сервер здесь не нужен: прокси не разбирает кадры, и
 * содержательная логика upstream проверяла бы не прокси, а её саму.
 *
 * **Почему не эхо.** Клиентские кадры МАСКИРОВАНЫ (RFC 6455 §5.1), а сырое
 * эхо вернуло бы маску обратно — клиент отвергает маскированный серверный
 * кадр и рвёт соединение. Поэтому сервер шлёт свой немаркированный кадр, а
 * входящие байты только считает: их доставку проверяет вызывающий тест.
 */
function startUpstream(seenPaths: string[], incoming: number[]): Promise<Server> {
  const server = createServer((_req, res) => {
    res.writeHead(400).end();
  });
  server.on("upgrade", (req, socket) => {
    seenPaths.push(req.url ?? "");
    const key = String(req.headers["sec-websocket-key"] ?? "");
    const accept = createHash("sha1")
      .update(`${key}${WS_GUID}`)
      .digest("base64");
    socket.write(
      "HTTP/1.1 101 Switching Protocols\r\n" +
        "Upgrade: websocket\r\n" +
        "Connection: Upgrade\r\n" +
        `Sec-WebSocket-Accept: ${accept}\r\n\r\n`,
    );
    // `API → браузер`: FIN + text, длина 4 в 7-битной форме без extended length.
    socket.write(Buffer.concat([Buffer.from([0x81, 0x04]), Buffer.from("pong")]));
    // `браузер → API`: считаем пришедшие байты. Маску снимает приложение, а не
    // прокси — здесь важно, что байты вообще доехали.
    socket.on("data", (chunk: Buffer) => incoming.push(chunk.length));
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

const started: Server[] = [];
const clients: WebSocket[] = [];

afterEach(async () => {
  for (const client of clients.splice(0)) {
    try {
      client.close();
    } catch {
      // уже закрыт
    }
  }
  // `close(cb)` здесь НЕ ожидается: туннель держит исходящий сокет, который
  // `closeAllConnections` не учитывает (он про входящие), и callback может не
  // прийти никогда — тест падал бы таймаутом хука вместо проверки прокси.
  // Ожидание закрытия не является предметом теста: важно, что порты
  // освобождаются, а `close()` это гарантирует.
  for (const server of started.splice(0)) {
    server.closeAllConnections?.();
    server.close();
  }
  await new Promise((resolve) => setTimeout(resolve, 50));
});

describe("isProxyPath", () => {
  it("принимает ровно /api/v1/ws и отвергает всё остальное", () => {
    // Путь совпадает с тем, что строит `ChatScreen`: менять публичный контракт
    // клиента ради внутренней детали прокси не нужно.
    expect(isProxyPath("/api/v1/ws")).toBe(true);
    expect(isProxyPath("/api/v1/ws?x=1")).toBe(true);
    // HMR Next идёт по своему пути и обязан остаться за Next: иначе
    // `npm run dev` теряет перезагрузку.
    expect(isProxyPath("/_next/webpack-hmr")).toBe(false);
    expect(isProxyPath("/ws")).toBe(false);
    expect(isProxyPath("/api/v1/wsx")).toBe(false);
    expect(isProxyPath("/api/v1/ws/extra")).toBe(false);
    expect(isProxyPath("")).toBe(false);
    // `new URL` нормализует `..`: `/api/v1/x/../ws` ДЕЙСТВИТЕЛЬНО ведёт в
    // `/api/v1/ws`, а `/api/v1/ws/../secret` — уже нет.
    expect(isProxyPath("/api/v1/x/../ws")).toBe(true);
    expect(isProxyPath("/api/v1/ws/../secret")).toBe(false);
  });
});

describe("attachWsProxy", () => {
  it("туннелирует /ws в /api/v1/ws и пересылает данные в обе стороны", async () => {
    const seenPaths: string[] = [];
    const incoming: number[] = [];
    const upstream = await startUpstream(seenPaths, incoming);
    started.push(upstream);
    const upstreamPort = (upstream.address() as AddressInfo).port;

    const proxy = createServer((_req, res) => res.writeHead(404).end());
    attachWsProxy(proxy, { apiUrl: `http://127.0.0.1:${upstreamPort}` });
    await new Promise<void>((resolve) => proxy.listen(0, "127.0.0.1", () => resolve()));
    started.push(proxy);
    const proxyPort = (proxy.address() as AddressInfo).port;

    // Настоящий клиент WebSocket (Node 22) — тот же протокол, что у браузера.
    const received = await new Promise<string>((resolve, reject) => {
      const client = new WebSocket(`ws://127.0.0.1:${proxyPort}/api/v1/ws`);
      clients.push(client);
      const timer = setTimeout(() => reject(new Error("handshake не завершился")), 5000);
      client.onopen = () => {
        // Клиентский кадр маскируется по RFC — прокси пересылает байты как есть.
        client.send("ping");
      };
      client.onmessage = (event: MessageEvent) => {
        clearTimeout(timer);
        resolve(String(event.data));
      };
      client.onerror = () => {
        clearTimeout(timer);
        reject(new Error("клиент не подключился через прокси"));
      };
    });

    // `API → браузер`: серверный кадр доехал через туннель.
    expect(received).toBe("pong");
    // Прокси обязан обратиться к РЕАЛЬНОМУ маршруту API. Литерал, а не
    // константа: сравнение с `WS_UPSTREAM_PATH` прошло бы и после правки
    // самой константы — то есть не проверяло бы ничего (§18.2).
    expect(seenPaths).toEqual(["/api/v1/ws"]);
    // `браузер → API`: кадр клиента (маскированный) доехал до upstream.
    // Достаточно факта ненулевой доставки: разбор кадров — работа API.
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(incoming.length).toBeGreaterThan(0);
  });
});
