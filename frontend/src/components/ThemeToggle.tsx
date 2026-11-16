import { useEffect, useState } from "react";

/** 与 index.html 里的首屏脚本共用同一个键，保证刷新后主题不闪 */
const THEME_KEY = "ragas-theme";

type Theme = "light" | "dark";

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle("dark", theme === "dark");
  document.documentElement.style.colorScheme = theme;
}

/**
 * 深色 / 浅色切换（默认浅色）。放在侧栏底部：位置固定、不打断主流程，也不需要浮层。
 * 两套变量已定义在 index.css（:root 与 .dark），这里只切 class，不引入新色值。
 */
export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light"));

  useEffect(() => {
    applyTheme(theme);
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-pressed={theme === "dark"}
      title={`切换到${next === "dark" ? "深色" : "浅色"}主题`}
      className="w-full flex items-center justify-center gap-2 h-8 rounded-md border border-border text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <span aria-hidden>{theme === "dark" ? "☀" : "☾"}</span>
      {theme === "dark" ? "切换到浅色" : "切换到深色"}
    </button>
  );
}
