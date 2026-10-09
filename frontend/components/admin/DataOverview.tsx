"use client";

import type { KbStats, SchemaResponse } from "@/lib/api";

/**
 * 数据概览：向量库里的文档块分布（按来源表）+ MySQL 各表的实际行数。
 *
 * 【为什么单独抽成组件】
 *   纯展示，数据来自 props（kb / schema），把「有多少数据」一眼看清，
 *   与「运行状态」（StatusCards）分开放，页面更清晰。
 */

type Props = {
  kb: KbStats | null;
  schema: SchemaResponse | null;
};

export default function DataOverview({ kb, schema }: Props) {
  const tables = schema?.tables ? Object.values(schema.tables) : [];

  return (
    <div className="admin-card admin-span-all">
      <div className="admin-card-title">
        数据概览
        <span className="badge badge-plain">向量库 {kb?.points ?? 0} 块</span>
      </div>

      <div className="stat-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" }}>
        <div className="stat">
          <div className="stat-label">向量库文档块</div>
          <div className="stat-value">{kb?.points ?? 0}</div>
        </div>
        <div className="stat">
          <div className="stat-label">BM25 文档数</div>
          <div className="stat-value">{kb?.bm25_docs ?? 0}</div>
        </div>
        {Object.entries(kb?.by_table ?? {}).map(([table, count]) => (
          <div className="stat" key={table}>
            <div className="stat-label">{kb?.table_labels?.[table] ?? table} · 块</div>
            <div className="stat-value">{count}</div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 12 }}>
        <div className="row-key" style={{ marginBottom: 4 }}>
          MySQL 表（各行数）
        </div>
        {tables.length === 0 && (
          <div className="notice notice-warn" style={{ marginTop: 4 }}>
            {schema?.error ?? "尚未获取到表结构（请检查 .env 中的 MySQL 配置）"}
          </div>
        )}
        {tables.map((table) => (
          <div className="row" key={table.table}>
            <span className="row-key">
              {table.label ?? table.table}
              <span style={{ color: "var(--text-faint)", fontSize: 11 }}>{table.table}</span>
            </span>
            <span className="row-val">
              {table.exists ? `${table.row_count ?? 0} 行` : "不存在"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}