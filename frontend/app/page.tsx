"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import AuthDialog from "@/components/AuthDialog";
import Icon from "@/components/ui/icons";
import ConversationRail from "@/components/chat/ConversationRail";
import EmptyState from "@/components/chat/EmptyState";
import MessageBubble, { type Message } from "@/components/chat/MessageBubble";
import Composer from "@/components/chat/Composer";
import ChatHeader from "@/components/chat/ChatHeader";
import ToastStack, { useToasts } from "@/components/chat/Toast";
import EvidencePanel, { type EvidenceSize } from "@/components/EvidencePanel";
import {
  analyzeImage,
  api,
  clearToken,
  getToken,
  streamAgentChat,
  type ChatMode,
  type ChatTurn,
  type Health,
  type KbStats,
  type PlanStepRun,
  type User,
} from "@/lib/api";
import { parseTs } from "@/lib/timefmt";

const uid = () => Math.random().toString(36).slice(2, 10);

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [kb, setKb] = useState<KbStats | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);

  const [imageFile, setImageFile] = useState<File | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [webSearch, setWebSearch] = useState<boolean | null>(null);
  const [mode, setMode] = useState<ChatMode>("react");

  const [user, setUser] = useState<User | null>(null);
  const [authOpen, setAuthOpen] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0);
  const [highlightId, setHighlightId] = useState<string | null>(null);
  const [railOpen, setRailOpen] = useState(false);

  const [toasts, pushToast] = useToasts();

  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const deltaBufRef = useRef<{ content: string; revision: string; byCitation: boolean } | null>(null);
  const flushTimerRef = useRef<number | null>(null);

  const atBottomRef = useRef(true);
  const [showJumpLatest, setShowJumpLatest] = useState(false);
  const [showJumpTop, setShowJumpTop] = useState(false);

  const [evidence, setEvidence] = useState<{
    msgId: string;
    size: EvidenceSize;
    index: number;
    nonce: number;
  } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, k] = await Promise.all([api.health(), api.kbStats()]);
      setHealth(h);
      setKb(k);
      setBanner(null);
    } catch {
      setBanner("服务暂时不可用，请检查网络或稍后重试。");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!atBottomRef.current) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => {
    if (!getToken()) return;
    let alive = true;
    void (async () => {
      try {
        const res = await api.me();
        if (alive) setUser(res.user);
      } catch {
        clearToken();
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);

  const clearDeltaQueue = useCallback(() => {
    if (flushTimerRef.current != null) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    deltaBufRef.current = null;
  }, []);

  useEffect(() => {
    if (!highlightId) return;
    const el = document.getElementById(`msg-${highlightId}`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
    const timer = setTimeout(() => setHighlightId(null), 2600);
    return () => clearTimeout(timer);
  }, [highlightId]);

  // ------------------------------ 交互 ------------------------------ //
  /** 注销：先通知后端作废令牌，再清掉本地状态（后端失败也照样清本地） */
  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* 令牌可能已过期，忽略 */
    }
    clearToken();
    setUser(null);
    setConversationId(null);
    setHistoryRefreshKey((k) => k + 1);
  }, []);

  /** 切换某条回复采纳的版本（决定下一轮回传正文还是修订稿） */
  const setAdopted = useCallback((id: string, version: "v1" | "v2") => {
    setMessages((prev) => prev.map((msg) => (msg.id === id ? { ...msg, adoptedVersion: version } : msg)));
  }, []);

  /** 新建对话：中断进行中的流式请求，再清空消息与当前会话 id，模式保持用户当前选择 */
  const newConversation = useCallback(() => {
    abortRef.current?.abort();
    clearDeltaQueue();
    setEvidence(null);
    setMessages([]);
    setConversationId(null);
    setHighlightId(null);
  }, [clearDeltaQueue]);

  /** 恢复历史会话：拉取详情 -> 按 meta 重建消息卡片 -> 采用该会话模式 -> 定位高亮 */
  const restoreConversation = useCallback(async (id: string, highlightMessageId?: string) => {
    // 切会话前先中断当前流，避免旧回答继续往新会话里写
    abortRef.current?.abort();
    clearDeltaQueue();
    try {
      const res = await api.conversationDetail(id);
      const restored: Message[] = res.messages.map((item) => {
        if (item.role === "user") {
          // created_at 兼容 SQLite 的 "YYYY-MM-DD HH:MM:SS" 与 ISO 两种写法；解析失败按无时间处理
          return {
            id: item.id,
            role: "user" as const,
            content: item.content,
            ts: item.created_at ? parseTs(item.created_at)?.getTime() ?? null : null,
          };
        }
        const meta = item.meta;
        // 把工具调用与结果按名字配对，补回「命中 N 条」徽标
        const toolCalls = meta?.tool_calls?.map((call) => ({
          name: call.name,
          query: call.query,
          count: meta.tool_results?.find((r) => r.name === call.name)?.count,
        }));
        return {
          id: item.id,
          role: "assistant" as const,
          content: item.content,
          ts: item.created_at ? parseTs(item.created_at)?.getTime() ?? null : null,
          mode: meta?.done_mode ?? meta?.mode ?? res.conversation.mode,
          toolCalls,
          sources: meta?.sources,
          // 恢复时后端只保留了 sql / row_count / error，没有结果明细行，故 rows 置空
          sqlQueries: meta?.sql
            ? [
                {
                  sql: meta.sql.sql,
                  columns: [],
                  rowCount: meta.sql.row_count,
                  rows: [],
                  error: meta.sql.error ?? null,
                },
              ]
            : undefined,
          graphQueries: meta?.graph
            ? [
                {
                  cypher: meta.graph.cypher,
                  rowCount: meta.graph.row_count,
                  rows: [],
                  error: meta.graph.error ?? null,
                },
              ]
            : undefined,
          webSearches: meta?.web_search
            ? [
                {
                  query: meta.web_search.query,
                  results: meta.web_search.results ?? [],
                  error: meta.web_search.error ?? null,
                },
              ]
            : undefined,
          plan: meta?.plan,
          steps: meta?.steps,
          reflections: meta?.reflections,
          revision: meta?.revision ?? undefined,
          // 历史里只存了错误原文，包成结构化错误块（人话 + 技术详情折叠）
          error: meta?.error
            ? { kind: "server" as const, message: "该回答生成时出错了。", detail: meta.error }
            : undefined,
        };
      });
      // 恢复历史视为一次「新定位」：贴底跟随到最新一条，浮动按钮状态随后由 scroll 事件校正
      atBottomRef.current = true;
      setMessages(restored);
      setConversationId(res.conversation.id);
      // 采用该会话记录的模式
      setMode((res.conversation.mode as ChatMode) ?? "react");
      setBanner(null);
      setEvidence(null);
      // 移动端：打开会话后收起抽屉
      setRailOpen(false);
      if (highlightMessageId) setHighlightId(highlightMessageId);
    } catch {
      setBanner("恢复会话失败，请稍后重试。");
    }
  }, [clearDeltaQueue]);

  /** 后端没配 TAVILY_API_KEY 时联网搜索不可用，开关置灰 */
  const canWebSearch = Boolean(health?.web_search_configured);
  /** 实际生效的开关值：用户没手动改过就沿用后端默认值 */
  const webSearchOn = canWebSearch && (webSearch ?? health?.web_search_default ?? false);

  /**
   * 真正发送一轮提问。
   * base 省略：普通发送，用当前 messages 作为上下文与追加基准；
   * base 传入：重新生成，用 messages.slice(0, i) 作为上下文并从这里重建，避免把旧回答喂回模型。
   */
  const runSend = useCallback(
    async (text: string, base?: Message[]) => {
      const question = text.trim();
      // 重新生成时不带上「当前待发送的图片」
      const file = base ? null : imageFile;
      const baseMsgs = base ?? messages;
      // 纯图片（不带文字）也允许发送。
      // 用 busyRef（同步）而非 busy（异步 state）做互斥：同一帧内连按两次 Enter 只放行一次。
      if ((!question && !file) || busyRef.current) return;

      const assistantId = uid();
      const patch = (updater: (msg: Message) => Message) =>
        setMessages((prev) => prev.map((msg) => (msg.id === assistantId ? updater(msg) : msg)));

      /**
       * 把一段 delta 放进缓冲，并在没有挂起定时器时安排一次冲刷。
       * 正文与 revision 两个通道分别累计、同一次冲刷一起落库。
       */
      const bufferDelta = (channel: string, text: string, reason?: string) => {
        const buf = deltaBufRef.current ?? { content: "", revision: "", byCitation: false };
        if (channel === "revision") {
          buf.revision += text;
          // reason="citation" 表示这份修订稿来自引用校验；深度模式的修订稿不带该标记。
          // 两者落在同一个 revision 框里，靠这个标记让 MessageBubble 显示不同文案。
          if (reason === "citation") buf.byCitation = true;
        } else buf.content += text;
        deltaBufRef.current = buf;
        if (flushTimerRef.current == null) {
          // 自适应：正文超过 6000 字符后合并窗口拉长到 120ms——
          // 长回答下 Markdown 重解析是最大开销，少刷几次比 50ms 的体感延迟更重要。
          const delay = buf.content.length > 6000 ? 120 : 50;
          flushTimerRef.current = window.setTimeout(() => {
            flushTimerRef.current = null;
            const pending = deltaBufRef.current;
            if (!pending) return;
            deltaBufRef.current = null;
            patch((msg) => ({
              ...msg,
              content: pending.content ? msg.content + pending.content : msg.content,
              revision: pending.revision ? (msg.revision ?? "") + pending.revision : msg.revision,
              revisionByCitation: pending.byCitation ? true : msg.revisionByCitation,
            }));
          }, delay);
        }
      };

      /** 立刻冲刷缓冲（done / error / 中断时调用）：保证尾巴上的 delta 不丢 */
      const flushNow = () => {
        if (flushTimerRef.current != null) {
          window.clearTimeout(flushTimerRef.current);
          flushTimerRef.current = null;
        }
        const pending = deltaBufRef.current;
        if (!pending) return;
        deltaBufRef.current = null;
        patch((msg) => ({
          ...msg,
          content: pending.content ? msg.content + pending.content : msg.content,
          revision: pending.revision ? (msg.revision ?? "") + pending.revision : msg.revision,
          revisionByCitation: pending.byCitation ? true : msg.revisionByCitation,
        }));
      };

      busyRef.current = true;
      setBusy(true);
      setInput("");

      // 每个请求配一个 AbortController：新建对话 / 切会话 / 卸载时中断它，避免旧流串台
      const controller = new AbortController();
      abortRef.current = controller;

      // ---- 带了图片：走视觉解析，识别结果以卡片展示，再由用户决定是否追问 ----
      if (file) {
        const previewUrl = imageUrl;
        setImageFile(null);
        setImageUrl(null);
        setAnalyzing(true);
        setMessages((prev) => [
          ...(base ?? prev),
          {
            id: uid(),
            role: "user",
            content: question,
            // 消息头像旁的时间戳：创建当下记录，展示层 fmtTime 格式化为 HH:mm
            ts: Date.now(),
            image: previewUrl ? { url: previewUrl, name: file.name } : undefined,
          },
          { id: assistantId, role: "assistant", content: "", ts: Date.now(), analyzing: true },
        ]);

        try {
          const result = await analyzeImage(file, question);
          patch((msg) => ({
            ...msg,
            analyzing: false,
            imageResult: { text: result.text, model: result.model },
          }));
        } catch (e) {
          // 中断（停止解析）不算错误；其余情况给人话提示，原始信息折叠在技术详情里
          patch((msg) => ({
            ...msg,
            analyzing: false,
            error: controller.signal.aborted
              ? null
              : {
                  kind: "server" as const,
                  message: "图片解析失败，请换一张图片重试。",
                  detail: e instanceof Error ? e.message : String(e),
                },
          }));
        } finally {
          setAnalyzing(false);
          busyRef.current = false;
          setBusy(false);
          if (abortRef.current === controller) abortRef.current = null;
        }
        return;
      }

      // 把之前的对话整理成历史一并回传（后端按 AGENT_HISTORY_TURNS 截断），
      // 这样 Agent 能理解「它」「那个项目」这类指代。
      // 注意：助手消息只回传「被采纳的版本」——用户选了修订稿就回传修订稿，避免下一轮正文重复。
      const history: ChatTurn[] = baseMsgs
        .filter((msg) => msg.content.trim() !== "")
        .map((msg) => ({
          role: msg.role,
          content:
            msg.role === "assistant" && msg.adoptedVersion === "v2" && msg.revision
              ? msg.revision
              : msg.content,
        }));

      setMessages((prev) => [
        ...(base ?? prev),
        // 两条消息都在创建时打上时间戳，消息头像旁才会显示 HH:mm
        { id: uid(), role: "user", content: question, ts: Date.now() },
        { id: assistantId, role: "assistant", content: "", ts: Date.now(), streaming: true },
      ]);

      try {
        for await (const event of streamAgentChat(question, history, undefined, webSearchOn, mode, conversationId, controller.signal)) {
          if (event.type === "conversation") {
            // 首个事件：落库成功时记住会话 id，并通知历史面板刷新
            if (event.saved && event.id) {
              setConversationId(event.id);
              setHistoryRefreshKey((k) => k + 1);
            }
          } else if (event.type === "retrieval") patch((msg) => ({ ...msg, meta: event.meta }));
          else if (event.type === "sources") patch((msg) => ({ ...msg, sources: event.sources }));
          else if (event.type === "delta")
            // delta 先进缓冲，由定时器每 ≥50ms 合并落库一次（正文 / revision 两通道各自累计）
            bufferDelta(event.channel ?? "answer", event.text ?? "", event.reason);
          else if (event.type === "thinking")
            // 思维链单独累积到 msg.thinking（MessageBubble 渲染为可折叠「思考过程」卡）
            patch((msg) => ({ ...msg, thinking: (msg.thinking ?? "") + event.delta }));
          else if (event.type === "plan")
            patch((msg) => ({ ...msg, plan: event.steps, planFallback: event.fallback }));
          else if (event.type === "plan_step")
            patch((msg) => {
              const steps = [...(msg.steps ?? [])];
              const index = steps.findIndex((s) => s.index === event.index);
              const next: PlanStepRun = {
                index: event.index,
                title: event.title,
                status: event.status,
                observation: event.observation,
                error: event.error,
              };
              if (index >= 0) steps[index] = next;
              else steps.push(next);
              steps.sort((a, b) => a.index - b.index);
              return { ...msg, steps };
            });
          else if (event.type === "reflection")
            patch((msg) => ({
              ...msg,
              reflections: [
                ...(msg.reflections ?? []),
                { round: event.round, verdict: event.verdict, issues: event.issues },
              ],
            }));
          else if (event.type === "tool_call")
            patch((msg) => ({ ...msg, toolCalls: [...(msg.toolCalls ?? []), { name: event.name, query: event.query, arguments: event.arguments }] }));
          else if (event.type === "tool_result")
            patch((msg) => {
              const calls = [...(msg.toolCalls ?? [])];
              // 结果补到最近一次同名调用上
              for (let i = calls.length - 1; i >= 0; i -= 1) {
                if (calls[i].name === event.name) {
                  calls[i] = { ...calls[i], count: event.count };
                  break;
                }
              }
              return { ...msg, toolCalls: calls };
            });
          else if (event.type === "sql_query")
            patch((msg) => ({
              ...msg,
              sqlQueries: [
                ...(msg.sqlQueries ?? []),
                {
                  sql: event.sql,
                  columns: event.columns,
                  rowCount: event.row_count,
                  rows: event.rows,
                  error: event.error ?? null,
                },
              ],
            }));
          else if (event.type === "graph_query")
            patch((msg) => ({
              ...msg,
              graphQueries: [
                ...(msg.graphQueries ?? []),
                {
                  cypher: event.cypher,
                  rowCount: event.row_count,
                  rows: event.rows,
                  error: event.error ?? null,
                },
              ],
            }));
          else if (event.type === "web_search")
            patch((msg) => ({
              ...msg,
              webSearches: [
                ...(msg.webSearches ?? []),
                {
                  query: event.query,
                  results: event.results,
                  error: event.error ?? null,
                },
              ],
            }));
          else if (event.type === "citation_check")
            // 引用体检结论：issues 非空表示发现越界引用或无依据数值，挂到消息上供 MessageBubble 提示；
            // number_checks 是数字逐字命中明细（found=false 的数字在正文里加醒目标记）；
            // confidence / hallucination_flags 是逐句可信度（新增字段，旧后端可能没有，直接透传 undefined）
            patch((msg) => ({
              ...msg,
              citationIssues: event.issues,
              numberChecks: event.number_checks,
              confidence: event.confidence,
              supportedSentences: event.supported_sentences,
              totalSentences: event.total_sentences,
              hallucinationFlags: event.hallucination_flags,
            }));
          else if (event.type === "done") {
            // 结束前先把缓冲里的最后一段 delta 冲刷出来，再收尾状态
            flushNow();
            patch((msg) => ({ ...msg, mode: event.mode, streaming: false }));
          } else if (event.type === "error") {
            // 服务端报错不清空已输出内容，只追加错误块；缓冲里的内容同样先落库
            flushNow();
            patch((msg) => ({
              ...msg,
              error: { kind: "server" as const, message: event.message || "服务出错了，请稍后重试。", detail: event.message },
              streaming: false,
            }));
          }
        }
      } catch (e) {
        // 先冲刷缓冲：无论中断还是断网，已生成的文字一律保留
        flushNow();
        // 主动中断（停止生成 / 切会话 / 新建对话 / 卸载）不是错误：标记为已停止，保留已输出内容
        if (controller.signal.aborted) {
          patch((msg) => ({ ...msg, aborted: true, streaming: false }));
        } else {
          patch((msg) => ({
            ...msg,
            error: {
              kind: "network" as const,
              message: "网络中断，请检查网络后重试。",
              detail: e instanceof Error ? e.message : String(e),
            },
            streaming: false,
          }));
        }
      } finally {
        flushNow();
        patch((msg) => ({ ...msg, streaming: false }));
        busyRef.current = false;
        setBusy(false);
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [messages, imageFile, imageUrl, webSearchOn, mode, conversationId],
  );

  /** 发送一轮提问 */
  const send = useCallback((text: string) => void runSend(text), [runSend]);

  /** 停止生成：中断当前流式请求，已输出的内容保留 */
  const stop = useCallback(() => abortRef.current?.abort(), []);

  /** 重新生成 / 继续生成：定位该助手消息，从它前面的用户提问处重来（不把旧回答当上下文） */
  const regenerate = useCallback(
    (id: string) => {
      const idx = messages.findIndex((msg) => msg.id === id);
      if (idx < 0) return;
      for (let i = idx - 1; i >= 0; i -= 1) {
        if (messages[i].role === "user") {
          void runSend(messages[i].content, messages.slice(0, i));
          return;
        }
      }
    },
    [messages, runSend],
  );

  /** 输入框随内容长高（最多 150px，超过就内部滚动） */
  const autoGrow = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
  }, []);

  /** 输入框改动：更新文本并让高度跟随内容 */
  const handleInputChange = (value: string) => {
    setInput(value);
    requestAnimationFrame(autoGrow);
  };

  /** 点击示例问题 / 「填入提问框」：填进输入框（不直接发送），方便先看清再决定。
   *  用 useCallback 固定引用（配合 MessageBubble 的 memo），避免流式期间整列消息重渲染。 */
  const fillInput = useCallback(
    (text: string) => {
      if (busyRef.current) return;
      setInput(text);
      requestAnimationFrame(() => {
        textareaRef.current?.focus();
        autoGrow();
      });
    },
    [autoGrow],
  );

  /** 选择图片：读成 data URL 做预览（无需管理 object URL 的生命周期） */
  const handlePickImage = (file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      setImageFile(file);
      setImageUrl(typeof reader.result === "string" ? reader.result : null);
      setBanner(null);
    };
    reader.readAsDataURL(file);
  };

  const clearImage = () => {
    setImageFile(null);
    setImageUrl(null);
  };

  // ------------------------------ 滚动与证据面板 ------------------------------ //
  /** 滚动事件：更新贴底状态与两个浮动按钮的可见性（距底 80px 内视为贴底） */
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const pinned = distance < 80;
    atBottomRef.current = pinned;
    setShowJumpLatest(!pinned && el.scrollHeight > el.clientHeight + 120);
    setShowJumpTop(el.scrollTop > 320);
  }, []);

  /** 回到最新：先恢复贴底标记，平滑滚到底，按钮立刻收起以恢复跟随 */
  const jumpToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setShowJumpLatest(false);
  }, []);

  /** 回到顶部：长回答读完后快速返回开头 */
  const jumpToTop = useCallback(() => {
    scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  /**
   * 打开 / 定位证据面板：点击行内 [n] 或来源条时触发，参数为来源下标（0 起）。
   * 目标消息取「最后一条来源数足够的助手消息」——流式期间它就是当前正在回答的这条。
   */
  const openEvidence = useCallback(
    (sourceIndex = 0) => {
      const target = [...messages].reverse().find(
        (msg) => msg.role === "assistant" && (msg.sources?.length ?? 0) > sourceIndex,
      );
      if (!target) return;
      setEvidence((prev) => ({
        msgId: target.id,
        // 同一消息且已展开（半屏 / 全屏）则保持尺寸；折叠状态下点开一律展为半屏
        size: prev && prev.msgId === target.id && prev.size !== "rail" ? prev.size : "half",
        index: sourceIndex,
        nonce: (prev?.nonce ?? 0) + 1,
      }));
    },
    [messages],
  );

  /** 切换面板尺寸。面板还处于「默认折叠条」（evidence 为 null）时，用当前绑定消息补一条状态 */
  const changeEvSize = useCallback(
    (size: EvidenceSize) => {
      setEvidence((prev) => {
        if (prev) return { ...prev, size };
        const fallback = [...messages].reverse().find(
          (msg) => msg.role === "assistant" && (msg.sources?.length ?? 0) > 0,
        );
        return fallback ? { msgId: fallback.id, size, index: 0, nonce: 0 } : prev;
      });
    },
    [messages],
  );

  /** 收起面板（回到折叠条，消息与来源绑定不清） */
  const closeEvidence = useCallback(() => {
    setEvidence((prev) => (prev ? { ...prev, size: "rail" } : prev));
  }, []);

  const emptyIndex = (kb?.points ?? 0) === 0;

  /**
   * 面板绑定的助手消息：优先用户显式打开的；否则取最后一条带来源的助手消息（默认折叠条）。
   * evBound 表示 evidence 状态确实落在了一条现存消息上——否则视为默认折叠态、不带定位请求。
   */
  const evMsg =
    (evidence ? messages.find((msg) => msg.id === evidence.msgId) : undefined) ??
    [...messages].reverse().find((msg) => msg.role === "assistant" && (msg.sources?.length ?? 0) > 0);
  const evBound = Boolean(evidence && evMsg && evidence.msgId === evMsg.id);
  const evSize: EvidenceSize = evBound && evidence ? evidence.size : "rail";
  const evLocate = evBound && evidence ? { index: evidence.index, nonce: evidence.nonce } : null;
  /** 面板里的高亮词与「该轮提问」：取绑定助手消息的上一条用户消息 */
  const evMsgIndex = evMsg ? messages.indexOf(evMsg) : -1;
  const evQuery = evMsgIndex > 0 && messages[evMsgIndex - 1].role === "user" ? messages[evMsgIndex - 1].content : "";

  return (
    <div className="app">
      <ConversationRail
        user={user}
        historyEnabled={Boolean(health?.history_configured)}
        historyRefreshKey={historyRefreshKey}
        onLoginClick={() => setAuthOpen(true)}
        onLogout={() => void logout()}
        onOpenConversation={(id, messageId) => void restoreConversation(id, messageId)}
        onNewConversation={() => {
          newConversation();
          setRailOpen(false);
        }}
        open={railOpen}
        onClose={() => setRailOpen(false)}
      />

      {authOpen && (
        <AuthDialog
          onClose={() => setAuthOpen(false)}
          onSuccess={(u) => {
            setUser(u);
            setAuthOpen(false);
            setHistoryRefreshKey((k) => k + 1);
          }}
        />
      )}

      <main className="main">
        <ChatHeader mode={mode} onOpenRail={() => setRailOpen(true)} />

        <div className="messages" ref={scrollRef} onScroll={onScroll}>
          {banner && (
            <div
              className="notice notice-danger"
              style={{
                maxWidth: 800,
                margin: "0 auto 16px",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "var(--sp-8)",
              }}
            >
              <span>{banner}</span>
              <button className="btn" type="button" onClick={() => void refresh()} style={{ width: "auto" }}>
                重试
              </button>
            </div>
          )}

          {messages.length === 0 && (
            <EmptyState
              emptyIndex={emptyIndex}
              disabled={busy}
              onPick={(text) => void send(text)}
            />
          )}

          {messages.map((msg) => (
            <MessageBubble
              key={msg.id}
              msg={msg}
              highlighted={highlightId === msg.id}
              busy={busy}
              onFillInput={fillInput}
              onAdopt={setAdopted}
              onRegenerate={regenerate}
              onOpenEvidence={openEvidence}
              onToast={pushToast}
            />
          ))}
        </div>

        {/* 浮动按钮：长回答 / 上滑脱离底部时出现（回到顶部、回到最新） */}
        <div className="floating">
          {showJumpTop && (
            <button className="fab" type="button" onClick={jumpToTop} data-testid="jump-top">
              <Icon name="arrow-up" size={13} />
              回到顶部
            </button>
          )}
          {showJumpLatest && (
            <button className="fab" type="button" onClick={jumpToBottom} data-testid="jump-latest">
              <Icon name="arrow-down" size={13} />
              回到最新
            </button>
          )}
        </div>

        <Composer
          input={input}
          onInputChange={handleInputChange}
          onSend={(text) => void send(text)}
          busy={busy}
          onStop={stop}
          analyzing={analyzing}
          imageUrl={imageUrl}
          imageName={imageFile?.name}
          onPickImage={handlePickImage}
          onClearImage={clearImage}
          webSearchOn={webSearchOn}
          canWebSearch={canWebSearch}
          onToggleWebSearch={() => setWebSearch(!webSearchOn)}
          mode={mode}
          onModeChange={setMode}
          textareaRef={textareaRef}
        />

        {/* 证据面板：本轮回答有来源时渲染（默认折叠条，点行内引用或来源条展开定位） */}
        {evMsg && evMsg.sources && evMsg.sources.length > 0 && (
          <EvidencePanel
            sources={evMsg.sources}
            query={evQuery}
            size={evSize}
            onSizeChange={changeEvSize}
            onClose={closeEvidence}
            locate={evLocate}
          />
        )}
      </main>

      <ToastStack toasts={toasts} />
    </div>
  );
}