"use client";

import { useRef } from "react";
import type { ChatMode } from "@/lib/api";

/**
 * 底部输入区：多行自适应输入框 + 附件（图片）+ 发送按钮，
 * 以及输入框上方的两个简洁控件：联网搜索开关、推理模式三选一。
 *
 * 【为什么单独抽成组件】
 *   这一整块（控件 + 图片预览 + 输入行 + 提示）原本内联在 page.tsx 里，
 *   它只跟「怎么把一句话发出去」有关，跟消息列表无关，抽出来让主页面更薄。
 *
 * 【谁是受控组件】
 *   输入框的文本 input、待发送的图片、模式、联网开关都由 page.tsx 持有（受控），
 *   这里只负责渲染与触发回调；唯一留在本组件内部的是「文件选择框 fileInputRef」。
 *
 * 【为什么 textareaRef 由外面传进来】
 *   page.tsx 的「点击示例问题 / 填入提问框」需要在别处直接聚焦并让输入框长高，
 *   所以输入框的 ref 由父组件持有、以 props 传入；输入框的高度也在父组件统一维护。
 */

/** 允许上传的图片类型（与后端 /analyze-image 的校验保持一致） */
const IMAGE_ACCEPT = ".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp";

/** 推理模式三选一（与后端 mode 字段取值一致） */
const MODES: { value: ChatMode; label: string; title: string }[] = [
  { value: "react", label: "ReAct", title: "单轮 ReAct：工具由模型自主调用（最快）" },
  { value: "plan", label: "Plan-and-Solve", title: "先拆解子任务再逐步执行，最后综合成答案" },
  { value: "deep", label: "深度思考", title: "在 Plan-and-Solve 基础上增加自评与必要的修订" },
];

type Props = {
  /** 输入框文本（受控） */
  input: string;
  onInputChange: (value: string) => void;
  /** 提交：点击发送或按 Enter 时调用，参数是当前文本 */
  onSend: (text: string) => void;
  /** 正在生成回答（禁用发送/输入相关按钮） */
  busy: boolean;
  /** 图片正在解析（用于按钮文案「解析中…」） */
  analyzing: boolean;
  /** 待发送图片的预览（data URL），空表示没选图片 */
  imageUrl: string | null;
  /** 待发送图片的文件名（仅用于展示） */
  imageName?: string | null;
  /** 选好图片（已读成 data URL 由父组件处理） */
  onPickImage: (file: File) => void;
  onClearImage: () => void;
  /** 联网搜索开关 */
  webSearchOn: boolean;
  canWebSearch: boolean;
  onToggleWebSearch: () => void;
  /** 推理模式 */
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  /** 输入框 ref：由父组件持有，便于别处聚焦 */
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
};

export default function Composer({
  input,
  onInputChange,
  onSend,
  busy,
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

  /** Enter 发送，Shift + Enter 换行 */
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onSend(input);
    }
  };

  /** 选择图片：读成 data URL 交给父组件做预览 */
  const pickImage = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // 复位 input，否则连续选同一个文件不会触发 change
    e.target.value = "";
    if (file) onPickImage(file);
  };

  return (
    <div className="composer">
      <div className="composer-inner">
        <div className="chips">
          {/* 联网搜索开关：本地知识库无答案时，Agent 自动联网搜最新公告与法规 */}
          <button
            className={`chip chip-toggle ${webSearchOn ? "chip-on" : ""}`}
            type="button"
            onClick={onToggleWebSearch}
            disabled={busy || !canWebSearch}
            title={
              canWebSearch
                ? "开启后，本地知识库查不到时会自动联网搜索最新公告与法规更新"
                : "后端未配置 TAVILY_API_KEY，联网搜索不可用"
            }
            aria-pressed={webSearchOn}
          >
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
            <button className="chip" type="button" onClick={onClearImage} disabled={busy}>
              移除
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
            className="attach"
            type="button"
            title="上传图片（jpg / png / webp），由视觉模型解析内容"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy}
          >
            📎
          </button>
          <textarea
            ref={textareaRef}
            value={input}
            placeholder="问点什么，例如：最近有哪些市政道路类的招标公告？（也可只上传图片让模型先识别）"
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <button
            className="send"
            onClick={() => onSend(input)}
            disabled={busy || (!input.trim() && !imageUrl)}
            type="button"
          >
            {busy ? (analyzing ? "解析中…" : "生成中…") : "发送"}
          </button>
        </div>
        <div className="hint">Enter 发送 · Shift + Enter 换行 · 附件仅支持 jpg / png / webp（≤10MB）</div>
      </div>
    </div>
  );
}