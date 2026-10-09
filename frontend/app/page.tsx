"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import AuthDialog from "@/components/AuthDialog";
import ConversationRail from "@/components/chat/ConversationRail";
import EmptyState from "@/components/chat/EmptyState";
import MessageBubble, { type Message } from "@/components/chat/MessageBubble";
import Composer from "@/components/chat/Composer";
import ChatHeader from "@/components/chat/ChatHeader";
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

const uid = () => Math.random().toString(36).slice(2, 10);

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [kb, setKb] = useState<KbStats | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  /** 待上传的图片与它的预览（data URL，发送后随消息一起展示） */
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  /** 图片解析中（用于按钮/提示文案） */
  const [analyzing, setAnalyzing] = useState(false);
  /** 「联网搜索」开关：null 表示还没手动改过，用后端的默认值 */
  const [webSearch, setWebSearch] = useState<boolean | null>(null);
  /** 推理模式：默认 ReAct（恢复历史会话时会采用该会话记录的模式） */
  const [mode, setMode] = useState<ChatMode>("react");

  /** 当前登录用户（null 为未登录） */
  const [user, setUser] = useState<User | null>(null);
  /** 登录 / 注册弹窗是否打开 */
  const [authOpen, setAuthOpen] = useState(false);
  /** 当前落库的会话 id（未登录 / 未落库时为 null） */
  const [conversationId, setConversationId] = useState<string | null>(null);
  /** 历史面板刷新信号（对话落库后自增） */
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0);
  /** 从检索结果跳转过来时要高亮的消息 id（短暂高亮后清除） */
  const [highlightId, setHighlightId] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // ------------------------------ 数据加载 ------------------------------ //
  const refresh = useCallback(async () => {
    try {
      const [h, k] = await Promise.all([api.health(), api.kbStats()]);
      setHealth(h);
      setKb(k);
      setBanner(null);
    } catch (e) {
      setBanner(
        `无法连接后端 ${e instanceof Error ? e.message : String(e)}。请确认后端已在 8000 端口启动。`,
      );
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  // 启动时若本地有令牌，拉一次当前用户；令牌失效（401）则静默清除，保持未登录
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

  // 从检索结果定位到某条消息：滚动到可视区并短暂高亮
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

  /** 新建对话：清空消息与当前会话 id，模式保持用户当前选择 */
  const newConversation = useCallback(() => {
    setMessages([]);
    setConversationId(null);
    setHighlightId(null);
  }, []);

  /** 恢复历史会话：拉取详情 -> 按 meta 重建消息卡片 -> 采用该会话模式 -> 定位高亮 */
  const restoreConversation = useCallback(async (id: string, highlightMessageId?: string) => {
    try {
      const res = await api.conversationDetail(id);
      const restored: Message[] = res.messages.map((item) => {
        if (item.role === "user") {
          return { id: item.id, role: "user" as const, content: item.content };
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
          error: meta?.error ?? undefined,
        };
      });
      setMessages(restored);
      setConversationId(res.conversation.id);
      // 采用该会话记录的模式
      setMode((res.conversation.mode as ChatMode) ?? "react");
      setBanner(null);
      if (highlightMessageId) setHighlightId(highlightMessageId);
    } catch (e) {
      setBanner(`恢复会话失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }, []);

  /** 后端没配 TAVILY_API_KEY 时联网搜索不可用，开关置灰 */
  const canWebSearch = Boolean(health?.web_search_configured);
  /** 实际生效的开关值：用户没手动改过就沿用后端默认值 */
  const webSearchOn = canWebSearch && (webSearch ?? health?.web_search_default ?? false);

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      const file = imageFile;
      // 纯图片（不带文字）也允许发送
      if ((!question && !file) || busy) return;

      const assistantId = uid();
      const patch = (updater: (msg: Message) => Message) =>
        setMessages((prev) => prev.map((msg) => (msg.id === assistantId ? updater(msg) : msg)));

      setBusy(true);
      setInput("");

      // ---- 带了图片：走视觉解析，识别结果以卡片展示，再由用户决定是否追问 ----
      if (file) {
        const previewUrl = imageUrl;
        setImageFile(null);
        setImageUrl(null);
        setAnalyzing(true);
        setMessages((prev) => [
          ...prev,
          {
            id: uid(),
            role: "user",
            content: question,
            image: previewUrl ? { url: previewUrl, name: file.name } : undefined,
          },
          { id: assistantId, role: "assistant", content: "", analyzing: true },
        ]);

        try {
          const result = await analyzeImage(file, question);
          patch((msg) => ({
            ...msg,
            analyzing: false,
            imageResult: { text: result.text, model: result.model },
          }));
        } catch (e) {
          patch((msg) => ({
            ...msg,
            analyzing: false,
            error: `图片解析失败：${e instanceof Error ? e.message : String(e)}`,
          }));
        } finally {
          setAnalyzing(false);
          setBusy(false);
        }
        return;
      }

      // 把之前的对话整理成历史一并回传（后端按 AGENT_HISTORY_TURNS 截断），
      // 这样 Agent 能理解「它」「那个项目」这类指代。
      // 注意：助手消息只回传「被采纳的版本」——用户选了修订稿就回传修订稿，避免下一轮正文重复。
      const history: ChatTurn[] = messages
        .filter((msg) => msg.content.trim() !== "")
        .map((msg) => ({
          role: msg.role,
          content:
            msg.role === "assistant" && msg.adoptedVersion === "v2" && msg.revision
              ? msg.revision
              : msg.content,
        }));

      setMessages((prev) => [
        ...prev,
        { id: uid(), role: "user", content: question },
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ]);

      try {
        for await (const event of streamAgentChat(question, history, undefined, webSearchOn, mode, conversationId)) {
          if (event.type === "conversation") {
            // 首个事件：落库成功时记住会话 id，并通知历史面板刷新
            if (event.saved && event.id) {
              setConversationId(event.id);
              setHistoryRefreshKey((k) => k + 1);
            }
          } else if (event.type === "retrieval") patch((msg) => ({ ...msg, meta: event.meta }));
          else if (event.type === "sources") patch((msg) => ({ ...msg, sources: event.sources }));
          else if (event.type === "delta")
            // revision 通道是改进稿，单独缓存，不追加到正文
            patch((msg) =>
              event.channel === "revision"
                ? { ...msg, revision: (msg.revision ?? "") + event.text }
                : { ...msg, content: msg.content + event.text },
            );
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
          else if (event.type === "done") patch((msg) => ({ ...msg, mode: event.mode, streaming: false }));
          else if (event.type === "error") patch((msg) => ({ ...msg, error: event.message, streaming: false }));
        }
      } catch (e) {
        patch((msg) => ({
          ...msg,
          error: `请求失败：${e instanceof Error ? e.message : String(e)}`,
          streaming: false,
        }));
      } finally {
        patch((msg) => ({ ...msg, streaming: false }));
        setBusy(false);
      }
    },
    [busy, messages, imageFile, imageUrl, webSearchOn, mode, conversationId],
  );

  /** 输入框随内容长高（最多 150px，超过就内部滚动） */
  const autoGrow = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
  };

  /** 输入框改动：更新文本并让高度跟随内容 */
  const handleInputChange = (value: string) => {
    setInput(value);
    requestAnimationFrame(autoGrow);
  };

  /** 点击示例问题 / 「填入提问框」：填进输入框（不直接发送），方便先看清再决定 */
  const fillInput = (text: string) => {
    if (busy) return;
    setInput(text);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      autoGrow();
    });
  };

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

  const emptyIndex = (kb?.points ?? 0) === 0;

  return (
    <div className="app">
      <ConversationRail
        user={user}
        historyEnabled={Boolean(health?.history_configured)}
        historyRefreshKey={historyRefreshKey}
        onLoginClick={() => setAuthOpen(true)}
        onLogout={() => void logout()}
        onOpenConversation={(id, messageId) => void restoreConversation(id, messageId)}
        onNewConversation={newConversation}
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
        <ChatHeader mode={mode} />

        <div className="messages" ref={scrollRef}>
          {banner && <div className="notice notice-danger" style={{ maxWidth: 900, margin: "0 auto 16px" }}>{banner}</div>}

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
            />
          ))}
        </div>

        <Composer
          input={input}
          onInputChange={handleInputChange}
          onSend={(text) => void send(text)}
          busy={busy}
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
      </main>
    </div>
  );
}