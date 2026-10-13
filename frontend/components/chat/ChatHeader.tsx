"use client";

import Link from "next/link";
import Icon from "@/components/ui/icons";
import ThemeToggle from "@/components/ThemeToggle";
import type { ChatMode } from "@/lib/api";


const MODE_LABEL: Record<ChatMode, string> = {
  react: "ReAct",
  plan: "Plan-and-Solve",
  deep: "深度思考",
};

type Props = {
  mode: ChatMode;
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
