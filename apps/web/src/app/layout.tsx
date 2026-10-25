import type { Metadata } from "next";
import type { ReactNode } from "react";
import localFont from "next/font/local";
import "./globals.css";
import { Geist } from "next/font/google";
import { cn } from "@/lib/utils";

const geist = Geist({subsets:['latin'],variable:'--font-sans'});

/**
 * Golos Text — SIL OFL, полная кириллица, создан под русский (§3.1).
 *
 * `next/font/local`, а не ручной `@font-face`: Next сам считает хеш имени
 * файла, добавляет `preload` и `font-display`, и не отдаёт 404 после смены
 * содержимого файла (у ручного @font-face имя кешируется браузером навсегда).
 *
 * Три начертания из шкалы §3.4: 400 текст, 500 подписи/статусы, 600 время и
 * заголовки. 700 не используется — избыточен для продукта без акцентов.
 *
 * `display: "swap"` — FOIT запрещён, FOUT допускается (§3.1): текст виден
 * сразу системным стеком, затем подменяется. `preload: true` на 400 — оно
 * основное; 500/600 грузятся по потребности.
 */
const golos = localFont({
  src: [
    // Путь от этого файла (src/app/layout.tsx): ../../ = корень apps/web
    { path: "../../public/fonts/golos-text-400.woff2", weight: "400", style: "normal" },
    { path: "../../public/fonts/golos-text-500.woff2", weight: "500", style: "normal" },
    { path: "../../public/fonts/golos-text-600.woff2", weight: "600", style: "normal" },
  ],
  display: "swap",
  preload: true,
  variable: "--font-golos",
  // Фолбэк-метрики: без них подмена шрифта сдвигает текст (layout shift) —
  // системный стек и Golos Text разной ширины. next/font считает `size-adjust`
  // автоматически только для next/font/google, поэтому задаём вручную близкое
  // значение: Golos Text чуть уже системного Helvetica при том же кегле.
  fallback: ["system-ui", "Segoe UI", "Roboto", "Arial", "sans-serif"],
  adjustFontFallback: false,
});

/**
 * Метаданные корня.
 *
 * Стори волны 1: продукт — action-first (встречи по расписанию, ответ за
 * 2 секунды), а не «дейтинг с экономикой внимания». Формулировки ниже
 * предложены в отчёте волны 1 и подлежат подтверждению владельцем
 * (не менять молча — изменение зафиксировано явно).
 */
export const metadata: Metadata = {
  title: "Карамель — встречи вместо переписки",
  description:
    "Сервис встреч в Томске: карточка с временем и свободными местами вместо бесконечной ленты. Ответ — за две секунды. Пилот в Томске.",
  robots: {
    // Закрытая часть приложения не индексируется, публичные страницы — индексируются
    index: true,
    follow: true,
  },
};

/**
 * Inline-скрипт против FOUC (§17.7).
 *
 * HTML отдаётся до гидратации. Если тему ставить в React-эффекте, первый кадр
 * всегда светлый — на тёмной системе человек видит вспышку при КАЖДОМ
 * переходе между страницами. Скрипт выполняется синхронно в момент парсинга,
 * до любого CSS-зависимого контента.
 *
 * Почему `dangerouslySetInnerHTML`, а не `next/script`: `strategy="beforeInteractive"`
 * добавляет загрузку чанка, а inline-строка не требует сети вообще. Для 8 строк
 * это единственный вариант без сетевого шага.
 *
 * `try/catch` обязателен: `localStorage.getItem` бросает в sandboxed iframe и при
 * заблокированном хранилище. Падение инлайн-скрипта — белый экран.
 *
 * Приоритет ровно по §17.5: явный выбор > системная настройка > светлая.
 */
const themeScript = `
(function(){
  try {
    var stored = localStorage.getItem('karamel-theme');
    var theme = (stored === 'light' || stored === 'dark')
      ? stored
      : (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    var root = document.documentElement;
    root.setAttribute('data-theme', theme);
    root.style.colorScheme = theme;
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
`;

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    // suppressHydrationWarning (§17.7 п.2): сервер отдал без data-theme, клиент
    // поставил атрибут до гидратации — иначе React предупредит о расхождении.
    <html lang="ru" className={cn("h-full", "antialiased", golos.variable, "font-sans", geist.variable)} suppressHydrationWarning>
      <head>
        {/* §17.8: браузер узнаёт до загрузки CSS, что тема поддерживается —
            иначе первый кадр рисуется со светлым скроллбаром. */}
        <meta name="color-scheme" content="light dark" />
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
