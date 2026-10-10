"use client";

import Icon, { type IconName } from "@/components/ui/icons";
import type { Health, KbStats, MetricsSummary, SchemaResponse } from "@/lib/api";

/**
 * 系统状态卡片区：按「模型 / 数据 / 工具」三组展示。
 *
 * 每张卡统一为「图标 + 名称 + 值 + 一句话说明 + 语义色状态点」：
 *   灰点 = 未启用，绿点 = 正常，红点 = 异常。
 * 缺失的值一律显示 `—`，避免出现「0 块 —」这类残缺拼接。
 */

type Tone = "ok" | "err" | "off";

type StatCard = {
  name: string;
  icon: IconName;
  value: string;
  desc: string;
  tone: Tone;
};

type Props = {
  health: Health | null;
  kb: KbStats | null;
  schema: SchemaResponse | null;
  /** 问答指标（/api/metrics）；拉取失败或还没拉时为 null，重排成功率卡显示「—」 */
  metrics?: MetricsSummary | null;
  /** 指标接口返回 401（未登录）：重排成功率卡显示「需登录后查看」 */
  metricsLocked?: boolean;
};

/** 空值统一成 `—` */
function dash(value: unknown): string {
  if (value === null || value === undefined) return "—";
  const text = String(value).trim();
  return text === "" ? "—" : text;
}

/** 千分位 + 单位 */
function fmtCount(value: number | undefined, unit: string): string {
  if (value === undefined || value === null) return "—";
  return `${value.toLocaleString("zh-CN")} ${unit}`;
}

export default function StatusCards({ health, kb, schema, metrics, metricsLocked }: Props) {
  const points = kb?.points ?? 0;
  const dim = kb?.vector_dim ?? 0;
  const pgSource = health?.kb_source === "postgres";
  const mysqlOk = Boolean(schema?.connected);

  // 重排成功率卡的三个口径：只统计「真正尝试过重排」的问答（成功 + 失败），
  // 缓存命中、候选不足没尝试的请求不进分母；一次都没试过时显示 —（灰点）。
  const rerankApplied = metrics?.rerank_applied ?? 0;
  const rerankFailed = metrics?.rerank_failed ?? 0;
  const rerankTried = rerankApplied + rerankFailed;
  const rerankRate = metrics?.rerank_success_rate ?? 0;

  const groups: { title: string; icon: IconName; cards: StatCard[] }[] = [
    {
      title: "模型",
      icon: "cpu",
      cards: [
        {
          name: "对话模型",
          icon: "message",
          value: health?.llm_configured ? dash(health.llm_model_display || health.llm_model) : "未配置",
          desc: "理解问题、调度工具并生成回答",
          tone: health?.llm_configured ? "ok" : "off",
        },
        {
          name: "重排模型",
          icon: "regenerate",
          value: health?.rerank_configured ? dash(health.rerank_model) : "未配置",
          desc: "对初步召回的来源重新排序",
          tone: health?.rerank_configured ? "ok" : "off",
        },
        {
          name: "重排成功率",
          icon: "chart",
          value: metricsLocked ? "需登录后查看" : rerankTried > 0 ? `${Math.round(rerankRate * 100)}%` : "—",
          desc: metricsLocked
            ? "指标接口已收进门禁，登录后展示"
            : rerankTried > 0
              ? `成功 ${rerankApplied} 次 · 失败降级 ${rerankFailed} 次`
              : "尚无重排记录",
          tone: metricsLocked ? "off" : rerankTried === 0 ? "off" : rerankRate >= 0.5 ? "ok" : "err",
        },
        {
          name: "向量模型",
          icon: "database",
          value: dim > 0 ? `${dash(kb?.embed_model ?? "内置模型")}` : "未建库",
          desc: dim > 0 ? `文本转成 ${dim} 维向量用于语义检索` : "建库后展示向量维度",
          tone: dim > 0 ? "ok" : "off",
        },
      ],
    },
    {
      title: "数据",
      icon: "database",
      cards: [
        {
          name: "知识库",
          icon: "database",
          value: fmtCount(kb?.points, "块"),
          desc: `关键词索引 ${fmtCount(kb?.bm25_docs, "篇")}`,
          tone: points > 0 ? "ok" : "off",
        },
        {
          name: "数据源",
          icon: "database",
          value: pgSource ? "PostgreSQL 副本" : mysqlOk ? "MySQL" : dash(schema?.database),
          desc: "建库管道从这里读取业务数据",
          tone: pgSource || mysqlOk ? "ok" : health?.mysql_configured ? "err" : "off",
        },
        {
          name: "检索策略",
          icon: "search",
          value: health?.rerank_configured ? "混合检索 + 重排" : "混合检索",
          desc: "向量与关键词两路召回后融合排序",
          tone: points > 0 ? "ok" : "off",
        },
      ],
    },
    {
      title: "工具",
      icon: "network",
      cards: [
        {
          name: "结构化统计",
          icon: "chart",
          value: health?.pg_configured ? "可用" : "未配置",
          desc: "预算、数量类问题的精确 SQL 统计",
          tone: health?.pg_configured ? "ok" : "off",
        },
        {
          name: "关系查询",
          icon: "network",
          value: health?.graph_configured ? "可用" : "未配置",
          desc: "公司、项目之间的关系类问题",
          tone: health?.graph_configured ? "ok" : "off",
        },
        {
          name: "联网搜索",
          icon: "external",
          value: health?.web_search_configured ? "可用" : "未配置",
          desc: "本地知识库查不到时补充最新公告",
          tone: health?.web_search_configured ? "ok" : "off",
        },
      ],
    },
  ];

  return (
    <>
      {groups.map((group) => (
        <div key={group.title}>
          <div className="stat-group-title">
            <Icon name={group.icon} size={14} />
            {group.title}
          </div>
          <div className="stat-grid">
            {group.cards.map((card) => (
              <div className="stat-card" key={card.name}>
                <div className="stat-top">
                  <span className={`stat-icon ${card.tone === "off" ? "is-off" : card.tone === "err" ? "is-err" : ""}`}>
                    <Icon name={card.icon} size={14} />
                  </span>
                  <span className="stat-name">{card.name}</span>
                  <span className="stat-dot-holder" title={card.tone === "ok" ? "正常" : card.tone === "err" ? "异常" : "未启用"}>
                    <span className={`dot ${card.tone === "ok" ? "dot-ok" : card.tone === "err" ? "dot-err" : "dot-off"}`} />
                  </span>
                </div>
                <div className="stat-value">{card.value}</div>
                <div className="stat-desc">{card.desc}</div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </>
  );
}
