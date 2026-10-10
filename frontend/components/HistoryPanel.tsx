"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Conversation, type HistoryHit, type User } from "@/lib/api";

/**
 * 对话历史面板（主对话页左栏专用）：新建对话 + 关键词检索 + 会话列表 + 重命名 / 删除。
 *
 * 【为什么变简单了】
 *   原先把「日期范围 + 模式筛选 + 检索/清空按钮」也放在这里，左栏改窄（260px）后太挤；
 *   按改造方案，这些偏「管理/分析」的筛选搬到了后台页 /admin（见 components/admin/HistorySearch.tsx），
 *   左栏只留最常用的「新建、搜索、列表」。
 *
 * 【检索怎么触发】
 *   只保留关键词输入框，按回车即检索；把输入框清空会自动回到普通会话列表。
 */

type Props = {
  /** 后端是否开启对话历史（/api/health 的 history_configured） */
  enabled: boolean;
  /** 当前登录用户（未登录时为 null） */
  user: User | null;
  /** 打开某个会话并定位到命中消息（messageId 可省略） */
  onOpenConversation: (id: string, messageId?: string) => void;
  /** 新建对话 */
  onNewConversation: () => void;
  /** 外部触发刷新（如新一轮对话落库后自增） */
  refreshKey: number;
};

/** 模式展示名（与输入区的三选一保持一致） */
const MODE_LABELS: Record<string, string> = {
  react: "ReAct",
  plan: "Plan-and-Solve",
  deep: "深度思考",
};

/** 把后端返回的时间戳截成「YYYY-MM-DD HH:mm」 */
function shortTime(value: string): string {
  return (value ?? "").replace("T", " ").slice(0, 16);
}

export default function HistoryPanel({ enabled, user, onOpenConversation, onNewConversation, refreshKey }: Props) {
  const [items, setItems] = useState<Conversation[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 关键词检索
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<HistoryHit[] | null>(null);
  const [searchTotal, setSearchTotal] = useState(0);
  const [searching, setSearching] = useState(false);

  // 重命名中的会话 id 与草稿
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");

  /** 拉取会话列表 */
  const load = useCallback(async () => {
    if (!enabled || !user) {
      setItems([]);
      setHits(null);
      return;
    }
    setLoading(true);
    try {
      const res = await api.conversations({ limit: 50 });
      setItems(res.items);
      setTotal(res.total);
      setError(null);
    } catch (e) {
      setError(`加载会话失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  }, [enabled, user]);

  // 登录态 / 外部 refreshKey 变化时重新拉取
  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  /** 关键词检索（仅按关键词，日期/模式筛选在后台页） */
  const runSearch = async () => {
    if (!user || !q.trim()) {
      setHits(null);
      return;
    }
    setSearching(true);
    try {
      const res = await api.searchHistory({ q: q.trim() });
      setHits(res.items);
      setSearchTotal(res.total);
      setError(null);
    } catch (e) {
      setError(`检索失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSearching(false);
    }
  };

  /** 输入框清空即回到普通会话列表 */
  const onChangeQuery = (value: string) => {
    setQ(value);
    if (!value.trim()) {
      setHits(null);
      setSearchTotal(0);
    }
  };

  /** 保存重命名 */
  const doRename = async (id: string) => {
    const title = renameText.trim();
    if (!title) {
      setRenamingId(null);
      return;
    }
    try {
      const res = await api.renameConversation(id, title);
      setItems((prev) => prev.map((c) => (c.id === id ? res.conversation : c)));
      setError(null);
    } catch (e) {
      setError(`重命名失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setRenamingId(null);
    }
  };

  /** 删除会话（连带清空该会话的检索命中） */
  const doDelete = async (id: string) => {
    if (!window.confirm("确认删除该对话？删除后不可恢复。")) return;
    try {
      await api.deleteConversation(id);
      setItems((prev) => prev.filter((c) => c.id !== id));
      setHits((prev) => (prev ? prev.filter((h) => h.conversation_id !== id) : prev));
      setError(null);
    } catch (e) {
      setError(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // 未开启历史 / 未登录：给出明确提示，不请求数据
  if (!enabled) {
    return <div className="history-hint">历史记录暂未开启，你的提问仍可正常使用，只是不会被保存。</div>;
  }
  if (!user) {
    return <div className="history-hint">登录后可保存并查看历史对话，未登录时问答照常但不会落库。</div>;
  }

  return (
    <div className="history-panel">
      <div className="history-toolbar">
        <button className="btn btn-primary" type="button" onClick={onNewConversation}>
          新建对话
        </button>
      </div>

      {/* 关键词检索：回车即搜；清空即回到列表 */}
      <div className="history-search">
        <input
          className="history-input"
          value={q}
          placeholder="检索历史消息关键词（回车搜索）"
          onChange={(e) => onChangeQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void runSearch();
          }}
        />
      </div>

      {error && <div className="notice notice-danger">{error}</div>}

      {/* 检索结果 */}
      {hits && (
        <div className="history-results">
          <div className="history-results-title">
            检索结果 {hits.length} / {searchTotal} 条
            {searching && <span className="badge badge-plain">检索中…</span>}
            <span className="badge badge-plain">点击定位到该条消息</span>
          </div>
          {hits.length === 0 && <div className="history-hint">没有匹配的历史消息。</div>}
          {hits.map((hit) => (
            <div
              className="history-hit"
              key={hit.message_id}
              onClick={() => onOpenConversation(hit.conversation_id, hit.message_id)}
            >
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

      {/* 会话列表 */}
      <div className="history-list-title">
        会话列表 {items.length} / {total}
        {loading && <span className="badge badge-plain">加载中…</span>}
      </div>
      {items.length === 0 && !loading && <div className="history-hint">还没有历史对话，先问一轮试试。</div>}
      <div className="history-list">
        {items.map((conv) => (
          <div className="history-item" key={conv.id}>
            {renamingId === conv.id ? (
              <div className="history-rename">
                <input
                  className="history-input"
                  value={renameText}
                  autoFocus
                  onChange={(e) => setRenameText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void doRename(conv.id);
                    if (e.key === "Escape") setRenamingId(null);
                  }}
                />
                <button className="chip" type="button" onClick={() => void doRename(conv.id)}>
                  保存
                </button>
                <button className="chip" type="button" onClick={() => setRenamingId(null)}>
                  取消
                </button>
              </div>
            ) : (
              <>
                <div
                  className="history-item-head"
                  onClick={() => onOpenConversation(conv.id)}
                  title="点击恢复该对话"
                >
                  <span className="history-title">{conv.title || "未命名对话"}</span>
                  <span className="badge badge-blue">{MODE_LABELS[conv.mode] ?? conv.mode}</span>
                </div>
                <div className="history-meta">
                  <span>{shortTime(conv.updated_at)}</span>
                  <span>·</span>
                  <span>{conv.message_count} 条消息</span>
                </div>
                <div className="history-actions">
                  <button
                    className="chip"
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setRenamingId(conv.id);
                      setRenameText(conv.title);
                    }}
                  >
                    重命名
                  </button>
                  <button className="chip" type="button" onClick={() => void doDelete(conv.id)}>
                    删除
                  </button>
                </div>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}