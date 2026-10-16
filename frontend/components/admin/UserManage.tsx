"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type AdminUser, type User } from "@/lib/api";
import { shortTime } from "@/lib/timefmt";


type Props = {
  /** 当前登录用户；null = 未登录 */
  user: User | null;
  /** 后端判定的管理员身份（role=admin 或 AUTH_ADMIN_USERS 白名单） */
  isAdmin: boolean;
  /** 状态变更后通知外层刷新（例如更新登录态卡片） */
  onChanged?: () => void;
  onToast: (message: string, type: "success" | "error" | "info") => void;
};

export default function UserManage({ user, isAdmin, onChanged, onToast }: Props) {
  /** null = 还没加载；加载中与空列表由 error / items 区分，不额外设 loading 态 */
  const [items, setItems] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** 正在变更状态的用户 id（禁用其按钮，防止连点） */
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.adminUsers();
      setItems(res.items);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- setState 都在 load 的异步回调里完成，与 admin 页同一豁免口径
    if (isAdmin) void load();
  }, [isAdmin, load]);

  const toggle = async (u: AdminUser) => {
    const next = u.status === "disabled" ? "active" : "disabled";
    setBusyId(u.id);
    try {
      await api.adminSetUserStatus(u.id, next);
      onToast(next === "disabled" ? `已停用：${u.username}` : `已启用：${u.username}`, "success");
      await load();
      onChanged?.();
    } catch (e) {
      onToast(`操作失败：${e instanceof Error ? e.message : String(e)}`, "error");
    } finally {
      setBusyId(null);
    }
  };

  // 仅管理员可见可操作：未登录显示登录提示，登录但非管理员直接隐藏整张卡。
  if (!user) {
    return (
      <div className="admin-card">
        <div className="admin-card-title">账号管理</div>
        <div className="history-hint">登录后才能管理账号。请回到对话页登录。</div>
      </div>
    );
  }
  if (!isAdmin) return null;

  return (
    <div className="admin-card">
      <div className="admin-card-title">
        账号管理
        <span className="badge badge-plain">仅管理员可见</span>
      </div>

      {error && <div className="notice notice-danger">{error}</div>}
      {!error && items && items.length === 0 && <div className="history-hint">还没有任何账号。</div>}
      {!items && !error && <div className="history-hint">加载中…</div>}

      {items && items.length > 0 && (
        <div className="schema-table">
          <div className="schema-head" style={{ cursor: "default" }}>
            <span>用户名</span>
            <span>角色 / 状态</span>
            <span>最近登录</span>
            <span>操作</span>
          </div>
          <div className="schema-body">
            {items.map((u) => {
              const isSelf = u.id === user.id;
              const disabled = u.status === "disabled";
              return (
                <div className="col-row" key={u.id} style={{ alignItems: "center", justifyContent: "space-between" }}>
                  <span style={{ minWidth: 120 }}>
                    {u.username}
                    {isSelf && <span className="badge badge-plain" style={{ marginLeft: 6 }}>本人</span>}
                  </span>
                  <span style={{ minWidth: 140 }}>
                    <span className={`badge ${u.role === "admin" ? "badge-blue" : "badge-plain"}`}>{u.role}</span>{" "}
                    <span className={`badge ${disabled ? "badge-danger" : "badge-mint"}`}>
                      {disabled ? "已停用" : "正常"}
                    </span>
                  </span>
                  <span style={{ minWidth: 100, color: "var(--neutral-500)" }}>
                    {u.last_login_at ? shortTime(u.last_login_at) : "从未登录"}
                  </span>
                  <span>
                    <button
                      className={`btn ${disabled ? "" : "btn-danger"}`}
                      type="button"
                      disabled={busyId === u.id || isSelf}
                      title={isSelf ? "不能停用自己的账号" : undefined}
                      onClick={() => void toggle(u)}
                    >
                      {busyId === u.id ? "处理中…" : disabled ? "启用" : "停用"}
                    </button>
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="history-hint">
        停用后该账号立即无法登录、调用鉴权接口一律 403（已发出的令牌同时失效）。不能停用自己的账号。
      </div>
    </div>
  );
}
