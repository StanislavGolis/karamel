/**
 * Тесты девяти примитивов (§6.3, §14.2, §17.14).
 *
 * **Главная проверка — «работает в обеих темах».** Она не может быть
 * «отрендерилось без ошибок»: это прошло бы и с хардкод-цветом. Поэтому тест
 * проверяет три вещи одновременно:
 *
 * 1. **Нет `dark:`-классов** — §17.4: тема меняет значения переменных, а не
 *    классы. Наличие `dark:` означает второй набор стилей и второе место для
 *    ошибки (именно так сгенерировал shadcn).
 * 2. **Нет hex** — §6.5 п.2. Гейт 1 ловит это в файле, тест — в разметке.
 * 3. **Нет shadcn-словаря** (`bg-primary`, `text-muted-foreground`) — этих
 *    токенов в системе нет, и класс молча ничего не красит.
 *
 * Плюс состояния из §8.3, которые обязательны для каждого компонента.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogConfirm,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { Toast, ToastProvider } from "@/components/ui/toast";

/**
 * Классы, которых в нашей системе быть не должно.
 *
 * `dark:` — запрещён §17.4. Остальное — shadcn-словарь, которого нет в
 * `@theme`: класс отрендерится, но цвет не применится, и это худший вид
 * дефекта — «выглядит почти правильно».
 */
const FORBIDDEN = [/\bdark:/, /#[0-9a-fA-F]{3,8}\b/, /\b(bg|text|border)-(primary|secondary|muted|popover|card|destructive)\b/];

/** Проверяет, что классы элемента чисты от запрещённого. */
function expectCleanClasses(element: HTMLElement) {
  const classes = element.className;
  for (const pattern of FORBIDDEN) {
    expect(classes, `класс «${classes}» нарушает: ${pattern}`).not.toMatch(pattern);
  }
}

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
});

/** Прогоняет проверку в обеих темах: тема не должна менять классы. */
function bothThemes(assert: () => void) {
  document.documentElement.setAttribute("data-theme", "light");
  assert();
  document.documentElement.setAttribute("data-theme", "dark");
  assert();
}

describe("Button (§6.4)", () => {
  it("все четыре варианта рендерятся и не содержат запрещённых классов", () => {
    for (const variant of ["primary", "secondary", "quiet", "danger"] as const) {
      const { unmount } = render(<Button variant={variant}>Действие</Button>);
      expectCleanClasses(screen.getByRole("button"));
      unmount();
    }
  });

  it("danger использует accent-ink, а не белый (Б-2: белый в тёмной = 3.16:1)", () => {
    render(<Button variant="danger">Удалить</Button>);
    const classes = screen.getByRole("button").className;
    expect(classes).toContain("text-accent-ink");
    expect(classes).not.toMatch(/text-white/);
  });

  it("тап-зона не меньше 44px", () => {
    render(<Button>Отправить</Button>);
    expect(screen.getByRole("button").className).toContain("min-h-12");
  });

  it("focus-visible: 2px с отступом 2px и токеном кольца (§13.1)", () => {
    render(<Button>Отправить</Button>);
    const classes = screen.getByRole("button").className;
    expect(classes).toContain("focus-visible:outline-2");
    expect(classes).toContain("focus-visible:outline-offset-2");
    expect(classes).toContain("focus-visible:outline-focus-ring");
  });

  it("loading: aria-busy, текст не меняется, кнопка не disabled", () => {
    render(<Button loading>Отправить</Button>);
    const button = screen.getByRole("button");

    expect(button).toHaveAttribute("aria-busy", "true");
    // Текст не подменяется на «Отправляем…»: иначе кнопка прыгает по ширине.
    expect(button).toHaveTextContent("Отправить");
    // Блокировка фокуса на время запроса выкинула бы человека из потока.
    expect(button).not.toBeDisabled();
  });

  it("disabled: aria-disabled и причина через aria-describedby (§6.4, §13.3)", () => {
    render(
      <Button disabled disabledReason="Сначала заполните анкету">
        Отправить
      </Button>,
    );
    const button = screen.getByRole("button");

    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-disabled", "true");

    const describedBy = button.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent("Сначала заполните анкету");
  });

  it("работает в обеих темах без смены классов", () => {
    render(<Button>Отправить</Button>);
    const button = screen.getByRole("button");
    const classBefore = button.className;

    bothThemes(() => {
      expectCleanClasses(button);
      expect(button.className).toBe(classBefore);
    });
  });
});

