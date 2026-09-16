import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { App, Button, Empty, Input, Space, Typography, message } from "antd";
import { aiApi } from "../api";
import { API_BASE, ApiError } from "../api/client";
import { getAccessToken } from "../auth";
import {
  ChatMessageParts,
  historyToUIMessages,
  type KhUIMessage,
} from "../components/ChatMessageParts";
import type { ChatMessage, ChatSession } from "../types";
import { formatTime } from "../utils";

const CHAT_ID = "kh-chat";

export default function ChatPage() {
  const navigate = useNavigate();
  const { modal } = App.useApp();
  const [params] = useSearchParams();
  const sessionId = params.get("session") || undefined;

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [input, setInput] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const sessionIdRef = useRef(sessionId);
  const loadedSessionRef = useRef<string | undefined>(undefined);
  const pinBottomRef = useRef(true);
  sessionIdRef.current = sessionId;

  const transport = useMemo(
    () =>
      new DefaultChatTransport<KhUIMessage>({
        // 必须走 API_BASE（默认 /api/v1）：vite proxy 对 /api 前缀原样转发、
        // 不做 rewrite，漏掉 /v1 会被后端当未知路由返回 404。
        api: `${API_BASE}/ai/chat/stream`,
        headers: () => {
          const token = getAccessToken();
          const headers: Record<string, string> = {};
          if (token) headers.Authorization = `Bearer ${token}`;
          return headers;
        },
      }),
    [],
  );

  const { messages, sendMessage, setMessages, status, stop, error } =
    useChat<KhUIMessage>({
      id: CHAT_ID,
      transport,
      onData: (part) => {
        if (part.type !== "data-session") return;
        const nextId = part.data.sessionId;
        if (!nextId || nextId === sessionIdRef.current) return;
        loadedSessionRef.current = nextId;
        navigate(`/chat?session=${nextId}`, { replace: true });
      },
      onFinish: () => {
        void loadSessions();
      },
      onError: (err) => {
        message.error(err.message || "请求失败");
      },
    });

  const streaming = status === "submitted" || status === "streaming";
  const busy = streaming;

  async function loadSessions() {
    try {
      const res = await aiApi.sessions();
      setSessions(res.items);
    } catch {
      /* 列表失败不挡问答 */
    }
  }

  useEffect(() => {
    void loadSessions();
  }, []);

  useEffect(() => {
    if (streaming) return;
    if (!sessionId) {
      if (loadedSessionRef.current) {
        loadedSessionRef.current = undefined;
        setMessages([]);
      }
      return;
    }
    if (loadedSessionRef.current === sessionId) return;
    let cancelled = false;
    loadedSessionRef.current = sessionId;
    aiApi
      .messages(sessionId)
      .then((rows: ChatMessage[]) => {
        if (cancelled) return;
        setMessages(historyToUIMessages(rows));
      })
      .catch((err) => {
        if (!cancelled) {
          loadedSessionRef.current = undefined;
          message.error(err instanceof ApiError ? err.message : "加载会话失败");
          navigate("/chat", { replace: true });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, streaming, navigate, setMessages]);

  function onLogScroll() {
    const el = logRef.current;
    if (!el) return;
    pinBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  useEffect(() => {
    if (!pinBottomRef.current) return;
    requestAnimationFrame(() => {
      logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
    });
  }, [messages, status]);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    pinBottomRef.current = true;
    await sendMessage({ text }, { body: { sessionId } });
  }

  function switchSession(id?: string) {
    if (busy) {
      message.warning("请等待当前回答结束再切换会话");
      return;
    }
    navigate(id ? `/chat?session=${id}` : "/chat");
  }

  async function onNew() {
    if (busy) {
      message.warning("请等待当前回答结束再开新对话");
      return;
    }
    try {
      const created = await aiApi.createSession();
      loadedSessionRef.current = created.id;
      setMessages([]);
      navigate(`/chat?session=${created.id}`);
      void loadSessions();
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : "创建失败");
    }
  }

  function onRemove(id: string, e: MouseEvent) {
    e.stopPropagation();
    if (busy) {
      message.warning("请等待当前回答结束再删除");
      return;
    }
    modal.confirm({
      title: "确定删除对话？",
      content: "删除后，聊天记录将不可恢复。",
      okText: "删除",
      cancelText: "取消",
      okType: "danger",
      centered: true,
      onOk: async () => {
        try {
          await aiApi.removeSession(id);
          if (sessionId === id) {
            loadedSessionRef.current = undefined;
            setMessages([]);
            navigate("/chat");
          }
          void loadSessions();
        } catch (err) {
          message.error(err instanceof ApiError ? err.message : "删除失败");
          throw err;
        }
      },
    });
  }

  return (
    <div className="kh-page kh-chat-layout">
      <aside className="kh-chat-sessions">
        <Button
          type="primary"
          icon={<PlusOutlined />}
          block
          disabled={busy}
          onClick={() => void onNew()}
        >
          新对话
        </Button>
        <div className="kh-chat-session-list">
          {sessions.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="还没有会话"
            />
          ) : (
            sessions.map((s) => (
              <div
                key={s.id}
                className={`kh-chat-session-item${sessionId === s.id ? " active" : ""}${busy ? " disabled" : ""}`}
                onClick={() => switchSession(s.id)}
              >
                <div className="kh-chat-session-title">{s.title}</div>
                <div className="kh-chat-session-meta">
                  <span>{formatTime(s.updatedAt)}</span>
                  <DeleteOutlined onClick={(e) => onRemove(s.id, e)} />
                </div>
              </div>
            ))
          )}
        </div>
      </aside>
      <div className="kh-chat-main">
        <Typography.Title level={4} style={{ marginTop: 0 }}>
          知识问答
        </Typography.Title>
        <Typography.Paragraph type="secondary">
          流式回答会展示知识库检索、思考与联网搜索过程，并写入左侧会话。
        </Typography.Paragraph>
        <div className="kh-chat-log" ref={logRef} onScroll={onLogScroll}>
          {messages.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="输入问题开始一段对话"
            />
          ) : (
            messages.map((m, i) => {
              const liveAssistant =
                streaming &&
                m.role === "assistant" &&
                i === messages.length - 1;
              return (
                <div key={m.id} className={`kh-bubble ${m.role}`}>
                  <ChatMessageParts
                    messageId={m.id}
                    parts={m.parts}
                    role={m.role}
                    showSources={!liveAssistant}
                  />
                </div>
              );
            })
          )}
          {error ? <div className="kh-chat-error">{error.message}</div> : null}
        </div>
        <Space.Compact style={{ width: "100%" }}>
          <Input
            size="large"
            placeholder="例如：上线前如何做金丝雀验证？"
            value={input}
            disabled={busy}
            onChange={(e) => setInput(e.target.value)}
            onPressEnter={() => void send()}
          />
          {streaming ? (
            <Button size="large" onClick={() => void stop()}>
              停止
            </Button>
          ) : (
            <Button type="primary" size="large" onClick={() => void send()}>
              发送
            </Button>
          )}
        </Space.Compact>
      </div>
    </div>
  );
}
