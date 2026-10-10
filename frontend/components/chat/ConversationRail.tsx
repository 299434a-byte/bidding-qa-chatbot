"use client";

import Icon from "@/components/ui/icons";
import HistoryPanel from "@/components/HistoryPanel";
import type { User } from "@/lib/api";

/**
 * 主对话页左栏：账号信息 + 新建对话 + 关键词搜索 + 会话列表 + 底部登录状态。
 * 移动端（≤768px）转为抽屉：靠 open 控制滑入，配一层遮罩。
 */

type Props = {
  /** 当前登录用户（未登录为 null） */
  user: User | null;
  /** 后端是否开启对话历史 */
  historyEnabled: boolean;
  /** 历史面板刷新信号（对话落库后自增） */
  historyRefreshKey: number;
  /** 打开登录 / 注册弹窗 */
  onLoginClick: () => void;
  /** 注销 */
  onLogout: () => void;
  /** 打开某个历史会话并定位到指定消息 */
  onOpenConversation: (id: string, messageId?: string) => void;
  /** 新建对话 */
  onNewConversation: () => void;
  /** 移动端抽屉是否展开 */
  open?: boolean;
  /** 关闭移动端抽屉 */
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
      {/* 移动端抽屉遮罩：桌面不渲染（CSS 里 display:none） */}
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

        {/* 底部：用户信息 / 注销 */}
        <div className="rail-user">
          {user ? (
            <div className="card">
              <div className="row">
                <span className="row-key">已登录</span>
                <span className="badge badge-mint">{user.username}</span>
              </div>
              <button className="btn" type="button" onClick={onLogout} style={{ marginTop: "var(--sp-8)" }}>
                <Icon name="x" size={14} />
                注销
              </button>
            </div>
          ) : (
            <div className="card">
              <div className="row">
                <span className="row-key">未登录</span>
                <span className="badge badge-plain">问答不保存</span>
              </div>
              {/* 次级描边按钮：不与底部的发送键抢焦点 */}
              <button className="btn" type="button" onClick={onLoginClick} style={{ marginTop: "var(--sp-8)" }}>
                登录 / 注册
              </button>
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
