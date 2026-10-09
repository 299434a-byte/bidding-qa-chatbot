"use client";

import type { Health, KbStats, SchemaResponse } from "@/lib/api";

/**
 * 系统状态卡片区：把原来主对话页顶部那排「知识库 N 块 / PostgreSQL 已连接 / 图谱 / 检索策略」大徽标
 * 搬到这里，用整齐的小卡片展示。
 *
 * 【为什么单独抽成组件】
 *   纯展示，数据全部来自 props（health / kb / schema），没有自己的状态，方便复用与阅读。
 */

type Props = {
  health: Health | null;
  kb: KbStats | null;
  schema: SchemaResponse | null;
};

/** 单张状态卡：标题 + 值 + 一个表示状态的小徽标 */
function StatusCard({ label, value, tone, badge }: { label: string; value: string; tone: string; badge: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4, flexWrap: "wrap" }}>
        <span className={`badge ${tone}`}>{badge}</span>
        <span style={{ fontSize: 12, color: "var(--text-dim)" }}>{value}</span>
      </div>
    </div>
  );
}

export default function StatusCards({ health, kb, schema }: Props) {
  const cards = [
    {
      label: "运行模式",
      badge: health?.agent_configured ? "Agent 模式" : "仅检索模式",
      tone: health?.agent_configured ? "badge-mint" : "badge-warn",
      value: health?.llm_configured ? health.llm_model_display || health.llm_model || "" : "未配置大模型",
    },
    {
      label: "知识库",
      badge: `${kb?.points ?? 0} 块`,
      tone: (kb?.points ?? 0) === 0 ? "badge-warn" : "badge-blue",
      value: kb?.collection ?? "—",
    },
    {
      label: "MySQL 数据源",
      badge: schema?.connected ? "已连接" : health?.mysql_configured ? "连接失败" : "未配置",
      tone: schema?.connected ? "badge-mint" : health?.mysql_configured ? "badge-danger" : "badge-warn",
      value: schema?.database ?? "—",
    },
    {
      label: "PostgreSQL",
      badge: health?.pg_configured ? "已连接" : "未配置",
      tone: health?.pg_configured ? "badge-mint" : "badge-plain",
      value: "结构化统计查询",
    },
    {
      label: "知识图谱",
      badge: health?.graph_configured ? "已连接" : "未配置",
      tone: health?.graph_configured ? "badge-mint" : "badge-plain",
      value: "Neo4j 关系查询",
    },
    {
      label: "联网搜索",
      badge: health?.web_search_configured ? "已就绪" : "未配置",
      tone: health?.web_search_configured ? "badge-mint" : "badge-plain",
      value: "Tavily 最新公告",
    },
    {
      label: "检索策略",
      badge: `向量 + BM25 + RRF${health?.rerank_configured ? " + 重排" : ""}`,
      tone: "badge-plain",
      value: health?.rerank_configured ? "已启用重排" : "未启用重排",
    },
    {
      label: "向量模型",
      badge: kb?.embed_model ?? `${kb?.vector_dim ?? 512}d`,
      tone: "badge-plain",
      value: `${kb?.bm25_docs ?? 0} 篇 BM25 文档`,
    },
  ];

  return (
    <div className="admin-status-grid">
      {cards.map((card) => (
        <StatusCard key={card.label} {...card} />
      ))}
    </div>
  );
}