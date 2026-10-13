"use client";


const SAMPLES: { kind: string; text: string }[] = [
  { kind: "知识库检索", text: "最近有哪些市政道路类的招标公告？" },
  { kind: "知识库检索", text: "哪些项目接受联合体投标？条件是什么？" },
  { kind: "结构化统计", text: "预算超过一百万的项目有几个？" },
  { kind: "结构化统计", text: "中标金额最高的五个项目是哪些？" },
  { kind: "关系查询", text: "某公司中标了哪些项目？" },
  { kind: "关系查询", text: "有哪些关于空调的采购？" },
];

type Props = {
  emptyIndex: boolean;
  disabled?: boolean;
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

      {}
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
