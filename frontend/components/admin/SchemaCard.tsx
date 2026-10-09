"use client";

import { useState } from "react";
import type { SchemaResponse } from "@/lib/api";

/**
 * MySQL 表结构卡片：每张表一个「可展开」的小卡片，点标题展开字段明细（保留原来的折叠交互），
 * 下方再列出库内其他表（核对表名用）。
 *
 * 【为什么单独抽成组件】
 *   原来这块在主对话页左栏，现按改造方案搬到后台页；折叠状态 openTable 只属于本组件，
 *   放在内部即可，不需要父组件知道。
 */

type Props = {
  schema: SchemaResponse | null;
};

export default function SchemaCard({ schema }: Props) {
  const [openTable, setOpenTable] = useState<string | null>(null);
  const tables = schema?.tables ? Object.values(schema.tables) : [];

  return (
    <div className="admin-card">
      <div className="admin-card-title">
        MySQL 表结构
        <span className="badge badge-plain">点击展开字段</span>
      </div>

      {!schema?.connected && (
        <div className="notice notice-warn">{schema?.error ?? "尚未获取到表结构（请检查 .env 中的 MySQL 配置）"}</div>
      )}

      {tables.map((table) => (
        <div className="schema-table" key={table.table}>
          <div
            className="schema-head"
            onClick={() => setOpenTable((cur) => (cur === table.table ? null : table.table))}
          >
            <span>
              {table.label ?? table.table}
              <span style={{ color: "var(--text-faint)", fontSize: 11, marginLeft: 6 }}>{table.table}</span>
            </span>
            <span>
              {table.exists ? (
                <span className="badge badge-plain">{table.row_count ?? 0} 行</span>
              ) : (
                <span className="badge badge-danger">不存在</span>
              )}
            </span>
          </div>
          {openTable === table.table && table.columns && (
            <div className="schema-body">
              {table.columns.map((col) => (
                <div className="col-row" key={col.name}>
                  <span className="col-name">
                    {col.key === "PRI" ? "🔑 " : ""}
                    {col.name}
                  </span>
                  <span className="col-type">{col.column_type}</span>
                  <span className="col-comment">{col.comment || "—"}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}

      {schema?.available_tables && schema.available_tables.length > 0 && (
        <div className="card" style={{ marginTop: 4 }}>
          <div className="row-key" style={{ marginBottom: 4 }}>
            库内其他表（核对表名用）
          </div>
          {schema.available_tables.map((t) => (
            <div className="row" key={t.table}>
              <span className="row-key">{t.table}</span>
              <span className="row-val">≈{t.approx_rows}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}