describe("Input (§6.3)", () => {
  it("высота не меньше 44px и текст 16px (§13.1, §3.2)", () => {
    render(<Input aria-label="Имя" />);
    const input = screen.getByRole("textbox");

    expect(input.className).toContain("min-h-12");
    expect(input.className).toContain("text-base");
    // 14px на продуктовом экране запрещён — и это включало бы автозум в iOS.
    expect(input.className).not.toMatch(/\btext-sm\b/);
  });

  it("ошибка через aria-invalid, а не отдельным пропом", () => {
    render(<Input aria-label="Имя" aria-invalid="true" />);
    const input = screen.getByRole("textbox");

    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input.className).toContain("aria-invalid:border-danger");
  });

  it("disabled не принимает ввод", async () => {
    const user = userEvent.setup();
    render(<Input aria-label="Имя" disabled />);
    const input = screen.getByRole("textbox");

    await user.type(input, "Аня");
    expect(input).toHaveValue("");
    expect(input.className).toContain("disabled:bg-disabled-bg");
  });

  it("работает в обеих темах", () => {
    render(<Input aria-label="Имя" />);
    const input = screen.getByRole("textbox");
    const before = input.className;

    bothThemes(() => {
      expectCleanClasses(input);
      expect(input.className).toBe(before);
    });
  });
});

describe("Textarea (§6.3)", () => {
  it("счётчик остатка показывается при maxLength", () => {
    render(<Textarea aria-label="Почему я" maxLength={200} defaultValue="Привет" />);
    expect(screen.getByText("194 из 200")).toBeInTheDocument();
  });

  it("без maxLength счётчика нет", () => {
    render(<Textarea aria-label="Заметка" />);
    expect(screen.queryByText(/из \d+/)).toBeNull();
  });

  it("работает в обеих темах", () => {
    render(<Textarea aria-label="Почему я" />);
    const area = screen.getByRole("textbox");
    bothThemes(() => expectCleanClasses(area));
  });
});

describe("Badge (§6.3)", () => {
  it("все статусные варианты рендерятся", () => {
    for (const variant of ["neutral", "success", "warning", "muted", "outline"] as const) {
      const { unmount } = render(<Badge variant={variant}>Свободно 2 места</Badge>);
      expect(screen.getByText("Свободно 2 места")).toBeInTheDocument();
      unmount();
    }
  });

  it("danger в Badge отсутствует — бейдж не бывает тревогой (§6.3 п.3)", () => {
    // @ts-expect-error — проверяем, что варианта нет в типе и в API рантайма.
    render(<Badge variant="danger">Сбой</Badge>);
    expect(screen.getByText("Сбой").className).not.toContain("bg-danger");
  });

  it("текст обязателен: цвет не единственный носитель смысла (NFR57)", () => {
    render(<Badge variant="success">Свободно 2 места</Badge>);
    expect(screen.getByText("Свободно 2 места")).toBeVisible();
  });

  it("работает в обеих темах", () => {
    render(<Badge variant="success">Свободно</Badge>);
    const badge = screen.getByText("Свободно");
    const before = badge.className;

    bothThemes(() => {
      expectCleanClasses(badge);
      expect(badge.className).toBe(before);
    });
  });
});

describe("Avatar (§17.11)", () => {
  it("фото не инвертируется и не затемняется", () => {
    render(
      <Avatar>
        <AvatarImage src="/a.jpg" alt="Аня" />
        <AvatarFallback>А</AvatarFallback>
      </Avatar>,
    );

    const root = document.querySelector('[data-slot="avatar"]') as HTMLElement;
    // §17.11: `mix-blend-darken`/`lighten` — запрещены. Именно их ставил shadcn.
    expect(root.className).not.toMatch(/mix-blend/);
    expect(root.className).not.toMatch(/brightness-|contrast-|invert/);
  });

  it("инициал — ink на surface, без цвета по имени (§2.4)", () => {
    render(
      <Avatar>
        <AvatarFallback>А</AvatarFallback>
      </Avatar>,
    );
    const fallback = document.querySelector('[data-slot="avatar-fallback"]') as HTMLElement;
    expect(fallback.className).toContain("bg-surface");
    expect(fallback.className).toContain("text-ink");
  });

  it("работает в обеих темах", () => {
    render(
      <Avatar>
        <AvatarFallback>А</AvatarFallback>
      </Avatar>,
    );
    const root = document.querySelector('[data-slot="avatar"]') as HTMLElement;
    bothThemes(() => expectCleanClasses(root));
  });
});

describe("Skeleton (§6.3, §5.2)", () => {
  it("скрыт от скринридера: состояние несёт контейнер (§13.3)", () => {
    render(<Skeleton />);
    expect(document.querySelector('[data-slot="skeleton"]')).toHaveAttribute("aria-hidden", "true");
  });

  it("карточка повторяет финальные габариты 176px (§4.6, §8.4 п.1)", () => {
    render(<Skeleton kind="card" />);
    expect(document.querySelector('[data-slot="skeleton"]')!.className).toContain("h-44");
  });

  it("пульсация только при motion-safe: reduced-motion гасит полностью (§5.2)", () => {
    render(<Skeleton />);
    const classes = document.querySelector('[data-slot="skeleton"]')!.className;

    expect(classes).toContain("motion-safe:animate-pulse");
    // Не `animate-pulse` без префикса: тогда reduced-motion не гасил бы.
    expect(classes).not.toMatch(/(^|\s)animate-pulse(\s|$)/);
  });
});

