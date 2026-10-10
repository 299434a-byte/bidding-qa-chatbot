"use client";

import { useEffect, useRef, useState } from "react";
import Icon from "@/components/ui/icons";
import type { IngestStatus } from "@/lib/api";
import type { ToastKind } from "@/components/chat/Toast";

/**
 * 调试控制台：重建 / 增量更新 + 建库进度 + 实时日志 + 结果回显。
 *
 * 重建是整库重灌的高危动作，点击后先弹二次确认；未登录则完全不显示写操作按钮，
 * 只保留「去登录」提示。建库结束后用 toast 汇报成功 / 失败。
 */

type IngestResult = {
  documents?: number;
  points?: number;
  bm25_terms?: number;
  elapsed?: number;
  warnings?: string[];
  errors?: string[];
  added?: number;
  updated?: number;
  deleted?: number;
  unchanged?: number;
};

type Props = {
  ingest: IngestStatus | null;
  loading: boolean;
  /** 是否已登录：未登录时隐藏「重建知识库」等写操作，避免匿名触发高危运维动作 */
  loggedIn: boolean;
  onRebuild: () => void;
  onIncremental: () => void;
  onRefresh: () => void;
  /** 未登录时点「去登录」，由页面打开登录弹窗 */
  onLogin: () => void;
  onToast: (text: string, kind?: ToastKind) => void;
};

function fmt(value: number | undefined): string {
  return value === undefined || value === null ? "—" : value.toLocaleString("zh-CN");
}

export default function DebugConsole({
  ingest,
  loading,
  loggedIn,
  onRebuild,
  onIncremental,
  onRefresh,
  onLogin,
  onToast,
}: Props) {
  const running = Boolean(ingest?.running);
  const result = (ingest?.result ?? null) as IngestResult | null;
  const [confirmOpen, setConfirmOpen] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  /** 上一次的 running 值：只在「进行中 → 结束」那一刻弹提示，避免首屏误报 */
  const wasRunning = useRef(running);

  // 建库结束：根据有无错误弹成功 / 失败提示
  useEffect(() => {
    if (wasRunning.current && !running) {
      if (ingest?.error) onToast("操作失败，请查看下方日志", "error");
      else onToast(typeof result?.added === "number" ? "增量更新已完成" : "重建已完成", "success");
    }
    wasRunning.current = running;
  }, [running, ingest?.error, result?.added, onToast]);

  // 日志实时滚动到底部
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [ingest?.logs]);

  return (
    <div className="admin-card">
      <div className="admin-card-title">
        调试控制台
        <button className="badge badge-plain" style={{ cursor: "pointer" }} onClick={onRefresh} disabled={loading} type="button">
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {loggedIn ? (
        <div style={{ display: "flex", gap: "var(--sp-8)", flexWrap: "wrap" }}>
          <button
            className="btn btn-primary"
            onClick={() => setConfirmOpen(true)}
            disabled={running}
            type="button"
            style={{ width: "auto" }}
          >
            <Icon name="refresh" size={14} />
            {running ? "进行中…" : "重建知识库"}
          </button>
          <button className="btn" onClick={onIncremental} disabled={running} type="button" style={{ width: "auto" }}>
            <Icon name="arrow-down" size={14} />
            增量更新
          </button>
        </div>
      ) : (
        <div className="notice notice-warn" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "var(--sp-8)" }}>
          <span>登录后可执行重建 / 增量更新等运维操作。</span>
          <button className="btn" type="button" onClick={onLogin} style={{ width: "auto" }}>
            去登录
          </button>
        </div>
      )}

      {/* 进行中：阶段文案 + 进度条 */}
      {running && (
        <div style={{ marginTop: "var(--sp-12)" }}>
          <div className="row">
            <span className="row-key">{ingest?.message || ingest?.stage || "处理中…"}</span>
            <span className="row-val">{ingest?.stage ?? ""}</span>
          </div>
          <div className="progress" style={{ marginTop: "var(--sp-8)" }}>
            <div className="progress-bar" />
          </div>
        </div>
      )}

      {/* 结果回显 */}
      <div className="card" style={{ marginTop: "var(--sp-12)" }}>
        {ingest?.error && <div className="notice notice-danger">{ingest.error}</div>}
        {result && !running && (
          <>
            <div className="row">
              <span className="row-key">文档块</span>
              <span className="row-val">{fmt(result.documents)}</span>
            </div>
            <div className="row">
              <span className="row-key">写入点数</span>
              <span className="row-val">{fmt(result.points)}</span>
            </div>
            <div className="row">
              <span className="row-key">关键词索引</span>
              <span className="row-val">{fmt(result.bm25_terms)}</span>
            </div>
            <div className="row">
              <span className="row-key">耗时</span>
              <span className="row-val">{result.elapsed === undefined ? "—" : `${result.elapsed}s`}</span>
            </div>
            {/* 增量模式结果：回显四个差集计数（全量模式没有 added 字段，故不显示这几行）。 */}
            {typeof result.added === "number" && (
              <>
                <div className="row">
                  <span className="row-key">新增</span>
                  <span className="row-val">{fmt(result.added)}</span>
                </div>
                <div className="row">
                  <span className="row-key">更新</span>
                  <span className="row-val">{fmt(result.updated)}</span>
                </div>
                <div className="row">
                  <span className="row-key">删除</span>
                  <span className="row-val">{fmt(result.deleted)}</span>
                </div>
                <div className="row">
                  <span className="row-key">未变</span>
                  <span className="row-val">{fmt(result.unchanged)}</span>
                </div>
              </>
            )}
          </>
        )}
        {!result && !running && !ingest?.error && <div className="history-hint">还没有建库记录。</div>}
      </div>

      {ingest?.logs && ingest.logs.length > 0 && (
        <div className="log" style={{ marginTop: "var(--sp-12)" }} ref={logRef}>
          {ingest.logs.slice(-60).join("\n")}
        </div>
      )}

      {/* 二次确认：重建会清空并重灌整个知识库 */}
      {confirmOpen && (
        <div className="auth-overlay" onClick={() => setConfirmOpen(false)} role="presentation">
          <div className="auth-card" onClick={(e) => e.stopPropagation()}>
            <div className="auth-head">
              <div className="auth-title">重建知识库？</div>
              <button className="icon-btn" type="button" onClick={() => setConfirmOpen(false)} aria-label="关闭">
                <Icon name="x" size={16} />
              </button>
            </div>
            <div className="auth-hint" style={{ marginTop: 0, color: "var(--neutral-600)" }}>
              重建会清空现有知识库并重新灌入全部数据，过程可能持续数分钟。若只想同步新增与变更，请改用「增量更新」。
            </div>
            <div className="auth-actions">
              <button
                className="btn btn-primary"
                type="button"
                onClick={() => {
                  setConfirmOpen(false);
                  onRebuild();
                }}
              >
                确认重建
              </button>
              <button className="btn" type="button" onClick={() => setConfirmOpen(false)}>
                取消
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
