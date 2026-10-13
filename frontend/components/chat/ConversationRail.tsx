"use client";

import Icon from "@/components/ui/icons";
import HistoryPanel from "@/components/HistoryPanel";
import type { User } from "@/lib/api";


type Props = {
  user: User | null;
  historyEnabled: boolean;
  historyRefreshKey: number;
  onLoginClick: () => void;
  onLogout: () => void;
  onOpenConversation: (id: string, messageId?: string) => void;
  onNewConversation: () => void;
  open?: boolean;
  onClose?: () => void;
};

export default function ConversationRail({
  user,
  historyEnabled,
  historyRefreshKey,
  onLoginClick,
  onLogout,
  onOpenConversation,
  onNewConversation,
  open,
  onClose,
}: Props) {
  return (
    <>
      {}
      <div className={`rail-scrim ${open ? "open" : ""}`} onClick={onClose} role="presentation" />

      <aside className={`sidebar ${open ? "open" : ""}`}>
        <div className="rail-scroll">
          <div className="brand">
            <div className="brand-logo">标</div>
            <div>
              <div className="brand-title">招投标智能问答</div>
              <div className="brand-sub">对话历史</div>
            </div>
          </div>

          <div className="section">
            <HistoryPanel
              enabled={historyEnabled}
              user={user}
              onOpenConversation={onOpenConversation}
              onNewConversation={onNewConversation}
              refreshKey={historyRefreshKey}
            />
          </div>
        </div>

        {/* 底部：用户信息 / 注销（单行减重：状态一行放，小按钮贴右侧，不再套卡片占位） */}
        <div className="rail-user">
          {user ? (
            <div className="rail-user-row">
              <span className="badge badge-mint">{user.username}</span>
              <button className="act-btn" type="button" onClick={onLogout}>
                <Icon name="x" size={13} />
                注销
              </button>
            </div>
          ) : (
            <div className="rail-user-row">
              {/* 未登录的完整说明在历史区顶部已有，这里只留最轻的一行状态 */}
              <span className="rail-note">未登录</span>
              {/* 次级描边按钮：不与底部的发送键抢焦点 */}
              <button className="rail-login" type="button" onClick={onLoginClick}>
                登录 / 注册
              </button>
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
