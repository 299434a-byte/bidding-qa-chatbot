"use client";

// ============================================================================
// 文件：app/memory/page.tsx —— 长期记忆看板
//
// 【这个页面整体在做什么】
//   展示当前登录用户的长期记忆画像：
//     · 关注实体标签云（字号按提及次数）——项目编号 / 法规名 / 企业名；
//     · 常问主题频次条；
//     · 最近提问时间线；
//   右上角「清空记忆」按钮走 DELETE /api/memory。
//   未登录 → 引导登录；记忆未启用（503）→ 明确说明，不当成错误闪红。
// ============================================================================

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import AuthDialog from "@/components/AuthDialog";
import ThemeToggle from "@/components/ThemeToggle";
import ToastStack, { useToasts } from "@/components/chat/Toast";
import Icon from "@/components/ui/icons";
import { api, getToken, type MemoryItem, type MemoryProfile, type User } from "@/lib/api";


/** 标签云字号：按次数在 [min,max] 之间线性放大，让"常问的"一眼可见 */
function tagSize(count: number, maxCount: number): number {
  if (maxCount <= 1) return 15;
  return 14 + Math.round(((count - 1) / (maxCount - 1)) * 8);
}

function timeText(ts: string): string {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function MemoryPage() {
  const [user, setUser] = useState<User | null>(null);
  const [profile, setProfile] = useState<MemoryProfile | null>(null);
  const [summary, setSummary] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [error, setError] = useState<{ message: string; detail?: string } | null>(null);
  /** 记忆功能未启用（后端 503）：显示说明而不是错误块 */
  const [disabled, setDisabled] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const tokenNotified = useRef(false);

  const [toasts, pushToast] = useToasts();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.memory();
      setProfile(res.profile);
      setSummary(res.summary || "");
      setDisabled(false);
      setError(null);
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      if (detail.includes("503")) {
        setDisabled(true);
        setError(null);
      } else if (detail.includes("401") || detail.includes("未登录")) {
        setDisabled(false);
        setError({ message: "请先登录后查看自己的长期记忆。", detail });
      } else {
        setDisabled(false);
        setError({ message: "记忆暂时读不到，请稍后重试。", detail });
      }
    } finally {
      setLoading(false);
      setLoadedOnce(true);
    }
  }, []);

  useEffect(() => {
    void (async () => {
      if (!getToken()) {
        setLoadedOnce(true);
        setError({ message: "请先登录后查看自己的长期记忆。" });
        return;
      }
      try {
        const res = await api.me();
        setUser(res.user);
      } catch {
        if (!tokenNotified.current) {
          tokenNotified.current = true;
          pushToast("登录已过期，请重新登录", "error");
        }
      }
      await refresh();
    })();
  }, [refresh, pushToast]);

  const clearMemory = useCallback(async () => {
    try {
      await api.clearMemory();
      pushToast("长期记忆已清空", "info");
      setConfirmClear(false);
      await refresh();
    } catch (e) {
      pushToast(`清空失败：${e instanceof Error ? e.message : String(e)}`, "error");
    }
  }, [pushToast, refresh]);

  const entities = profile?.entities ?? [];
  const topics = profile?.topics ?? [];
  const recent = profile?.recent ?? [];
  const maxEntity = entities.reduce((m, it) => Math.max(m, it.count), 1);
  const maxTopic = topics.reduce((m, it) => Math.max(m, it.count), 1);

  return (
    <div className="admin">
      <div className="admin-head">
        <div className="admin-title">
          <span className="brand-logo">忆</span>
          长期记忆
        </div>
        {profile?.updated_at && (
          <span className="admin-last-updated">最后更新 {timeText(profile.updated_at)}</span>
        )}
        <button className="btn" type="button" onClick={() => void refresh()} disabled={loading} style={{ width: "auto" }}>
          <Icon name="refresh" size={14} />
          {loading ? "刷新中…" : "刷新"}
        </button>
        <button
          className="btn btn-danger"
          type="button"
          onClick={() => setConfirmClear(true)}
          disabled={!user || disabled || entities.length + topics.length + recent.length === 0}
          style={{ width: "auto" }}
        >
          清空记忆
        </button>
        <ThemeToggle />
        <Link className="btn" href="/" style={{ width: "auto" }}>
          返回对话
        </Link>
      </div>

      {error && (
        <div className="err-block">
          <div className="err-title">
            <Icon name="alert" size={16} />
            {error.message}
          </div>
          <div className="err-actions">
            {!user ? (
              <button className="btn btn-primary" type="button" onClick={() => setAuthOpen(true)}>
                登录 / 注册
              </button>
            ) : (
              <button className="btn btn-primary" type="button" onClick={() => void refresh()}>
                重试
              </button>
            )}
          </div>
          {error.detail && (
            <details className="err-detail">
              <summary>技术详情</summary>
              <pre>{error.detail}</pre>
            </details>
          )}
        </div>
      )}

      {disabled && loadedOnce && (
        <div className="notice notice-info">
          长期记忆当前未启用（需要同时开启 <code>MEMORY_ENABLED</code> 与 <code>AUTH_ENABLED</code>）。
          开启后系统会按用户记住关注的实体 / 主题，问答时作为理解上下文使用——不占引用位、不算来源。
        </div>
      )}

      {!error && !disabled && loadedOnce && (
        <div className="admin-grid">
          {/* —— 关注实体标签云 —— */}
          <div className="admin-card">
            <div className="admin-card-title">
              关注实体
              <span className="badge badge-plain">{entities.length} 个</span>
            </div>
            {entities.length === 0 ? (
              <div className="memory-empty">还没有沉淀到实体。多问几个带项目编号 / 法规名 / 企业名的问题就会出现。</div>
            ) : (
              <div className="memory-cloud">
                {(entities as MemoryItem[]).map((item) => (
                  <span
                    key={item.name}
                    className="memory-tag"
                    style={{ fontSize: tagSize(item.count, maxEntity) }}
                    title={`提到 ${item.count} 次`}
                  >
                    {item.name}
                    <em>{item.count}</em>
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* —— 常问主题频次条 —— */}
          <div className="admin-card">
            <div className="admin-card-title">
              常问主题
              <span className="badge badge-plain">{topics.length} 个</span>
            </div>
            {topics.length === 0 ? (
              <div className="memory-empty">还没有主题记录。问过几轮之后这里会按频次排开。</div>
            ) : (
              <div className="memory-bars">
                {(topics as MemoryItem[]).slice(0, 12).map((item) => (
                  <div className="memory-bar-row" key={item.name}>
                    <span className="memory-bar-name" title={item.name}>
                      {item.name}
                    </span>
                    <div className="memory-bar-track">
                      <div
                        className="memory-bar-fill"
                        style={{ width: `${Math.max(6, Math.round((item.count / maxTopic) * 100))}%` }}
                      />
                    </div>
                    <span className="memory-bar-count">{item.count}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* —— 最近提问时间线 —— */}
          <div className="admin-card admin-span-all">
            <div className="admin-card-title">
              最近提问
              <span className="badge badge-plain">最近 {recent.length} 条</span>
            </div>
            {recent.length === 0 ? (
              <div className="memory-empty">最近还没有提问记录。</div>
            ) : (
              <div className="memory-timeline">
                {[...recent].reverse().map((item, i) => (
                  <div className="memory-timeline-row" key={`${item.ts}-${i}`}>
                    <span className="memory-timeline-ts">{timeText(item.ts)}</span>
                    <span className="memory-timeline-q">{item.question}</span>
                  </div>
                ))}
              </div>
            )}
            {summary && (
              <details className="memory-summary">
                <summary>当前注入给模型的记忆摘要（不占引用位、不算来源）</summary>
                <pre>{summary}</pre>
              </details>
            )}
          </div>
        </div>
      )}

      {/* 清空二次确认：记忆是用户数据，误点不可逆 */}
      {confirmClear && (
        <div className="err-block notice-danger">
          <div className="err-title">
            <Icon name="alert" size={16} />
            确认清空长期记忆？此操作不可撤销。
          </div>
          <div className="err-actions">
            <button className="btn btn-danger" type="button" onClick={() => void clearMemory()}>
              确认清空
            </button>
            <button className="btn" type="button" onClick={() => setConfirmClear(false)}>
              取消
            </button>
          </div>
        </div>
      )}

      {/* 复用主页面同一套登录弹窗：登录成功后刷新画像，让卡片可读 */}
      {authOpen && (
        <AuthDialog
          onClose={() => setAuthOpen(false)}
          onSuccess={(u) => {
            setUser(u);
            setAuthOpen(false);
            void refresh();
          }}
        />
      )}
      <ToastStack toasts={toasts} />
    </div>
  );
}
