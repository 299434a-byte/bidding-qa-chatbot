"use client";

import type { IngestStatus } from "@/lib/api";

/**
 * 调试控制台：重建知识库按钮 + 建库进度 / 结果 + 日志输出。
 *
 * 【为什么单独抽成组件】
 *   原来这套「刷新 / 重建 / 日志」挤在主对话页左栏，现搬到后台页集中管理。
 *   组件本身不拉数据，只接收 props.ingest 与 onRebuild 回调，状态由页面持有。
 */

type Props = {
  ingest: IngestStatus | null;
  loading: boolean;
  onRebuild: () => void;
  onRefresh: () => void;
};

export default function DebugConsole({ ingest, loading, onRebuild, onRefresh }: Props) {
  const running = Boolean(ingest?.running);
  const result = ingest?.result as
    | { documents?: number; points?: number; bm25_terms?: number; elapsed?: number; warnings?: string[]; errors?: string[] }
    | null
    | undefined;

  return (
    <div className="admin-card">
      <div className="admin-card-title">
        调试控制台
        <button
          className="badge badge-plain"
          style={{ cursor: "pointer" }}
          onClick={onRefresh}
          disabled={loading}
          type="button"
        >
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      <button className="btn btn-primary" onClick={onRebuild} disabled={running} type="button">
        {running ? `重建中：${ingest?.stage ?? ""}` : "重建知识库"}
      </button>

      <div className="card" style={{ marginTop: 10 }}>
        {ingest?.error && <div className="notice notice-danger">{ingest.error}</div>}
        {result && !running && (
          <>
            <div className="row">
              <span className="row-key">文档块</span>
              <span className="row-val">{result.documents ?? 0}</span>
            </div>
            <div className="row">
              <span className="row-key">写入点数</span>
              <span className="row-val">{result.points ?? 0}</span>
            </div>
            <div className="row">
              <span className="row-key">BM25 词元</span>
              <span className="row-val">{result.bm25_terms ?? 0}</span>
            </div>
            <div className="row">
              <span className="row-key">耗时</span>
              <span className="row-val">{result.elapsed ?? 0}s</span>
            </div>
          </>
        )}
        {!result && !running && !ingest?.error && (
          <div className="history-hint">还没有建库记录，点击「重建知识库」开始灌入数据。</div>
        )}
      </div>

      {ingest?.logs && ingest.logs.length > 0 && (
        <div className="log" style={{ marginTop: 10 }}>
          {ingest.logs.slice(-40).join("\n")}
        </div>
      )}
    </div>
  );
}