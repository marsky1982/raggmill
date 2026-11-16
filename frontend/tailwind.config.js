/** @type {import('tailwindcss').Config} */
// 色板全部由 src/index.css 的 @theme inline 提供（shadcn 语义名 → :root / .dark 变量）。
// 旧色板 base/panel/panel2/line/pri/pri2/txt/mut 已在 2026-11 全量改名为语义名并删除，勿再新增同名映射。
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["system-ui", "-apple-system", "Segoe UI", "PingFang SC", "Microsoft YaHei", "sans-serif"],
      },
    },
  },
  plugins: [],
};
