"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Icon from "@/components/ui/icons";
import type { Source } from "@/lib/api";

/**
 * 右侧证据面板（本项目的「引用核心叙事」，对标 Perplexity 的来源面板）。
 *
 * 三种尺寸（由 data-size 驱动 CSS，见 globals.css 的 .evidence 规则）：
 *   rail —— 折叠条（默认）：只露一条竖排「证据 · N 条来源」，点击展开为半屏
 *   half —— 半屏抽屉：来源列表，每条可看名次对比
 *   full —— 全屏对比：宽度撑满，适合逐条细读
 *
 * 每条来源展示四层名次信息（这是「一眼看出重排有没有用」的关键）：
 *   向量名次 / BM25 名次 —— 两路召回各自把这条排在第几
 *   融合名次 —— 按 RRF 分数降序推出的「重排前」名次（后端重排后会打乱原始顺序，
 *               但 rrf_score 一直保留着，所以前端按分数排序即可还原融合名次）
 *   重排名次 —— 后端重排模型给出的名次（rerank_rank）；与融合名次的差值用
 *               ↑/↓ 徽标表达，例如「融合 #7 → 重排 #1 ↑6」表示重排把它提到了第 1
 *
 * 「不相关」标记：为后续反馈闭环留的口子——目前只在前端置灰该条，
 *   将来接反馈接口时把 (doc_id, 消息 id) 上报即可（见 toggleIrrelevant 内的注释）。
 */

export type EvidenceSize = "rail" | "half" | "full";

type Props = {
  /** 本轮回答引用的来源列表（来自 SSE 的 sources 事件） */
  sources: Source[];
  /** 本轮用户的提问原文：用于在命中片段里高亮问题中的词 */
  query: string;
  /** 面板当前尺寸（受控，状态在 page.tsx） */
  size: EvidenceSize;
  /** 切换面板尺寸 */
  onSizeChange: (size: EvidenceSize) => void;
  /** 关闭面板（仅半屏 / 全屏时可见） */
  onClose: () => void;
  /**
   * 定位请求：{ index: 第几条来源（0 起）, nonce: 递增序号 }。
   * nonce 每次定位 +1——即使定位同一条也要重新触发滚动与闪烁动画，所以用 nonce 而不是 index 做 effect 依赖。
   */
  locate: { index: number; nonce: number } | null;
};

/** 中文功能词（的 / 有 / 哪些…）：中文没有空格，整句会被粘成一个词，需要按这些字二次切开 */
const HIGHLIGHT_STOP =
  "的了吗呢么啊吧是在有和与及或对被把从向往到为以于其中上下个些怎何几许多少请问我你他她它们这那哪些什么怎么样是不是有没有查询查找搜索查一下看看相关关于";

/** 从提问里抽出用于高亮的词：按标点/空白切开，保留长度 ≥2 的词，最多 8 个 */
function extractTerms(query: string): string[] {
  // 只按标点切时，「市政道路工程的招标公告有哪些」会是一整个词、永远匹配不到片段——
  // 所以先按标点/空白切开（原逻辑），再按功能词把粘连的实词二次切开，高亮才真正生效
  const pieces = query.split(/[^一-龥A-Za-z0-9]+/);
  const terms = pieces.flatMap((piece) => piece.split(new RegExp(`[${HIGHLIGHT_STOP}]+`)));
  return Array.from(new Set(terms.filter((term) => term.length >= 2))).slice(0, 8);
}

