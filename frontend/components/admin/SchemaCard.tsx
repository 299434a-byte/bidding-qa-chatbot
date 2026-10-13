"use client";

import { useState } from "react";
import type { SchemaResponse } from "@/lib/api";


type Props = {
  schema: SchemaResponse | null;
};

export default function SchemaCard({ schema }: Props) {
  const [openTable, setOpenTable] = useState<string | null>(null);
  const tables = schema?.tables ? Object.values(schema.tables) : [];

  return (
    <div className="admin-card">
      <div className="admin-card-title">
        数据表结构
        <span className="badge badge-plain">点击展开字段</span>
      </div>

      {!schema?.connected && (
        <div className="notice notice-warn">暂时无法读取业务数据表，稍后重试或到后台日志查看原因。</div>
      )}

      {tables.map((table) => (
        <div className="schema-table" key={table.table}>
          <div
            className="schema-head"
            onClick={() => setOpenTable((cur) => (cur === table.table ? null : table.table))}
          >
            <span>{table.label ?? "业务数据表"}</span>
            <span>
              {table.exists ? (
                <span className="badge badge-plain">{(table.row_count ?? 0).toLocaleString("zh-CN")} 行</span>
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
                    {col.key === "PRI" ? "主键 · " : ""}
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
        <div className="card" style={{ marginTop: "var(--sp-4)" }}>
          <div className="row-key" style={{ marginBottom: "var(--sp-4)" }}>
            库内其他表（核对表名用）
          </div>
          {schema.available_tables.map((t) => (
            <div className="row" key={t.table}>
              <span className="row-key">{t.table}</span>
              <span className="row-val">≈{(t.approx_rows ?? 0).toLocaleString("zh-CN")}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}