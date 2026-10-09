"use client";

import type { GraphQueryInfo, SqlQueryInfo, WebSearchInfo } from "@/lib/api";

/**
 * 工具调用痕迹：把 Agent 调用 PostgreSQL / Neo4j / 联网搜索 三类工具的结果，
 * 以「可折叠」的形式展示在回答下方。
 *
 * 【为什么单独抽成组件】
 *   这三块结构相似、加起来两百多行，抽出来后 page.tsx 只留一句 <ToolTrace/>。
 *
 * 【为什么把 SQL 与 Cypher 合并成一个 QueryBlock】
 *   两者的折叠结构完全一样（标题 + 代码块 + 结果表格 + 提示），只是字段名不同
 *   （PostgreSQL 是 sql，Neo4j 是 cypher）。合并后只需维护一份渲染逻辑。
 *
 * 【为什么不再写死浅色】
 *   原先内联样式里写死了 #fff / #e2e8f0 等浅色，在深色主题下很刺眼；
 *   现在统一改用 globals.css 里新增的 .trace-* 类，跟随主题变量。
 */

/** 结果表格里的一行（列名 -> 值） */
type Row = Record<string, unknown>;

/** 单个查询块：标题 + 代码（SQL/Cypher）+ 结果表格 */
function QueryBlock({
  index,
  code,
  error,
  columns,
  rows,
  rowCount,
}: {
  index: number;
  code: string;
  error?: string | null;
  columns: string[];
  rows: Row[];
  rowCount: number;
}) {
  return (
    <details className="trace-item">
      <summary className="trace-summary">
        查询 {index + 1}
        {error ? " · 失败" : ` · 返回 ${rowCount} 行`}
      </summary>
      <pre className="trace-code">{code}</pre>
      {error && (
        <div className="notice notice-danger" style={{ marginTop: 8 }}>
          {error}
        </div>
      )}
      {!error && rows.length > 0 && (
        <div className="trace-scroll">
          <table className="trace-table">
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column}>{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {columns.map((column) => (
                    <td key={column}>
                      {row[column] === null || row[column] === undefined ? "" : String(row[column])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {rowCount > rows.length && (
            <div className="trace-note">仅展示前 {rows.length} 行（共 {rowCount} 行）</div>
          )}
        </div>
      )}
      {!error && rows.length === 0 && (
        <div className="trace-note">
          {rowCount > 0
            ? `查询成功，共 ${rowCount} 行（历史记录未保存结果明细）。`
            : "查询成功，返回 0 行。"}
        </div>
      )}
    </details>
  );
}

type Props = {
  sqlQueries?: SqlQueryInfo[];
  graphQueries?: GraphQueryInfo[];
  webSearches?: WebSearchInfo[];
};

export default function ToolTrace({ sqlQueries, graphQueries, webSearches }: Props) {
  return (
    <>
      {/* PostgreSQL：可折叠展示生成的 SQL 与结果表格 */}
      {sqlQueries && sqlQueries.length > 0 && (
        <div className="sources">
          <div className="sources-title">
            PostgreSQL 查询 · {sqlQueries.length} 次
            <span className="badge badge-plain">点击展开 SQL 与结果</span>
          </div>
          {sqlQueries.map((query, index) => (
            <QueryBlock
              key={index}
              index={index}
              code={query.sql || "（未生成 SQL）"}
              error={query.error}
              columns={query.columns}
              rows={query.rows}
              rowCount={query.rowCount}
            />
          ))}
        </div>
      )}

      {/* Neo4j：可折叠展示生成的 Cypher 与结果（列名从结果第一行推出） */}
      {graphQueries && graphQueries.length > 0 && (
        <div className="sources">
          <div className="sources-title">
            知识图谱查询 · {graphQueries.length} 次
            <span className="badge badge-plain">点击展开 Cypher 与结果</span>
          </div>
          {graphQueries.map((query, index) => (
            <QueryBlock
              key={index}
              index={index}
              code={query.cypher || "（未生成 Cypher）"}
              error={query.error}
              columns={query.rows.length > 0 ? Object.keys(query.rows[0]) : []}
              rows={query.rows}
              rowCount={query.rowCount}
            />
          ))}
        </div>
      )}

      {/* 联网搜索：来源链接 + 相关性评分（Tavily score，0~1） */}
      {webSearches && webSearches.length > 0 && (
        <div className="sources">
          <div className="sources-title">
            联网搜索结果 · {webSearches.reduce((n, s) => n + s.results.length, 0)} 条
            <span className="badge badge-plain">点击展开来源与相关性评分</span>
          </div>
          {webSearches.map((search, searchIndex) => (
            <details className="trace-item" key={searchIndex}>
              <summary className="trace-summary">
                搜索 {searchIndex + 1} · 关键词「{search.query || "—"}」
                {search.error ? " · 失败" : ` · ${search.results.length} 条`}
              </summary>
              {search.error && (
                <div className="notice notice-danger" style={{ marginTop: 8 }}>
                  {search.error}
                </div>
              )}
              {!search.error && search.results.length === 0 && (
                <div className="trace-note">搜索成功，但没有返回结果。</div>
              )}
              {search.results.map((item, itemIndex) => (
                <div className="web-item" key={`${item.url}-${itemIndex}`}>
                  <div className="web-item-head">
                    <a className="web-item-title" href={item.url} target="_blank" rel="noreferrer">
                      {item.title || item.url}
                    </a>
                    <span className="badge badge-mint" title="Tavily 相关性评分（0~1，越大越相关）">
                      相关性 {item.score.toFixed(2)}
                    </span>
                  </div>
                  <div className="web-item-score">
                    <span style={{ width: `${Math.max(0, Math.min(1, item.score)) * 100}%` }} />
                  </div>
                  <div className="web-item-url">{item.url}</div>
                  <div className="web-item-snippet">{item.content}</div>
                </div>
              ))}
            </details>
          ))}
        </div>
      )}
    </>
  );
}