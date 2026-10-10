import type { Metadata } from "next";
import { THEME_INIT_SCRIPT } from "@/lib/theme";
import "./globals.css";

export const metadata: Metadata = {
  title: "招投标智能问答机器人",
  description: "MySQL → Qdrant 混合检索（向量 + BM25）→ 大模型问答，答案附带参考来源",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // suppressHydrationWarning：主题脚本会在 React 挂载前改写 <html data-theme>，属预期行为
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        {/* 在样式与内容渲染前落主题，避免暗色偏好用户看到一闪而过的白屏 */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}