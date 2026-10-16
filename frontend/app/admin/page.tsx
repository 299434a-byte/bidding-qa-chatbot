"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import AuthDialog from "@/components/AuthDialog";
import ThemeToggle from "@/components/ThemeToggle";
import ToastStack, { useToasts } from "@/components/chat/Toast";
import StatusCards from "@/components/admin/StatusCards";
import DataOverview from "@/components/admin/DataOverview";
import DebugConsole from "@/components/admin/DebugConsole";
import EvalCard from "@/components/admin/EvalCard";
import SchemaCard from "@/components/admin/SchemaCard";
import HistorySearch from "@/components/admin/HistorySearch";
import UserManage from "@/components/admin/UserManage";
import Icon from "@/components/ui/icons";
import {
  api,
  getToken,
  type Health,
  type IngestStatus,
  type KbStats,
  type MetricsSummary,
  type SchemaResponse,
  type User,
} from "@/lib/api";


const AUTO_REFRESH_MS = 30_000;

function clockText(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export default function AdminPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [kb, setKb] = useState<KbStats | null>(null);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  /** 问答指标（含重排成功率），供「重排成功率」状态卡展示 */
  const [metrics, setMetrics] = useState<MetricsSummary | null>(null);
  /** 指标接口返回 401（未登录）时为 true：状态卡显示「需登录后查看」 */
  const [metricsLocked, setMetricsLocked] = useState(false);
  const [ingest, setIngest] = useState<IngestStatus | null>(null);
  const [loading, setLoading] = useState(false);
  /** 首次加载是否已完成（用于骨架屏） */
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [error, setError] = useState<{ message: string; detail?: string } | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  /** 后端判定的管理员身份（账号管理卡仅管理员可见可操作） */
  const [isAdmin, setIsAdmin] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);

  const [toasts, pushToast] = useToasts();

  /** 拉取后台所需数据 */
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [h, k, s] = await Promise.all([
        api.health(),
        api.kbStats(),
        api.schema(),
      ]);
      setHealth(h);
      setKb(k);
      setSchema(s);
      // /api/metrics 已需登录（部署加固）：401 只锁指标卡，不让整个状态区跟着报错
      try {
        setMetrics(await api.metrics());
        setMetricsLocked(false);
      } catch (e) {
        setMetrics(null);
        const detail = e instanceof Error ? e.message : String(e);
        setMetricsLocked(detail.includes("401") || detail.includes("未登录"));
      }
      setError(null);
      setLastUpdated(clockText());
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      setError({ message: "服务暂时不可用，请稍后重试。", detail });
    } finally {
      setLoading(false);
      setLoadedOnce(true);
    }
  }, []);

  /** 启动重建知识库 */
  const rebuild = useCallback(async () => {
    try {
      await api.startIngest("full");
      setIngest(await api.ingestStatus());
      pushToast("已开始重建知识库", "info");
    } catch (e) {
      pushToast(`启动失败：${e instanceof Error ? e.message : String(e)}`, "error");
    }
  }, [pushToast]);

  /** 启动增量更新：只写新增/变更的块、删掉失效块，保留其余数据 */
  const incremental = useCallback(async () => {
    try {
      await api.startIngest("incremental");
      setIngest(await api.ingestStatus());
      pushToast("已开始增量更新", "info");
    } catch (e) {
      pushToast(`启动失败：${e instanceof Error ? e.message : String(e)}`, "error");
    }
  }, [pushToast]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载后各拉取一次初始状态，setState 都在异步回调里完成，非渲染期同步更新
    void refresh();
    void (async () => {
      try {
        setIngest(await api.ingestStatus());
      } catch {
        /* 未开始建库或接口异常时忽略 */
      }
    })();
  }, [refresh]);

  // 自动刷新 30s
  useEffect(() => {
    if (!autoRefresh) return;
    const timer = setInterval(() => void refresh(), AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [autoRefresh, refresh]);

  // 建库进行中时轮询进度
  useEffect(() => {
    if (!ingest?.running) return;
    const timer = setInterval(async () => {
      try {
        const status = await api.ingestStatus();
        setIngest(status);
        if (!status.running) void refresh();
      } catch {
        /* 轮询失败忽略，下一轮继续 */
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [ingest?.running, refresh]);

  // 启动时若本地有令牌，拉一次当前用户，供「历史检索」与写操作按钮判断登录态
  const tokenNotified = useRef(false);
  useEffect(() => {
    if (!getToken()) return;
    let alive = true;
    void (async () => {
      try {
        const res = await api.me();
        if (alive) {
          setUser(res.user);
          setIsAdmin(res.is_admin === true);
        }
      } catch {
        if (alive && !tokenNotified.current) {
          tokenNotified.current = true;
          pushToast("登录已过期，请重新登录", "error");
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [pushToast]);

  return (
    <div className="admin">
      <div className="admin-head">
        <div className="admin-title">
          <span className="brand-logo">标</span>
          后台控制台
        </div>
        {lastUpdated && <span className="admin-last-updated">最后更新 {lastUpdated}</span>}
        <button
          className={`chip ${autoRefresh ? "chip-on" : ""}`}
          type="button"
          onClick={() => setAutoRefresh((v) => !v)}
          aria-pressed={autoRefresh}
          title="开启后每 30 秒自动刷新一次"
        >
          <Icon name="refresh" size={13} />自动刷新 30s
        </button>
        <button className="btn" type="button" onClick={() => void refresh()} disabled={loading} style={{ width: "auto" }}>
          <Icon name="refresh" size={14} />
          {loading ? "刷新中…" : "刷新"}
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
            <button className="btn btn-primary" type="button" onClick={() => void refresh()}>
              重试
            </button>
          </div>
          {error.detail && (
            <details className="err-detail">
              <summary>技术详情</summary>
              <pre>{error.detail}</pre>
            </details>
          )}
        </div>
      )}

      <div className="admin-grid">
        <div className="admin-card admin-span-all">
          <div className="admin-card-title">系统状态</div>
          {loadedOnce ? (
            <StatusCards health={health} kb={kb} schema={schema} metrics={metrics} metricsLocked={metricsLocked} />
          ) : (
            <div className="stat-grid">
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <div className="stat-card" key={i}>
                  <div className="skeleton skel-line" style={{ width: "40%" }} />
                  <div className="skeleton skel-line" style={{ width: "70%" }} />
                </div>
              ))}
            </div>
          )}
        </div>

        <DataOverview kb={kb} schema={schema} />

        <DebugConsole
          ingest={ingest}
          loading={loading}
          loggedIn={Boolean(user)}
          onRebuild={() => void rebuild()}
          onIncremental={() => void incremental()}
          onRefresh={() => void refresh()}
          onLogin={() => setAuthOpen(true)}
          onToast={pushToast}
        />

        <EvalCard />

        <SchemaCard schema={schema} />

        <HistorySearch user={user} />

        <UserManage
          user={user}
          isAdmin={isAdmin}
          onToast={pushToast}
          onChanged={() => {
            // 登录态变化后重拉一次 me，保持管理员标记与账号状态同步
            void api
              .me()
              .then((res) => {
                setUser(res.user);
                setIsAdmin(res.is_admin === true);
              })
              .catch(() => undefined);
          }}
        />
      </div>

      {/* 复用主页面同一套登录弹窗：登录成功后更新登录态并刷新，使写操作按钮可用 */}
      {authOpen && (
        <AuthDialog
          onClose={() => setAuthOpen(false)}
          onSuccess={(u) => {
            setUser(u);
            setAuthOpen(false);
            pushToast(`已登录：${u.username}`, "success");
            void refresh();
            // 重新判定管理员身份（账号管理卡是否展示取决于它）
            void api
              .me()
              .then((res) => setIsAdmin(res.is_admin === true))
              .catch(() => undefined);
          }}
        />
      )}

      <ToastStack toasts={toasts} />
    </div>
  );
}
