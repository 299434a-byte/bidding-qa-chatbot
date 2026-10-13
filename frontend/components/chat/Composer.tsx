"use client";

import { useRef } from "react";
import Icon, { type IconName } from "@/components/ui/icons";
import type { ChatMode } from "@/lib/api";


const IMAGE_ACCEPT = ".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp";

const MODES: { value: ChatMode; label: string; title: string; icon: IconName }[] = [
  { value: "react", label: "ReAct", title: "单轮 ReAct：工具由模型自主调用（最快）", icon: "regenerate" },
  { value: "plan", label: "Plan-and-Solve", title: "先拆解子任务再逐步执行，最后综合成答案", icon: "chart" },
  { value: "deep", label: "深度思考", title: "在 Plan-and-Solve 基础上增加自评与必要的修订", icon: "cpu" },
];

type Props = {
  input: string;
  onInputChange: (value: string) => void;
  onSend: (text: string) => void;
  busy: boolean;
  onStop: () => void;
  analyzing: boolean;
  imageUrl: string | null;
  imageName?: string | null;
  onPickImage: (file: File) => void;
  onClearImage: () => void;
  webSearchOn: boolean;
  canWebSearch: boolean;
  onToggleWebSearch: () => void;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
};

export default function Composer({
  input,
  onInputChange,
  onSend,
  busy,
  onStop,
  analyzing,
  imageUrl,
  imageName,
  onPickImage,
  onClearImage,
  webSearchOn,
  canWebSearch,
  onToggleWebSearch,
  mode,
  onModeChange,
  textareaRef,
}: Props) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onSend(input);
    }
  };

  const pickImage = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (file) onPickImage(file);
  };

  return (
    <div className="composer">
      <div className="composer-inner">
        <div className="toolrow">
          {}
          <button
            className={`chip ${webSearchOn ? "chip-on" : ""}`}
            type="button"
            onClick={onToggleWebSearch}
            disabled={busy || !canWebSearch}
            title={
              canWebSearch
                ? "开启后，本地知识库查不到时会自动联网搜索最新公告与法规更新"
                : "当前未开启联网搜索能力"
            }
            aria-pressed={webSearchOn}
          >
            <Icon name="external" size={13} />
            联网搜索 {webSearchOn ? "开" : "关"}
          </button>
          {/* 推理模式三选一：ReAct / Plan-and-Solve / 深度思考 */}
          <div className="seg" role="group" aria-label="推理模式">
            {MODES.map((item) => (
              <button
                key={item.value}
                className={`seg-item ${mode === item.value ? "seg-active" : ""}`}
                type="button"
                onClick={() => onModeChange(item.value)}
                disabled={busy}
                title={item.title}
                aria-pressed={mode === item.value}
              >
                <Icon name={item.icon} size={13} />
                {item.label}
              </button>
            ))}
          </div>
        </div>

        {/* 已选图片：预览缩略图 + 移除（发送前可随时更换） */}
        {imageUrl && (
          <div className="attach-preview">
            <img src={imageUrl} alt="待解析图片预览" />
            <div className="attach-preview-name" title={imageName ?? undefined}>
              {imageName ?? "已选择图片"}
            </div>
            <button className="icon-btn" type="button" onClick={onClearImage} disabled={busy} aria-label="移除图片">
              <Icon name="x" size={15} />
            </button>
          </div>
        )}

        <div className="input-row">
          <input
            ref={fileInputRef}
            type="file"
            accept={IMAGE_ACCEPT}
            style={{ display: "none" }}
            onChange={pickImage}
          />
          <button
            className="icon-btn attach-btn"
            type="button"
            title="上传图片（jpg / png / webp），由视觉模型解析内容"
            aria-label="上传图片"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy}
          >
            <Icon name="image" size={18} />
          </button>
          <textarea
            className="chat-input"
            ref={textareaRef}
            value={input}
            placeholder="问点什么，例如：最近有哪些市政道路类的招标公告？（也可只上传图片让模型先识别）"
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={onKeyDown}
          />
          {busy ? (
            <button className="send-btn stop-btn" type="button" onClick={onStop}>
              <Icon name="stop" size={15} />
              {analyzing ? "停止解析" : "停止生成"}
            </button>
          ) : (
            <button
              className="send-btn"
              type="button"
              onClick={() => onSend(input)}
              disabled={!input.trim() && !imageUrl}
            >
              <Icon name="send" size={15} />
              发送
            </button>
          )}
        </div>
        <div className="hint">Enter 发送 · Shift + Enter 换行 · 附件支持 jpg / png / webp（≤10MB）</div>
      </div>
    </div>
  );
}
