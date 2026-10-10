import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// Next 官方规则集（core-web-vitals：React hooks、可访问性、next/router 使用等
// + typescript：TS 层面的 eslint 规则）。Next 16 起 next lint 子命令已移除，
// 直接用 eslint CLI 跑同一套规则（package.json 的 lint 脚本）。
const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // 构建产物与生成文件不进 lint。
  globalIgnores(["node_modules/**", ".next/**", "out/**", "build/**", "next-env.d.ts"]),
]);

export default eslintConfig;