/** 把命中片段里的关键词包上 <mark> 高亮；用捕获组 split，奇数位就是命中词 */
function highlightSnippet(text: string, terms: string[]): React.ReactNode[] {
  if (terms.length === 0) return [text];
  const pattern = terms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  let regex: RegExp;
  try {
    regex = new RegExp(`(${pattern})`, "gi");
  } catch {
    return [text];
  }
  return text.split(regex).map((part, i) =>
    i % 2 === 1 ? (
      <mark key={i}>{part}</mark>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

/** 取来源标题：优先标题类字段，其次首个非空文本字段（与 MessageBubble 保持一致） */
function pickTitle(source: Source): string {
  const entries = Object.entries(source.fields ?? {});
  const preferred = entries.find(
    ([key, value]) => /title|name|subject|标题|名称|项目名/i.test(key) && typeof value === "string" && value.trim(),
  );
  if (preferred) return String(preferred[1]);
  const firstText = entries.find(([, value]) => typeof value === "string" && value.trim());
  if (firstText) return String(firstText[1]);
  return `记录 ${source.primary_key ?? "—"}`;
}

/** 名次单元格：ranks 里没有该通道时显示 — */
function RankChip({ label, rank }: { label: string; rank: number | null }) {
  return (
    <span title={rank == null ? `${label}：该来源未参与此路召回` : `${label}名次：第 ${rank} 位`}>
      {label} #{rank ?? "—"}
    </span>
  );
}

export default function EvidencePanel({ sources, query, size, onSizeChange, onClose, locate }: Props) {
  const listRef = useRef<HTMLDivElement>(null);
  /** 正在闪烁高亮的来源下标（定位时短暂高亮） */
  const [flashIndex, setFlashIndex] = useState<number | null>(null);
  /** 被用户标记「不相关」的来源下标集合（仅前端态，反馈接口留口见下） */
  const [irrelevant, setIrrelevant] = useState<ReadonlySet<number>>(new Set());

  /** 高亮词只随提问变化，这里 useMemo 避免每次渲染重算 */
  const terms = useMemo(() => extractTerms(query), [query]);

  /**
   * 融合名次（重排前名次）：按 RRF 分数降序排列还原。
   * 背景：后端重排后会把 hits 重新排序，rerank_rank 记录的是重排后的名次；
   * 但每条的 rrf_score（RRF 融合分）始终保留，按它降序就是「重排前的融合名次」。
   * rankByIndex[i] = 第 i 条来源在融合排序里的名次（1 起）。
   */
  const rankByIndex = useMemo(() => {
    const order = sources
      .map((source, index) => ({ index, score: source.rrf_score ?? 0 }))
      .sort((a, b) => b.score - a.score);
    const ranks = new Map<number, number>();
    order.forEach((entry, position) => ranks.set(entry.index, position + 1));
    return ranks;
  }, [sources]);

  /** 是否有重排结果（决定要不要显示「重排 →」对比徽标） */
  const hasRerank = sources.some((source) => typeof source.rerank_rank === "number");

  // 定位请求到来：滚动到对应条目并闪烁 1.6s（动画时长与 CSS 的 ev-flash 保持一致）
  useEffect(() => {
    if (!locate) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-ev-index="${locate.index}"]`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 定位到来时的瞬时高亮，随 locate 变化重置、1.7s 后自动清除，属于对 props 变化的响应
    setFlashIndex(locate.index);
    const timer = window.setTimeout(() => setFlashIndex(null), 1700);
    return () => window.clearTimeout(timer);
  }, [locate]);

  /**
   * 标记 / 撤销「不相关」。
   * 【反馈闭环留口】将来在这里追加上报即可，例如：
   *   void api.reportIrrelevant({ doc_id, message_id, relevant: false })
   * 目前只做前端置灰（ev-dim），不影响其它任何逻辑。
   */
  const toggleIrrelevant = (index: number) => {
    setIrrelevant((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  // ---------- 折叠条（rail）：竖排文字按钮，点击展开为半屏 ---------- //
  if (size === "rail") {
    return (
      <aside className="evidence" data-size="rail" aria-label="证据面板（折叠）">
        <button
          className="evidence-rail"
          type="button"
          onClick={() => onSizeChange("half")}
          title="展开证据面板"
          data-testid="evidence-rail"
        >
          <Icon name="panel" size={16} />
          <span className="evidence-rail-text">证据 · {sources.length} 条来源</span>
        </button>
      </aside>
    );
  }

  // ---------- 半屏 / 全屏：完整列表 ---------- //
  return (
    <aside className="evidence" data-size={size} aria-label="证据面板">
      <div className="evidence-body">
        <div className="evidence-head">
          <div className="evidence-title">
            本轮来源
            <span className="badge badge-plain" style={{ marginLeft: "var(--sp-8)" }}>
              {sources.length} 条
            </span>
            {hasRerank && <span className="badge badge-blue">已重排</span>}
          </div>
          {/* 尺寸切换：半屏 / 全屏（名次对比列在全屏下更舒展） */}
          <div className="evidence-resize" role="group" aria-label="面板大小">
            <button
              className={`seg-item ${size === "half" ? "seg-active" : ""}`}
              type="button"
              onClick={() => onSizeChange("half")}
              title="半屏"
              aria-pressed={size === "half"}
            >
              <Icon name="panel" size={13} />
            </button>
            <button
              className={`seg-item ${size === "full" ? "seg-active" : ""}`}
              type="button"
              onClick={() => onSizeChange("full")}
              title="全屏对比"
              aria-pressed={size === "full"}
            >
              <Icon name="external" size={13} />
            </button>
          </div>
          <button className="icon-btn" type="button" onClick={onClose} aria-label="收起证据面板" data-testid="evidence-close">
            <Icon name="x" size={16} />
          </button>
        </div>

        {sources.length === 0 ? (
          <div className="evidence-empty">这一轮回答没有引用知识库来源。</div>
        ) : (
          <div className="evidence-list" ref={listRef} data-testid="evidence-list">
            {sources.map((source, index) => {
              const fusedRank = rankByIndex.get(index) ?? index + 1;
              const rerankRank = typeof source.rerank_rank === "number" ? source.rerank_rank : null;
              // 重排前后名次差：正数表示重排把它往前提了（up），负数是往后压了（down）
              const shift = rerankRank != null ? fusedRank - rerankRank : null;
              const similarity = typeof source.scores?.dense === "number" ? source.scores.dense : null;
              const isIrrelevant = irrelevant.has(index);
              return (
                <article
                  className={`ev-item ${flashIndex === index ? "ev-flash" : ""} ${isIrrelevant ? "ev-dim" : ""}`}
                  data-ev-index={index}
                  key={`${source.doc_id}-${source.chunk_index}`}
                >
                  <div className="ev-head">
                    <span className="src-n">{index + 1}</span>
                    <span className="ev-title" title={pickTitle(source)}>
                      {pickTitle(source)}
                    </span>
                  </div>
                  <div className="ev-docid">
                    {source.doc_id}
                    {source.chunk_total > 1 && ` · 第 ${source.chunk_index + 1}/${source.chunk_total} 块`}
                  </div>

                  {/* 四层名次：两路召回 → 融合 → 重排（前后对比一眼可见） */}
                  <div className="ev-ranks">
                    <RankChip label="向量" rank={source.ranks?.dense ?? null} />
                    <RankChip label="BM25" rank={source.ranks?.bm25 ?? null} />
                    <span title={`按融合分数排出的名次（重排前）：第 ${fusedRank} 位`}>融合 #{fusedRank}</span>
                    {rerankRank != null ? (
                      <span
                        className={`ev-rank-shift ${shift != null && shift > 0 ? "up" : shift != null && shift < 0 ? "down" : ""}`}
                        title={`重排模型给出的名次：第 ${rerankRank} 位（融合时是第 ${fusedRank} 位）`}
                      >
                        重排 #{rerankRank}
                        {shift != null && shift > 0 && <span aria-label={`提升了 ${shift} 名`}>↑{shift}</span>}
                        {shift != null && shift < 0 && <span aria-label={`下降了 ${-shift} 名`}>↓{-shift}</span>}
                        {shift === 0 && <span>（名次未变）</span>}
                      </span>
                    ) : (
                      // 后端未返回重排名次（未配置重排 / 候选不足 / 调用失败降级）：
                      // 第四层的位置照样留出来并标注「未启用」，四层名次始终对齐可见
                      <span
                        className="ev-rank-none"
                        title="本轮未启用大模型重排（未配置 / 候选不足 / 调用失败已降级）"
                      >
                        重排 未启用
                      </span>
                    )}
                  </div>

                  {/* 命中片段：提问中的关键词高亮 */}
                  {source.text && (
                    <div className="ev-snippet">{highlightSnippet(source.text, terms)}</div>
                  )}

                  <div className="ev-foot">
                    {similarity != null && (
                      <span className="badge badge-plain" title="向量相似度（越高越相关）">
                        相似度 {similarity.toFixed(2)}
                      </span>
                    )}
                    {/* 反馈闭环入口：标记后该条置灰；后续接后端接口即可上报 */}
                    <button
                      className={`act-btn ${isIrrelevant ? "act-on" : ""}`}
                      type="button"
                      onClick={() => toggleIrrelevant(index)}
                      aria-pressed={isIrrelevant}
                      title={isIrrelevant ? "点击撤销标记" : "这条内容与问题无关？标记后可帮助改进检索"}
                    >
                      {isIrrelevant ? "已标不相关（撤销）" : "不相关"}
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </aside>
  );
}
