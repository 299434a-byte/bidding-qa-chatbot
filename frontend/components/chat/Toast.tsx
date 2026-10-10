"use client";

import { useCallback, useRef, useState } from "react";

/**
 * 轻量 Toast：右下角、3 秒自动消失。
 * 用法：const [toasts, pushToast] = useToasts(); 页面里渲染 <ToastStack toasts={toasts} />。
 *
 * 【为什么不用 context】
 *   全站只有两个页面需要 toast（对话页、后台页），直接用 hook 返回值传递更直白，
 *   也避免为一个小功能引入 provider 层。
 */

export type ToastKind = "success" | "error" | "info";

export type ToastItem = {
  id: number;
  text: string;
  kind: ToastKind;
};

const DOT_CLASS: Record<ToastKind, string> = {
  success: "dot-ok",
  error: "dot-err",
  info: "dot-off",
};

export function useToasts(duration = 3000) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const pushToast = useCallback(
    (text: string, kind: ToastKind = "info") => {
      seq.current += 1;
      const id = seq.current;
      setToasts((prev) => [...prev.slice(-2), { id, text, kind }]);
      window.setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, duration);
    },
    [duration],
  );

  return [toasts, pushToast] as const;
}

export default function ToastStack({ toasts }: { toasts: ToastItem[] }) {
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div className="toast" key={t.id}>
          <span className={`dot ${DOT_CLASS[t.kind]}`} />
          {t.text}
        </div>
      ))}
    </div>
  );
}