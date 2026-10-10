"use client";

import { useState } from "react";
import Icon from "@/components/ui/icons";
import { api, setToken, type User } from "@/lib/api";

type Props = {
  /** 关闭弹窗 */
  onClose: () => void;
  /** 登录 / 注册成功（令牌已写入本地，回调里只需更新登录态） */
  onSuccess: (user: User) => void;
};

/** 登录 / 注册弹窗：两态切换，错误提示直接展示后端的 detail。 */
export default function AuthDialog({ onClose, onSuccess }: Props) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (busy) return;
    const name = username.trim();
    if (!name || !password) {
      setError("请填写用户名与口令");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = mode === "login" ? await api.login(name, password) : await api.register(name, password);
      setToken(res.token);
      onSuccess(res.user);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    // 点击遮罩关闭；点击卡片内部不关闭
    <div className="auth-overlay" onClick={onClose} role="presentation">
      <div className="auth-card" onClick={(e) => e.stopPropagation()}>
        <div className="auth-head">
          <div className="auth-title">{mode === "login" ? "登录" : "注册"}</div>
          <button className="icon-btn" type="button" onClick={onClose} title="关闭" aria-label="关闭">
            <Icon name="x" size={16} />
          </button>
        </div>

        {/* 登录 / 注册两态切换 */}
        <div className="auth-tabs">
          <button
            className={`seg-item ${mode === "login" ? "seg-active" : ""}`}
            type="button"
            onClick={() => {
              setMode("login");
              setError(null);
            }}
          >
            登录
          </button>
          <button
            className={`seg-item ${mode === "register" ? "seg-active" : ""}`}
            type="button"
            onClick={() => {
              setMode("register");
              setError(null);
            }}
          >
            注册
          </button>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <div className="auth-field">
            <label htmlFor="auth-username">用户名</label>
            <input
              id="auth-username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="3~32 位中英文、数字或下划线"
              autoComplete="username"
              autoFocus
            />
          </div>
          <div className="auth-field">
            <label htmlFor="auth-password">口令</label>
            <input
              id="auth-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 8 位"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
            />
          </div>

          {error && <div className="notice notice-danger">{error}</div>}

          <div className="auth-actions">
            <button className="btn btn-primary" type="submit" disabled={busy}>
              {busy ? "处理中…" : mode === "login" ? "登录" : "注册并登录"}
            </button>
            <button className="btn" type="button" onClick={onClose} disabled={busy}>
              取消
            </button>
          </div>
        </form>

        <div className="auth-hint">
          登录后每轮对话会自动保存，支持跨设备恢复、按关键词 / 时间 / 模式检索历史。
        </div>
      </div>
    </div>
  );
}