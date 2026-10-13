"use client";

import { useEffect, useState } from "react";
import Icon from "@/components/ui/icons";
import { mdToHtml } from "@/lib/markdown";


type State = { status: "loading" | "ready" | "empty"; html: string };

export default function EvalCard() {
  const [state, setState] = useState<State>({ status: "loading", html: "" });

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch("/api/eval-report", { cache: "no-store" });
        const data = (await res.json()) as { available: boolean; markdown: string };
        if (!alive) return;
        if (data.available && data.markdown.trim()) {
          setState({ status: "ready", html: mdToHtml(data.markdown) });
        } else {
          setState({ status: "empty", html: "" });
        }
      } catch {
        if (alive) setState({ status: "empty", html: "" });
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="admin-card">
      <div className="admin-card-title">
        评测结果
        <span className="badge badge-plain">检索 · 路由 · 答案</span>
      </div>

      {state.status === "loading" && (
        <div>
          <div className="skeleton skel-line" />
          <div className="skeleton skel-line" />
          <div className="skeleton skel-line short" />
        </div>
      )}

      {state.status === "empty" && (
        <div className="history-hint">
          <Icon name="chart" size={14} /> 还没有评测报告。运行评测脚本后会在这里展示汇总指标。
        </div>
      )}

      {state.status === "ready" && (
        <div
          className="eval-body answer"
          style={{ maxHeight: 420, overflowY: "auto" }}
          dangerouslySetInnerHTML={{ __html: state.html }}
        />
      )}
    </div>
  );
}
