"use client";

import Icon from "@/components/ui/icons";
import type { KbStats, SchemaResponse } from "@/lib/api";

/**
 * 数据概览：知识库的文档块分布（按来源）与业务数据表的实际行数。
 * 纯展示，数据来自 props（kb / schema）；数字统一千分位 + 单位。
 */

type Props = {
  kb: KbStats | null;
  schema: SchemaResponse | null;
};

/** 千分位 + 单位；空值显示 `—` */
function fmt(value: number | undefined, unit: string): string {
  if (value === undefined || value === null) return "—";
  return `${value.toLocaleString("zh-CN")} ${unit}`;
}

export default function DataOverview({ kb, schema }: Props) {
  const tables = schema?.tables ? Object.values(schema.tables) : [];
  const byTable = Object.entries(kb?.by_table ?? {});

  return (
    <div className="admin-card admin-span-all">
      <div className="admin-card-title">
        数据概览
        <span className="badge badge-plain">向量库 {fmt(kb?.points, "块")}</span>
      </div>

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-top">
            <span className="stat-icon">
              <Icon name="database" size={14} />
            </span>
            <span className="stat-name">文档块</span>
          </div>
          <div className="stat-value">{fmt(kb?.points, "块")}</div>
          <div className="stat-desc">可供检索的原文片段总数</div>
        </div>

        <div className="stat-card">
          <div className="stat-top">
            <span className="stat-icon">
              <Icon name="search" size={14} />
            </span>
            <span className="stat-name">关键词索引</span>
          </div>
          <div className="stat-value">{fmt(kb?.bm25_docs, "篇")}</div>
          <div className="stat-desc">参与关键词检索的文档数</div>
        </div>

        {byTable.map(([table, count]) => (
          <div className="stat-card" key={table}>
            <div className="stat-top">
              <span className="stat-icon">
                <Icon name="file-text" size={14} />
              </span>
              <span className="stat-name">{kb?.table_labels?.[table] ?? "业务数据"}</span>
            </div>
            <div className="stat-value">{fmt(count, "块")}</div>
            <div className="stat-desc">来自该来源的文档块</div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: "var(--sp-16)" }}>
        <div className="row-key" style={{ marginBottom: "var(--sp-4)" }}>
          业务数据表（行数）
        </div>
        {tables.length === 0 && (
          <div className="notice notice-warn">暂时无法读取业务数据表，稍后重试或到后台日志查看原因。</div>
        )}
        {tables.map((table) => (
          <div className="row" key={table.table}>
            <span className="row-key">{table.label ?? "业务数据表"}</span>
            <span className="row-val">{table.exists ? fmt(table.row_count, "行") : "—"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
