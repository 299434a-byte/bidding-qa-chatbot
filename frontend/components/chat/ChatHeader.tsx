"use client";

import Link from "next/link";
import type { ChatMode } from "@/lib/api";

/**
 * 聊天区顶部的极简标题栏：左侧 logo + 「招投标智能问答」+ 当前推理模式小标签，
 * 右侧一个「控制台」入口跳到后台页 /admin。
 *
 * 【为什么单独抽成组件】
 *   原先这里塞了一整排「知识库 9164 块 / PostgreSQL 已连接 / 图谱已连接 / 检索策略」
 *   大徽标，属于系统状态信息，用户希望它们搬到后台页；主对话页只留最轻的一行标题。
 *   抽成组件后，page.tsx 只需传一个 mode。
 */

/** 推理模式 -> 展示文案 */
const MODE_LABEL: Record<ChatMode, string> = {
  react: "ReAct",
  plan: "Plan-and-Solve",
  deep: "深度思考",
};

export default function ChatHeader({ mode }: { mode: ChatMode }) {
  return (
    <header className="topbar">
      <div className="topbar-title chat-head">
        <span className="chat-head-logo">📑</span>
        招投标智能问答
        <span className="badge badge-blue">{MODE_LABEL[mode]}</span>
      </div>
      <Link className="chat-head-link" href="/admin">
        控制台
      </Link>
    </header>
  );
}