import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // cacheComponents включает Cache Components в Next 16.
  // partialPrefetching не является документированной опцией Next 16 — удалён,
  // иначе ключ молча игнорируется и создаёт ложное впечатление оптимизации.
  cacheComponents: true,

  // Стенд запускает web в dev-режиме (Dockerfile: npm run dev) и открывается
  // через SSH-туннель на 127.0.0.1:3000. Next 16 по умолчанию блокирует
  // cross-origin запросы к своим dev-ресурсам (/_next/hmr и чанки) — в браузере
  // это выглядит как негидратированная страница: форма входа делала нативный
  // submit и перезагружала страницу вместо перехода на шаг ввода кода.
  // Симптом был диагностирован 2026-10-25: formReact=[] при живом CSS.
  // Список — только хосты, с которых реально открывают стенд.
  allowedDevOrigins: ["127.0.0.1", "localhost", "129.101.113.246"],
};

export default nextConfig;
