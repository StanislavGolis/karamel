# Прод-образ веба. Раньше здесь был `npm run dev` (заглушка «до стори 8.6»),
# из-за чего стенд работал в dev-режиме: Next 16 блокирует cross-origin запросы
# к своим dev-ресурсам (/_next/hmr и чанки), и при открытии стенда через caddy
# по домену карамель.рф страница приходила без гидратации — форма входа делала
# нативный submit и перезагружала страницу вместо шага ввода кода.
#
# Симптом подтверждён 2026-10-25 в логе web-контейнера:
#   ⚠ Blocked cross-origin request to Next.js dev resource /_next/hmr
#   from "www.xn--j1accp.xn--p1ai"
# allowedDevOrigins лечил симптом, но список пришлось бы пополнять при каждом
# новом способе доступа. Прод-сборка убирает сам механизм: dev-ресурсов нет.
FROM node:22-alpine

WORKDIR /app

# Зависимости отдельным слоем: пересборка кода не тянет npm ci заново.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Сборка на этапе образа. `server.mjs` читает NODE_ENV: при production
# next() поднимается в прод-режиме и отдаёт готовые чанки без HMR.
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

ENV NODE_ENV=production

EXPOSE 3000

CMD ["npm", "run", "start"]
