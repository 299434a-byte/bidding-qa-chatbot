"use client";

import { useState } from "react";
import { api, type HistoryHit, type User } from "@/lib/api";
// 【展示本地化】时间列不再直接截断字符串：库里的 UTC 时间戳要解析后换算成东八区展示
// （历史 naive 文本按东八区解释），实现见 lib/timefmt.ts。
import { shortTime } from "@/lib/timefmt";

/**
 * 历史检索（后台页专用）：关键词 + 日期范围 + 模式 三个维度的完整检索，
 * 以及「检索 / 清空」按钮与命中结果列表。
 *
 * 【为什么放在后台页】
 *   主对话页左栏改窄成 260px 后，只保留最常用的「新建、关键词搜索、列表」；
 *   这套偏「分析/筛查」的日期与模式筛选按改造方案搬到这里，功能不丢失。
 *
 * 【命中结果的点击行为】
 *   点击一条命中会跳到主对话页并定位到那条消息，所以需要用到 router 跳转。
 */

type Props = {
  user: User | null;
};

/** 模式展示名（与输入区的三选一保持一致） */
const MODE_LABELS: Record<string, string> = {
  react: "ReAct",
  plan: "Plan-and-Solve",
  deep: "深度思考",
};

/** 【展示本地化】shortTime 已上提到 lib/timefmt.ts：解析 UTC/老 naive 文本后按东八区格式化为「YYYY-MM-DD HH:mm」。 */


export default function HistorySearch({ user }: Props) {
  const [q, setQ] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [modeFilter, setModeFilter] = useState("");

  const [hits, setHits] = useState<HistoryHit[] | null>(null);
  const [searchTotal, setSearchTotal] = useState(0);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runSearch = async () => {
    if (!user || !q.trim()) {
      setHits(null);
      return;
    }
    setSearching(true);
    try {
      const res = await api.searchHistory({
        q: q.trim(),
        from: from || undefined,
        to: to || undefined,
        mode: modeFilter || undefined,
      });
      setHits(res.items);
      setSearchTotal(res.total);
      setError(null);
    } catch (e) {
      setError(`检索失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSearching(false);
    }
  };

  const clearSearch = () => {
    setQ("");
    setFrom("");
    setTo("");
    setHits(null);
    setSearchTotal(0);
    setError(null);
  };

  if (!user) {
    return (
      <div className="admin-card">
        <div className="admin-card-title">历史检索</div>
        <div className="history-hint">登录后才能检索历史对话。请回到对话页登录。</div>
      </div>
    );
  }

  return (
    <div className="admin-card">
      <div className="admin-card-title">
        历史检索
        <span className="badge badge-plain">关键词 + 时间 + 模式</span>
      </div>

      <div className="history-search">
        <input
          className="history-input"
          value={q}
          placeholder="检索历史消息关键词…"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void runSearch();
          }}
        />
        <div className="history-range">
          <input
            className="history-input"
            type="date"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            title="起始日期"
          />
          <span className="history-range-sep">至</span>
          <input
            className="history-input"
            type="date"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            title="结束日期"
          />
        </div>
        <select className="history-select" value={modeFilter} onChange={(e) => setModeFilter(e.target.value)}>
          <option value="">全部模式</option>
          <option value="react">ReAct</option>
          <option value="plan">Plan-and-Solve</option>
          <option value="deep">深度思考</option>
        </select>
        <div className="history-search-actions">
          <button className="btn btn-primary" type="button" onClick={() => void runSearch()} disabled={searching || !q.trim()}>
            {searching ? "检索中…" : "检索"}
          </button>
          <button className="btn" type="button" onClick={clearSearch} disabled={searching}>
            清空
          </button>
        </div>
      </div>

      {error && <div className="notice notice-danger">{error}</div>}

      {hits && (
        <div className="history-results">
          <div className="history-results-title">
            检索结果 {hits.length} / {searchTotal} 条
          </div>
          {hits.length === 0 && <div className="history-hint">没有匹配的历史消息。</div>}
          {hits.map((hit) => (
            <div className="history-hit" key={hit.message_id}>
              <div className="history-hit-head">
                <span className="history-hit-title">{hit.conversation_title || "未命名对话"}</span>
                <span className="badge badge-plain">{MODE_LABELS[hit.mode] ?? hit.mode}</span>
                <span className="history-hit-time">{shortTime(hit.created_at)}</span>
              </div>
              {/* snippet 已由后端转义并带上 <em> 高亮标签 */}
              <div className="history-snippet" dangerouslySetInnerHTML={{ __html: hit.snippet }} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}