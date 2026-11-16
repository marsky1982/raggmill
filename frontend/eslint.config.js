import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**", "coverage/**"],
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    // Playwright 端到端用例与配置：此前不在任何 files 模式里，eslint . 会静默跳过。
    // 跑在 Node 侧（process / __dirname 等），不涉及 React 规则。
    files: ["e2e/**/*.ts", "playwright.config.ts"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.node, ...globals.browser },
    },
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    // 测试文件会 import vitest 与 node 侧全局（process 等）
    files: ["src/**/*.test.{ts,tsx}", "src/test/**/*.{ts,tsx}"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
  {
    // shadcn 组件层按上游约定同时导出组件与 cva variants，不属于页面组件，
    // 关闭 fast-refresh 规则；页面/业务组件仍保留该规则。
    files: ["src/components/ui/**/*.tsx"],
    rules: {
      "react-refresh/only-export-components": "off",
    },
  },
  {
    // Context 模块按 React 官方示例同时导出 Provider 与 useTaskState（hook），
    // 拆文件只是搬运，暂按豁免处理。
    files: ["src/lib/TaskContext.tsx"],
    rules: {
      "react-refresh/only-export-components": "off",
    },
  },
  {
    // 历史遗留豁免：页面与业务组件里仍在用 Record<string, any> 承接后端动态 JSON
    // （KG properties / 质检报告等字段未定类型）。为过 lint 强行加类型属于另一笔改造，
    // 先仅在这些目录留口子；新增 lib/ 与 api.ts 仍禁止 any。
    files: ["src/pages/**/*.tsx", "src/components/**/*.tsx"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
);