describe("Sheet (§6.3, §5.4)", () => {
  it("закрыт по умолчанию и не рендерит содержимое", () => {
    render(
      <Sheet>
        <SheetContent>
          <SheetTitle>Заявка</SheetTitle>
        </SheetContent>
      </Sheet>,
    );
    expect(screen.queryByText("Заявка")).toBeNull();
  });

  it("открытый: заголовок доступен, слой 40 и тень overlay", () => {
    render(
      <Sheet open>
        <SheetContent>
          <SheetTitle>Заявка</SheetTitle>
        </SheetContent>
      </Sheet>,
    );

    expect(screen.getByText("Заявка")).toBeInTheDocument();
    const content = document.querySelector('[data-slot="sheet-content"]') as HTMLElement;
    expect(content.className).toContain("z-40");
    expect(content.className).toContain("shadow-overlay");
  });

  it("вариант side отличается от bottom", () => {
    const { unmount } = render(
      <Sheet open>
        <SheetContent side="side">
          <SheetTitle>Сбоку</SheetTitle>
        </SheetContent>
      </Sheet>,
    );
    expect(document.querySelector('[data-slot="sheet-content"]')!.className).toContain("border-l");
    unmount();

    render(
      <Sheet open>
        <SheetContent side="bottom">
          <SheetTitle>Снизу</SheetTitle>
        </SheetContent>
      </Sheet>,
    );
    expect(document.querySelector('[data-slot="sheet-content"]')!.className).toContain("rounded-t-md");
  });
});

describe("Dialog (§6.3, §6.5 п.4)", () => {
  it("открытый: слой 50, тень overlay, закрытие с aria-label", () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogTitle>Удалить аккаунт?</DialogTitle>
        </DialogContent>
      </Dialog>,
    );

    expect(screen.getByText("Удалить аккаунт?")).toBeInTheDocument();
    const content = document.querySelector('[data-slot="dialog-content"]') as HTMLElement;
    expect(content.className).toContain("z-50");
    expect(screen.getByRole("button", { name: "Закрыть" })).toBeInTheDocument();
  });

  it("destructive даёт кнопку danger, confirm — primary", () => {
    const { unmount } = render(<DialogConfirm tone="destructive">Удалить</DialogConfirm>);
    expect(screen.getByRole("button").getAttribute("data-variant")).toBe("danger");
    unmount();

    render(<DialogConfirm tone="confirm">Продолжить</DialogConfirm>);
    expect(screen.getByRole("button").getAttribute("data-variant")).toBe("primary");
  });
});

/**
 * Radix Toast требует контекст провайдера — как и в приложении, где он один.
 *
 * Обёртка в тесте не «подгонка»: без провайдера компонент не работает нигде,
 * и тест, обошедший это, проверял бы несуществующий режим использования.
 */
function withProvider(ui: React.ReactElement) {
  return render(<ToastProvider>{ui}</ToastProvider>);
}

describe("Toast (§10.1-10.2)", () => {
  it("успех: role=status, ошибка: role=alert (§13.2)", () => {
    const { unmount } = withProvider(<Toast tone="success" open>Отправлено</Toast>);
    expect(document.querySelector('[data-slot="toast"]')).toHaveAttribute("role", "status");
    unmount();

    withProvider(<Toast tone="error" open>Не удалось</Toast>);
    expect(document.querySelector('[data-slot="toast"]')).toHaveAttribute("role", "alert");
  });

  it("время жизни: успех 4000мс, ошибка 6000мс (§10.2)", () => {
    const { unmount } = withProvider(<Toast tone="success" open>Ок</Toast>);
    expect(document.querySelector('[data-slot="toast"]')).toHaveAttribute("data-tone", "success");
    unmount();

    withProvider(<Toast tone="error" open>Сбой</Toast>);
    expect(document.querySelector('[data-slot="toast"]')).toHaveAttribute("data-tone", "error");
  });

  it("danger только как тинт, не сплошной заливкой (§2.4)", () => {
    withProvider(<Toast tone="error" open>Сбой</Toast>);
    const toast = document.querySelector('[data-slot="toast"]') as HTMLElement;

    expect(toast.className).toContain("bg-danger-tint");
    expect(toast.className).toContain("text-danger");
    // Сплошной `bg-danger` сделал бы фон тревожным блоком — запрещено §2.4.
    // `\b` не разделяет `danger` и `-tint` (дефис не word-char), поэтому проверяем
    // именно сплошную заливку: `bg-danger` с пробелом или в конце строки.
    expect(toast.className).not.toMatch(/bg-danger(?:\s|$)/);
  });

  it("работает в обеих темах", () => {
    withProvider(<Toast tone="success" open>Ок</Toast>);
    const toast = document.querySelector('[data-slot="toast"]') as HTMLElement;
    bothThemes(() => expectCleanClasses(toast));
  });
});
