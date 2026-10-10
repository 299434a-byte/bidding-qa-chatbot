"use client";

import Link from "next/link";
import Icon from "@/components/ui/icons";
import ThemeToggle from "@/components/ThemeToggle";
import type { ChatMode } from "@/lib/api";

/**
 * 聊天区顶部标题栏：移动端菜单按钮 + 标题 + 当前推理模式标签，
 * 右侧主题切换与后台控制台入口。sticky + 半透明，内容不会透过顶栏。
 */

/** 推理模式 -> 展示文案 */
const MODE_LABEL: Record<ChatMode, string> = {
  react: "ReAct",
  plan: "Plan-and-Solve",
  deep: "深度思考",
};

type Props = {
  mode: ChatMode;
  /** 移动端打开左栏抽屉（桌面不显示该按钮） */
  onOpenRail?: () => void;
};

export default function ChatHeader({ mode, onOpenRail }: Props) {
  return (
    <header className="topbar">
      <div className="topbar-title">
        {onOpenRail && (
          <button className="icon-btn only-mobile" type="button" onClick={onOpenRail} aria-label="打开对话列表">
            <Icon name="menu" size={16} />
          </button>
        )}
        招投标智能问答
        <span className="badge badge-blue">{MODE_LABEL[mode]}</span>
      </div>
      <div className="topbar-actions">
        <ThemeToggle />
        <Link className="icon-btn" href="/admin" title="后台控制台" aria-label="后台控制台">
          <Icon name="panel" size={16} />
        </Link>
      </div>
    </header>
  );
}
