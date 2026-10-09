"use client";

import HistoryPanel from "@/components/HistoryPanel";
import type { User } from "@/lib/api";

/**
 * 主对话页左栏（260px）：上方是账号信息 + 新建对话 + 关键词搜索 + 会话列表，
 * 底部固定一块用户信息 / 注销。
 *
 * 【为什么单独抽成组件】
 *   原来的左栏 Sidebar 里还塞了「运行状态 / 知识库统计 / MySQL 表结构」三块管理内容；
 *   按改造方案它们搬到了后台页 /admin，左栏只留「对话相关」的东西，因此单独拆出本组件。
 *
 * 【为什么不做数据请求】
 *   它自己不拉数据：账号信息由 page.tsx 传入，会话列表交给子组件 HistoryPanel 自己管。
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
};

export default function ConversationRail({
  user,
  historyEnabled,
  historyRefreshKey,
  onLoginClick,
  onLogout,
  onOpenConversation,
  onNewConversation,
}: Props) {
  return (
    <aside className="sidebar">
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
            <div style={{ marginTop: 8 }}>
              <button className="btn" type="button" onClick={onLogout}>
                注销
              </button>
            </div>
          </div>
        ) : (
          <div className="card">
            <div className="row">
              <span className="row-key">未登录</span>
              <span className="badge badge-plain">问答不保存</span>
            </div>
            <div style={{ marginTop: 8 }}>
              <button className="btn btn-primary" type="button" onClick={onLoginClick}>
                登录 / 注册
              </button>
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}