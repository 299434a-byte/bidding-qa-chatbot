"use client";

/**
 * 空状态：没有任何对话时，居中展示价值主张与示例问题。
 *
 * 【为什么单独抽成组件】
 *   它是「没有对话才出现」的一整块界面，和聊天消息区、输入区互不相干。
 *   抽出来后 page.tsx 只负责「什么时候渲染它」，具体长什么样由这里负责。
 *
 * 【文案原则】
 *   只说用户看得懂的事（能回答哪些问题、点示例即可提问），
 *   不出现数据库 / 组件名 / 端口等实现细节。
 */

/** 示例问题：覆盖知识库检索 / 结构化统计 / 关系查询三类问题 */
const SAMPLES: { kind: string; text: string }[] = [
  { kind: "知识库检索", text: "最近有哪些市政道路类的招标公告？" },
  { kind: "知识库检索", text: "哪些项目接受联合体投标？条件是什么？" },
  { kind: "结构化统计", text: "预算超过一百万的项目有几个？" },
  { kind: "结构化统计", text: "中标金额最高的五个项目是哪些？" },
  { kind: "关系查询", text: "某公司中标了哪些项目？" },
  { kind: "关系查询", text: "有哪些关于空调的采购？" },
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
      <h2>招投标智能问答</h2>
      <p className="empty-sub">
        用一句日常语言提问，系统会在招标公告、招投标项目与公司信息里帮你查找，
        并给出带原文出处的回答。也可以直接上传图片，让模型先识别内容。
      </p>

      {/* 示例问题：点击即提问（直接发送，无需再按回车） */}
      <div className="empty-grid">
        {SAMPLES.map((sample) => (
          <button
            className="empty-q"
            key={sample.text}
            title={`${sample.kind}：${sample.text}`}
            onClick={() => onPick(sample.text)}
            disabled={disabled}
            type="button"
          >
            <span>{sample.text}</span>
            <span className="empty-q-kind">{sample.kind}</span>
          </button>
        ))}
      </div>

      {emptyIndex && (
        <div className="notice notice-warn" style={{ marginTop: 18, textAlign: "left" }}>
          当前还没有可检索的资料，问答可以正常提问，但会提示「暂无可用数据」。
          待资料入库后即可基于原文作答。
        </div>
      )}
    </div>
  );
}
