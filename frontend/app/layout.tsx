import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "招投标智能问答机器人",
  description: "MySQL → Qdrant 混合检索（向量 + BM25）→ 大模型问答，答案附带参考来源",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}