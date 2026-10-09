"use client";

/**
 * 空状态：没有任何对话时，居中展示系统说明与示例问题。
 *
 * 【为什么单独抽成组件】
 *   它是「没有对话才出现」的一整块界面，和聊天消息区、输入区互不相干。
 *   抽出来后 page.tsx 只负责「什么时候渲染它」，具体长什么样由这里负责。
 *
 * 【和旧版的区别】
 *   旧版示例问题是常驻在输入框上方的一排 chip，不管有没有对话都一直显示；
 *   现在改为只在空状态出现，且**点击直接提问**（而不是只填进输入框）。
 */

/** 示例问题：覆盖语义检索 / 结构化统计 / 图谱关系三类问题 */
const SAMPLES = [
  { kind: "知识库检索", text: "最近有哪些市政道路类的招标公告？" },
  { kind: "知识库检索", text: "哪些项目接受联合体投标？条件是什么？" },
  { kind: "PostgreSQL 统计", text: "预算超过一百万的项目有几个？" },
  { kind: "知识图谱", text: "有哪些关于空调的采购？" },
  { kind: "知识图谱", text: "某公司中标了哪些项目？" },
];

type Props = {
  /** 知识库是否为空（为空时额外给一条提示） */
  emptyIndex: boolean;
  /** 正在回答时禁用示例按钮，避免重复提问 */
  disabled?: boolean;
  /** 点击示例问题：由父组件负责「直接发送」 */
  onPick: (text: string) => void;
};

export default function EmptyState({ emptyIndex, disabled, onPick }: Props) {
  return (
    <div className="empty">
      <div className="empty-logo">标</div>
      <h2>招投标智能问答机器人</h2>
      <p>知识库来自 MySQL 的招标公告、招投标项目、公司信息三张表。</p>
      <p>
        Agent 会先判断问题是否与招投标业务相关：相关则自动检索知识库并依据命中的原文作答，
        无关的闲聊则直接回答。回答下方会附上命中的原文来源。
      </p>
      <p>
        统计、计数、求和、排序、精确筛选这类结构化问题（如「预算超过一千万的项目有几个」），
        会改走 PostgreSQL 精确查询；回答下方可展开查看生成的 SQL 与结果。
      </p>
      <p>
        实体之间的关系类问题（如「有哪些关于空调的采购」「某公司中标了哪些项目」），
        会改走 Neo4j 知识图谱按关系查询；回答下方可展开查看生成的 Cypher 与结果。
      </p>

      {/* 示例问题：点击即提问（直接发送，无需再按回车） */}
      <div className="empty-samples">
        {SAMPLES.map((sample) => (
          <button
            className="chip"
            key={sample.text}
            title={`${sample.kind}：${sample.text}`}
            onClick={() => onPick(sample.text)}
            disabled={disabled}
            type="button"
          >
            {sample.text}
          </button>
        ))}
      </div>

      {emptyIndex && (
        <div className="notice notice-warn" style={{ marginTop: 18, textAlign: "left" }}>
          当前知识库为空（三张表还没灌数据，或尚未建库）。服务已可正常问答，
          但会直接提示「无可用数据」；灌入数据后点击左侧「重建知识库」即可。
        </div>
      )}
    </div>
  );
}